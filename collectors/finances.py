"""What UBC costs a student: tuition rates, fees, and first-year estimates.

Three sources, because UBC publishes cost in three unrelated places and none of
them is an API:

- **The Academic Calendar's `/fees/` pages** carry the authoritative rates as
  HTML tables -- per-credit tuition by program for domestic and international
  students, the latter split across seven cohort columns because international
  tuition is fixed at the rate of the year you started. Also flat-rate programs
  (Dentistry, Medicine, Vantage), certificates, graduate programs, and the
  Board- and society-approved student fees.
- **you.ubc.ca's cost estimator** carries UBC's own first-year estimate per
  program area -- tuition, student fees, lab fees, books -- plus what it assumes
  housing, meals, rent and groceries cost. Its tuition figures are already
  computed on a 30-credit load, which is what makes them estimates rather than
  rates.
- **The program finder** supplies the programs those estimates get attached to.

The tables are read into one long format rather than mirrored column-for-column.
UBC's fee tables have between one and eight value columns and change shape from
page to page; a row per (item, column) cell survives that, keeps the column
header verbatim, and derives `student_type`, `cohort_year` and `unit` from it.
Anything on those pages that isn't a money cell -- footnotes, conditions,
instalment counts -- goes to `fee_notes` rather than being dropped.

`program_cost_estimates` is the one derived table: each program joined to the
estimator area that covers it, with the year-one total added up. The join is
UBC's own data matched on names, not a mapping typed out here, so every row
records how it was matched and `_unmatched.json` lists what didn't.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import admissions, htmldoc
from .base import (
    Collector,
    Http,
    Output,
    js_literal,
    jsonapi_collection,
    register,
    wants,
)

CAMPUSES = {
    "vancouver": "vancouver.calendar.ubc.ca",
    "okanagan": "okanagan.calendar.ubc.ca",
}

YOU_HOST = "you.ubc.ca"
PROGRAMS_URL = f"https://{YOU_HOST}/programs/"

FEES_SECTION = "/fees"

MONEY_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")

# "commenced in 2023S or 2023W" on a column header, and the bare "2025S or
# 2025W" the graduate tables use as an in-table divider instead.
COHORT_RE = re.compile(r"commenc\w*\s+in\s+(\d{4})|\b(\d{4})[SW]\b", re.I)

# A footnote marker opening a cell: "1 Offered by the Faculty of Pharmacy".
# Distinguishable from a cohort divider, which runs digits into a letter.
FOOTNOTE_RE = re.compile(r"^\d+\s+\S")

# Longest a single-cell row can be and still be read as a divider labelling the
# rows beneath it rather than a note written under the table.
DIVIDER_LIMIT = 80

# What a figure is charged against. Checked in order, so "per credit" wins over
# a bare "credit" appearing elsewhere in a long header.
UNITS = (
    ("per_credit", re.compile(r"per[- ]credit", re.I)),
    # "Instalments per year: 3" says how a year is split, not what is charged
    # annually, so it must not read as a per-year rate.
    ("per_year", re.compile(
        r"(?<!instalments\s)(?<!installments\s)per\s+(year|annum)|annually|per\s+session", re.I)),
    ("per_term", re.compile(r"per\s+term", re.I)),
    ("per_month", re.compile(r"per\s+month|monthly", re.I)),
    # Graduate tables often name the unit nowhere but the row label ("Instalment
    # 1") or the preamble ("Instalments per year: 3"), so this one is loose. It
    # is checked last, and only against a text no earlier pattern matched.
    ("per_instalment", re.compile(r"inst[ai]lment", re.I)),
)

# Which page a rate came off, which is the only place its level is recorded.
LEVELS = (
    ("masters", "/fees/tuition-fees/graduate/masters"),
    ("doctoral", "/fees/tuition-fees/graduate/doctoral"),
    ("graduate", "/fees/tuition-fees/graduate"),
    ("certificate_diploma", "/fees/tuition-fees/certificate-and-diploma-programs"),
    ("vantage_college", "/fees/tuition-fees/vantage-college"),
    ("dual_degree", "/fees/tuition-fees/undergraduatemaster-management-dual-degree"),
    ("undergraduate", "/fees/tuition-fees/undergraduate"),
)

TUITION_PREFIX = "/fees/tuition-fees"

# A full-time year at UBC, and the load the cost estimator states its own
# tuition figures on. Used to turn a per-credit rate into an annual one.
CREDITS_PER_YEAR = 30

# Tokens that say nothing about which program a name refers to.
STOPWORDS = {"bachelor", "of", "the", "in", "and", "a", "option", "program", "degree"}


def _money(text: str) -> float | None:
    match = MONEY_RE.search(text or "")
    return float(match.group(1).replace(",", "")) if match else None


def _unit(*texts: str) -> str | None:
    """What a figure is charged against, from the most specific text that says.

    Texts are tried in the order given rather than patterns in the order listed,
    so a column header naming its own unit always beats one inferred from the
    heading three levels up.
    """
    for text in texts:
        for name, pattern in UNITS:
            if pattern.search(text or ""):
                return name
    return None


def _student_type(*texts: str) -> str | None:
    joined = " ".join(text or "" for text in texts).lower()
    if "international" in joined:
        return "international"
    if "domestic" in joined or "canadian" in joined:
        return "domestic"
    return None


def _cohort(*texts: str) -> tuple[int | None, str | None]:
    """The intake a rate applies to, from the first text that names one.

    International tuition is frozen at the rate of the year a student started,
    so the tables are cut by cohort. Undergraduate pages put that in the column
    header ("commenced in 2023S or 2023W"); the graduate pages put it in a
    divider row instead. Callers pass both, most specific first.
    """
    for text in texts:
        match = COHORT_RE.search(text or "")
        if not match:
            continue
        year = int(match.group(1) or match.group(2))
        lowered = text.lower()
        if "or later" in lowered:
            return year, "or_later"
        if "or earlier" in lowered:
            return year, "or_earlier"
        return year, "exactly"
    return None, None


def _level(alias: str) -> str | None:
    return next((name for name, prefix in LEVELS if alias.startswith(prefix)), None)


def _tokens(text: str) -> tuple[str, ...]:
    words = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split()
    return tuple(word for word in words if word not in STOPWORDS)


@register
class Finances(Collector):
    name = "finances"
    title = "Tuition, student fees and first-year cost estimates"
    description = (
        "What studying at UBC costs: per-credit tuition rates by program for domestic "
        "and international students including the cohort year each rate applies to, "
        "flat-rate and per-instalment programs, Board- and society-approved student "
        "fees, UBC's own first-year cost estimate for every program area with the "
        "housing, meal, rent and grocery figures behind it, and a per-program estimate "
        "derived by joining the two."
    )
    sources = (
        f"https://{CAMPUSES['vancouver']}{FEES_SECTION}",
        PROGRAMS_URL,
    )

    def collect(self, http: Http, out: Output) -> None:
        _describe(out)

        tuition: list[dict[str, Any]] = []
        fees: list[dict[str, Any]] = []
        notes: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []

        for campus, host in CAMPUSES.items():
            if not wants(campus):
                continue
            pages = self._fee_pages(http, host)
            sources.append({"campus": campus, "host": host, "fee_pages": len(pages)})
            for page in pages:
                rows, page_notes = _parse_page(page, campus=campus, host=host)
                target = tuition if page["alias"].startswith(TUITION_PREFIX) else fees
                target.extend(rows)
                notes.extend(page_notes)

        # `program` only means something on the tuition pages; a fee row's label
        # names a fee, not a program, so it keeps `item` and nothing more. The
        # annual figure is set on every row, null where it does not apply, so
        # the table has one shape rather than two.
        for row in tuition:
            row["program"], row["applies_to"] = _name(row)
            per_credit = row["unit"] == "per_credit" and row["amount"] is not None
            row["annual_at_30_credits"] = (
                round(row["amount"] * CREDITS_PER_YEAR, 2) if per_credit else None
            )

        fees_url = f"https://{CAMPUSES['vancouver']}{FEES_SECTION}"
        out.table("tuition", tuition, source=fees_url)
        out.table("student_fees", fees, source=fees_url)
        out.table("fee_notes", notes, source=fees_url)

        estimates, living, programs, unmatched = self._estimates(http)
        out.table("cost_estimates", estimates, source=PROGRAMS_URL)
        out.table("living_costs", living, source=PROGRAMS_URL)
        out.table("program_cost_estimates", programs, source=PROGRAMS_URL)

        out.json("_sources.json", {
            "calendar": sources,
            "estimator": {"url": PROGRAMS_URL, "areas": len(estimates)},
            "credits_per_year": CREDITS_PER_YEAR,
        })
        if unmatched:
            out.json("_unmatched.json", unmatched)

    # -- the calendar's fee pages -------------------------------------------

    def _fee_pages(self, http: Http, host: str) -> list[dict[str, Any]]:
        """Calendar pages under /fees, with their body HTML.

        The JSON:API rejects a filter on the computed path alias, so the page
        collection comes down whole and is filtered here. It is ~30 requests.
        """
        pages = []
        for record in jsonapi_collection(http, host, "node/ubc_page"):
            alias = record.get("alias") or ""
            if not alias.startswith(FEES_SECTION):
                continue
            body = record.get("body") or {}
            pages.append({
                "title": record.get("title"),
                "alias": alias,
                "html": body.get("value") if isinstance(body, dict) else body,
            })
        return sorted(pages, key=lambda page: page["alias"])

    # -- you.ubc.ca's cost estimator ----------------------------------------

    def _estimates(
        self, http: Http
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[Any]]:
        html = http.get(PROGRAMS_URL).text
        data = js_literal(html, "costEstimatorData")
        costs = data.get("estimatorCosts") or {}

        estimates: list[dict[str, Any]] = []
        living: list[dict[str, Any]] = []

        for campus, payload in costs.items():
            if not campus or not isinstance(payload, dict) or not wants(campus):
                continue
            labels = data.get(f"{campus}ProgramLabels") or {}
            for key, values in (payload.get("programs") or {}).items():
                if isinstance(values, dict):
                    estimates.append(_estimate_row(campus, key, labels.get(key, key), values))
            living.extend(_living_rows(campus, payload.get("expenses") or {}))

        # The page ships both campuses; the estimates above are already filtered,
        # so the programs attached to them have to be filtered the same way.
        programs = admissions.on_campus(
            js_literal(html, "programs"), admissions.campus_ids(js_literal(html, "campuses"))
        )
        matched, unmatched = _match_programs(programs, js_literal(html, "degrees"), estimates)
        return estimates, living, matched, unmatched


# --------------------------------------------------------------------------
# Fee tables
# --------------------------------------------------------------------------


@dataclass
class Shape:
    """How one fee table is laid out, once its header has been located."""

    headers: list[str]
    context: list[str]
    body: list[list[str]]
    label: str | None = None


def _shape(table: htmldoc.Table) -> Shape:
    """Work out a table's header row, its preamble, and where its data starts.

    The graduate fee pages never open a `<th>`, so `htmldoc` has no header to
    hand back and the first rows are a mix of things: the program the table is
    about, an instalment count, then the real header. Two tells sort it out.
    The money says where the data begins -- everything above the first row
    carrying an amount is preamble. And a header is the preamble row that fills
    the *value* columns, not the label column: "Instalments per year | 3" fills
    one and is a fact about the program, while "Schedule | Domestic fee |
    International fee" fills two and names what is below it.

    A table with no `<th>` also puts its subject in the very first cell, above
    or beside the headers, which is the only place the graduate pages name the
    program a table is for.
    """
    headers, rows = table.headers, table.rows
    # A one-cell <th> row is the table's title, not its header -- "Doctor of
    # Pharmacy Flexible Program" sits above the real header three rows down.
    # Push it back into the body so the search below can find the real one.
    if headers and sum(1 for cell in headers if cell) <= 1:
        headers, rows = [], [headers, *rows]
    if headers:
        return Shape(headers, [], rows)

    label = rows[0][0] if rows and rows[0] else None

    first = next(
        (index for index, row in enumerate(rows)
         if any(_money(cell) is not None for cell in row)),
        None,
    )
    if first is None:
        return Shape([], [], rows, label)

    preamble = rows[:first]
    widths = [sum(1 for cell in row[1:] if cell) for row in preamble]
    best = max(widths, default=0)
    if best < 2:
        return Shape([], [_join(row) for row in preamble], rows[first:], label)

    header_at = widths.index(best)
    # Rows between the header and the first amount are dividers, not preamble;
    # leave them in the body so they can label the rows they head.
    return Shape(
        preamble[header_at],
        [_join(row) for row in preamble[:header_at]],
        rows[header_at + 1:],
        label,
    )


def _join(cells: list[str]) -> str:
    return " ".join(cell for cell in cells if cell).strip()


def _parse_page(
    page: dict[str, Any], *, campus: str, host: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Melt every table on a fee page into one row per money cell.

    UBC's fee tables run from one to eight value columns and reshape from page
    to page, so nothing here assumes a layout. The row label and the column
    header are kept verbatim next to what was derived from them, and every row
    that carries no money is kept too -- as a divider labelling the rows below
    it, as a note, or both.
    """
    alias = page["alias"]
    common = {
        "campus": campus,
        "level": _level(alias),
        "page": page["title"],
        "url": f"https://{host}{alias}",
    }

    rows: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    section = ""

    for block in htmldoc.blocks(page["html"] or ""):
        if isinstance(block, htmldoc.Heading):
            section = block.text
            continue

        shape = _shape(block)
        headers, context = shape.headers, shape.context
        where = {**common, "section": section, "context": context, "table_label": shape.label}
        divider = ""

        for cells in shape.body:
            amounts = [
                (index, cell) for index, cell in enumerate(cells)
                # A footnote can carry a dollar sign without being a rate.
                if _money(cell) is not None and not FOOTNOTE_RE.match(cell)
            ]
            if not amounts:
                text = _join(cells)
                if not text:
                    continue
                filled = sum(1 for cell in cells if cell)
                kind = "note"
                if filled == 1 and len(text) <= DIVIDER_LIMIT and not FOOTNOTE_RE.match(text):
                    divider, kind = text, "divider"
                notes.append({**where, "kind": kind, "note": text})
                continue

            # When the first cell is itself an amount the row has no label, and
            # the divider or preamble above it is the only thing naming it.
            item = cells[0] if amounts[0][0] != 0 else ""

            for index, cell in amounts:
                header = headers[index] if index < len(headers) else ""
                year, rule = _cohort(header, divider, item)
                rows.append({
                    **where,
                    "divider": divider,
                    "item": item,
                    "student_type": _student_type(
                        header, divider, section, headers[0] if headers else ""
                    ),
                    "cohort_year": year,
                    "cohort_rule": rule,
                    "unit": _unit(header, cell, item, _join(context), divider, section),
                    "amount": _money(cell),
                    "amount_text": cell,
                    "column": header,
                })

    return rows, notes


YEARS_RE = re.compile(r"\((Years?\s[^)]*)\)", re.I)

# A footnote marker welded onto the end of a name: "Unclassified1", "Visiting2".
TRAILING_MARKER_RE = re.compile(r"(?<=[a-z)])\d{1,2}$")


def _name(row: dict[str, Any]) -> tuple[str, str | None]:
    """The program a tuition row is about, and the year band it applies to.

    The row label carries both -- "Applied Science (Years 2 to 5)" -- except on
    the graduate pages, where the label is an instalment number or an entry year
    and the program names the table instead. Splitting the band off is what lets
    the name join to anything else.
    """
    name = row["table_label"] or (row["context"][0] if row["context"] else "") or row["item"]
    match = YEARS_RE.search(name or "")
    program = YEARS_RE.sub("", name or "").strip()
    return TRAILING_MARKER_RE.sub("", program).strip(), match.group(1).strip() if match else None


# --------------------------------------------------------------------------
# Cost estimator
# --------------------------------------------------------------------------

ESTIMATE_FIELDS = (
    "tuition_domestic",
    "tuition_international",
    "student_fees",
    "lab_clinic_fees_domestic",
    "lab_clinic_fees_international",
    "books_supplies",
)

# The estimator states housing and meal plans as a year and everything else as
# a month; nothing in the payload says so, only the copy around it.
LIVING_BASIS = {"rent": "per_month", "groceries": "per_month"}


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _estimate_row(campus: str, key: str, label: str, values: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {"campus": campus, "area_key": key, "area": label}
    for field in ESTIMATE_FIELDS:
        row[field] = _number(values.get(field))
    row["custom_tuition_message"] = htmldoc.text(values.get("custom_tuition_message") or "")

    for student_type in ("domestic", "international"):
        parts = [
            row[f"tuition_{student_type}"],
            row["student_fees"],
            row[f"lab_clinic_fees_{student_type}"],
            row["books_supplies"],
        ]
        row[f"educational_total_{student_type}"] = (
            round(sum(part for part in parts if part is not None), 2)
            if row[f"tuition_{student_type}"] is not None
            else None
        )
    return row


def _living_rows(campus: str, expenses: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item, value in expenses.items():
        variants = value if isinstance(value, dict) else {"standard": value}
        for variant, amount in variants.items():
            rows.append({
                "campus": campus,
                "item": item,
                "variant": variant,
                "amount": _number(amount),
                "basis": LIVING_BASIS.get(item, "per_academic_year"),
            })
    return rows


def _match_programs(
    programs: Any, degrees: Any, estimates: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Attach each program to the estimator area that prices it.

    The estimator is per faculty or school; the finder is per program. Nothing
    in either links them, so they are matched on names: an exact token match on
    the program title or one of its degrees first, then the area sharing the
    most tokens with it, and only where that area is unique. Ties are left
    unmatched rather than guessed -- "Bachelor of Education" really is
    ambiguous across two Education areas, and a wrong price is worse than none.
    """
    rows = programs if isinstance(programs, list) else list((programs or {}).values())
    degree_names = {
        str(term.get("term_id")): term.get("name")
        for term in (degrees if isinstance(degrees, list) else (degrees or {}).values())
        if isinstance(term, dict)
    }

    exact: dict[tuple[str, ...], dict[str, Any]] = {}
    vocabulary: list[tuple[set[str], dict[str, Any]]] = []
    for estimate in estimates:
        keys = (_tokens(estimate["area"]), _tokens(estimate["area_key"]))
        for key in keys:
            exact.setdefault(key, estimate)
        vocabulary.append((set(keys[0]) | set(keys[1]), estimate))

    def overlap(tokens: tuple[str, ...]) -> dict[str, Any] | None:
        wanted = set(tokens)
        if not wanted:
            return None
        scored = [(len(wanted & words), estimate) for words, estimate in vocabulary]
        best = max(score for score, _ in scored)
        # One shared word is noise -- "science" alone sits in six area names.
        if best < 2:
            return None
        winners = [estimate for score, estimate in scored if score == best]
        return winners[0] if len(winners) == 1 else None

    matched: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []

    for program in rows:
        if not isinstance(program, dict):
            continue
        names = [name for name in (
            degree_names.get(str(term)) for term in (program.get("degrees") or [])
        ) if name]
        candidates = [("title", program.get("post_title"))] + [("degree", n) for n in names]

        estimate = how = None
        for source, name in candidates:
            estimate = exact.get(_tokens(name))
            if estimate:
                how = f"exact:{source}"
                break
        if not estimate:
            for source, name in candidates:
                estimate = overlap(_tokens(name))
                if estimate:
                    how = f"overlap:{source}"
                    break

        identity = {
            "program_id": program.get("id"),
            "program": program.get("post_title"),
            "degrees": names,
            "url": program.get("link"),
        }
        if not estimate:
            unmatched.append({**identity, "reason": "no unique cost-estimator area"})
            continue

        matched.append({
            **identity,
            "campus": estimate["campus"],
            "area_key": estimate["area_key"],
            "area": estimate["area"],
            "matched_by": how,
            **{field: estimate[field] for field in ESTIMATE_FIELDS},
            "educational_total_domestic": estimate["educational_total_domestic"],
            "educational_total_international": estimate["educational_total_international"],
            "custom_tuition_message": estimate["custom_tuition_message"],
        })

    return matched, unmatched


# --------------------------------------------------------------------------
# Documentation
# --------------------------------------------------------------------------


def _describe(out: Output) -> None:
    money = "amount in CAD"
    out.describe(
        "tuition",
        grain="one tuition figure: a program, a column of the table it came from, "
              "and what that column charges",
        columns={
            "level": "undergraduate, certificate_diploma, masters, doctoral, "
                     "vantage_college or dual_degree -- from the page the rate is on",
            "program": "program the rate applies to, with any year band removed",
            "applies_to": "the year band, e.g. `Years 2 to 5`, when the rate is banded",
            "student_type": "domestic or international",
            "cohort_year": "intake year the rate is frozen at; international rates only",
            "cohort_rule": "exactly, or_later or or_earlier -- how to read cohort_year",
            "unit": "per_credit, per_year, per_instalment or per_term",
            "amount": money,
            "annual_at_30_credits": "derived: amount x 30, for per-credit rates only. "
                                    "30 credits is a full-time year and the load UBC's own "
                                    "estimator uses",
            "amount_text": "the cell as UBC wrote it",
            "column": "the column header, verbatim",
            "section": "the heading above the table",
            "item": "the row label, verbatim; empty when the row had no label",
            "context": "rows above the header inside the same table -- on the graduate "
                       "pages this is where the program name and instalment count live",
            "table_label": "the table's own first cell, which is what `program` is "
                           "usually taken from",
            "divider": "the in-table row heading the amount sits under, e.g. `2026S or 2026W`",
            "url": "the calendar page",
        },
        joins=["program ~ finances/program_cost_estimates.area (by name, not an id)"],
    )
    out.describe(
        "student_fees",
        grain="one student fee as charged to one category of student",
        columns={
            "item": "the fee, e.g. `Athletics and Recreation Fee`",
            "column": "who pays it, e.g. `Undergraduate and Certificates Full-Time`",
            "unit": "per_year, per_credit or per_term",
            "amount": money,
            "section": "the heading above the table; says whether the Board of Governors "
                       "or a student society sets the fee",
            "url": "the calendar page",
        },
        joins=[],
    )
    out.describe(
        "fee_notes",
        grain="one row from a fee table that carries no money -- footnotes, eligibility "
              "conditions and instalment counts, kept because they qualify the rates",
        columns={"section": "the heading above the table", "note": "the row's text",
                 "url": "the calendar page"},
        joins=["url -> finances/tuition.url"],
    )
    out.describe(
        "cost_estimates",
        grain="UBC's own first-year cost estimate for one program area",
        columns={
            "area_key": "the estimator's slug, e.g. `land_food_systems_applied_biology`",
            "area": "the area as the estimator names it",
            "tuition_domestic": "year-one tuition on a 30-credit load, CAD",
            "tuition_international": "year-one tuition on a 30-credit load, CAD",
            "student_fees": "year-one student fees (U-Pass, basic health insurance...)",
            "lab_clinic_fees_domestic": "lab or clinic fees, where the area charges them",
            "lab_clinic_fees_international": "lab or clinic fees, where the area charges them",
            "books_supplies": "estimated books and supplies for the year",
            "educational_total_domestic": "derived: tuition + fees + lab + books",
            "educational_total_international": "derived: tuition + fees + lab + books",
            "custom_tuition_message": "caveat UBC attaches to this area's figure",
        },
        joins=["(campus, area_key) -> finances/program_cost_estimates"],
    )
    out.describe(
        "living_costs",
        grain="one cost-of-living figure the estimator assumes",
        columns={
            "item": "campus_housing_single, meal_plan, rent, groceries...",
            "variant": "standard, or vantage_one where Vantage College differs",
            "amount": money,
            "basis": "per_academic_year for housing and meal plans, per_month for the rest",
        },
        joins=[],
    )
    out.describe(
        "program_cost_estimates",
        grain="one undergraduate program with the first-year cost of the area that prices it",
        columns={
            "program_id": "joins to admissions/programs.json `id`",
            "program": "program name as the finder lists it",
            "degrees": "degree names this program leads to; what the match usually ran on",
            "campus": "campus the estimate is for",
            "area": "the cost-estimator area, e.g. `Land and Food Systems: Applied Biology`",
            "area_key": "the cost-estimator area matched to this program",
            "tuition_domestic": "year-one tuition on a 30-credit load, CAD",
            "tuition_international": "year-one tuition on a 30-credit load, CAD",
            "student_fees": "year-one student fees",
            "books_supplies": "estimated books and supplies for the year",
            "matched_by": "how the match was made -- exact or overlap, on the program title "
                          "or on one of its degrees. Programs with no unique area are in "
                          "_unmatched.json rather than here",
            "educational_total_domestic": "derived: tuition + fees + lab + books for year one",
            "educational_total_international": "derived: tuition + fees + lab + books for year one",
        },
        joins=[
            "program_id -> admissions/programs.id",
            "program_id -> admissions/requirements/program_requirements.program_id",
            "(campus, area_key) -> finances/cost_estimates",
        ],
    )
