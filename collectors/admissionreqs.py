"""What each undergraduate program actually requires you to have taken.

The program finder tells you a degree exists; it does not tell you that Commerce
wants Pre-Calculus 12. That lives behind the "Program requirements" section of
each program page, and it is not in the page source either -- the page ships a
dropdown of provinces and countries and calls `admin-ajax.php` for the rules
that apply to whichever one you pick, because the answer genuinely differs by
curriculum. Ontario's requirement for Arts is "ENG4U"; British Columbia's is
"English Studies 12 or English First Peoples 12"; China's is written as prose
about Upper Middle School years.

So this drives the same endpoint the page does. Two things make that tractable:

- **Requirements are shared, not per-program.** Every program page declares a
  `requirement` key in `programRequirementsSettings`, and 122 Vancouver programs
  resolve to 20 keys -- all 41 Arts majors ask the same thing of an applicant.
  Fetching per key rather than per program turns 15,000 requests into 2,500,
  and `program_requirements` keeps the program-to-key mapping so nothing is lost.
- **The IB tab is rendered server-side.** Unlike the province and country tabs,
  it sits in the page HTML already, so it costs no extra request.

The output is deliberately two-layered: `by_location` keeps UBC's own prose per
group and location, and `required_courses` explodes the `<li>` lists inside it
into one row per requirement, which is the form an actual question -- "what do I
need for Engineering from Alberta?" -- can be answered from directly.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import htmldoc
from .base import Http, Output, wants

HOST = "you.ubc.ca"
ENDPOINT = f"https://{HOST}/wp-admin/admin-ajax.php"

SETTINGS_RE = re.compile(r"\bvar\s+programRequirementsSettings\s*=\s*(\{.*?\});", re.S)
OPTIONS_RE = re.compile(r'data-taxonomy="([^"]+)"(.*?)</select>', re.S)
OPTION_RE = re.compile(r'value="(\d+)"\s+data-slug="([^"]*)"\s+data-select="([^"]*)"')
IB_TAB_RE = re.compile(r'id="requirement_ib">(.*?)(?=<div class="tabs-content-item"|</section>)', re.S)

# The IB tab is one static block; give it the same shape as an AJAX answer so a
# reader does not have to special-case the curriculum they are asking about.
IB_TAXONOMY = "requirement_ib"
IB_LOCATION = {"term_id": None, "slug": "ib-diploma", "name": "International Baccalaureate"}

# UBC keys the specific requirements by grade band, and uses a different key
# when a curriculum has no grade bands to speak of. `kind` in the output names
# which one a row came from so the distinction survives. Order matters:
# `course_suggestions` and `related_courses` are the same thing under two names
# and UBC's own script takes the first that is set, so the first wins here too.
SPECIFIC_FIELDS = (
    ("minimum_requirements", "minimum"),
    ("grade_12", "grade_12"),
    ("grade_11", "grade_11"),
    ("recommended_grade_range", "recommended_grade_range"),
    ("course_suggestions", "suggested_subjects"),
    ("related_courses", "suggested_subjects"),
)

# Every kind a summary row can carry, so all rows have the same columns whether
# or not a given curriculum states that kind.
KINDS = tuple(dict.fromkeys(kind for _, kind in SPECIFIC_FIELDS))


def _summary(**values: Any) -> dict[str, Any]:
    """A `by_location` row with every column present, unset ones null.

    Curricula state different subsets -- Ontario gives grade bands, China gives
    one block of minimum requirements -- and a table whose columns depend on the
    row is a nuisance to read. So the shape is fixed here and filled in there.
    """
    row: dict[str, Any] = {
        "available": False,
        "specific_name": None,
        "english": None,
        "general": None,
        "profile_note": None,
        "note": None,
        **dict.fromkeys(KINDS),
    }
    row.update(values)
    return row


# Requirements are advice on these two; everything else is a hard gate.
ADVISORY = {"suggested_subjects", "recommended_grade_range"}

# Headings the IB tab uses, mapped onto the AJAX response's field names.
IB_HEADINGS = (
    ("english", "english-language"),
    ("general", "general admission"),
    ("specific", "degree-specific"),
    ("related_courses", "related courses"),
)


def _text(value: Any) -> str:
    return htmldoc.text(value) if isinstance(value, str) else ""


def _settings(html: str) -> dict[str, Any] | None:
    match = SETTINGS_RE.search(html)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _locations(html: str) -> dict[str, list[dict[str, Any]]]:
    """The province and country dropdowns a program page offers."""
    found: dict[str, list[dict[str, Any]]] = {}
    for taxonomy, body in OPTIONS_RE.findall(html):
        found[taxonomy] = [
            {"term_id": int(term_id), "slug": slug, "name": htmldoc.clean(name)}
            for term_id, slug, name in OPTION_RE.findall(body)
        ]
    return found


def _ib_sections(html: str) -> dict[str, Any] | None:
    """The pre-rendered IB tab, reshaped to look like an AJAX response.

    Its headings are the same four the AJAX handler builds client-side, so
    matching on them keeps one parser downstream instead of two.
    """
    match = IB_TAB_RE.search(html)
    if not match:
        return None

    specific_name = ""
    buckets: dict[str, list[str]] = {}
    prose: dict[str, str] = {}

    for section in htmldoc.sections(match.group(1)):
        heading = section.heading.lower()
        field = next((name for name, marker in IB_HEADINGS if marker in heading), None)
        if field is None:
            continue
        if field == "specific":
            # "Degree-specific requirements: Commerce (UBC Sauder School of Business)"
            specific_name = section.heading.split(":", 1)[-1].strip()
        buckets.setdefault(field, []).extend(section.items)
        if section.text:
            prose[field] = section.text

    if not buckets:
        return None

    return {
        "result": True,
        "english": prose.get("english", ""),
        "english_items": buckets.get("english", []),
        "general": prose.get("general", ""),
        "general_items": buckets.get("general", []),
        "specific_name": specific_name,
        "specific_items": buckets.get("specific", []),
        "related_items": buckets.get("related_courses", []),
        "related_text": prose.get("related_courses", ""),
    }


def _rows_from_ajax(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Split one AJAX answer into its summary row and its requirement lines."""
    specific = payload.get("specific")
    if not isinstance(specific, dict) or not specific:
        return _summary(), []

    name, fields = next(iter(specific.items()))
    if not isinstance(fields, dict):
        fields = {}

    summary = _summary(
        available=True,
        specific_name=htmldoc.clean(name),
        english=_text(payload.get("english")),
        general=_text(payload.get("general")),
        profile_note=_text((payload.get("profile") or {}).get("description")),
    )

    lines: list[dict[str, Any]] = []
    for field, kind in SPECIFIC_FIELDS:
        value = fields.get(field)
        if not isinstance(value, str) or not value.strip() or summary[kind] is not None:
            continue
        summary[kind] = htmldoc.text(value)
        for position, item in enumerate(htmldoc.items(value), start=1):
            lines.append({"kind": kind, "position": position, "requirement": item})

    # General admission rules are a requirement too, and asking "what do I need"
    # should not silently drop "graduation from high school".
    for field, kind in (("general", "general"), ("english", "english_language")):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            for position, item in enumerate(htmldoc.items(value), start=1):
                lines.append({"kind": kind, "position": position, "requirement": item})

    return summary, lines


def _rows_from_ib(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    summary = _summary(
        available=True,
        specific_name=payload["specific_name"],
        english=payload["english"] or " ".join(payload["english_items"]),
        general=" ".join(payload["general_items"]) or payload["general"],
        # The IB tab states one block of degree-specific rules with no grade
        # bands, which is the same thing `minimum_requirements` is elsewhere.
        minimum=" ".join(payload["specific_items"]) or None,
        suggested_subjects=" ".join(payload["related_items"]) or None,
    )
    lines: list[dict[str, Any]] = []
    for kind, items in (
        ("minimum", payload["specific_items"]),
        ("general", payload["general_items"]),
        ("english_language", payload["english_items"]),
        ("suggested_subjects", payload["related_items"]),
    ):
        for position, item in enumerate(items, start=1):
            lines.append({"kind": kind, "position": position, "requirement": item})
    return summary, lines


def collect(
    http: Http,
    out: Output,
    *,
    programs: list[dict[str, Any]],
    degrees: dict[str, Any],
) -> None:
    """Fetch every requirement group against every curriculum location."""
    pages = http.map(lambda program: _read_program(http, program), programs)

    program_rows: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    locations: dict[tuple[str, int], dict[str, Any]] = {}
    ib_payloads: dict[tuple[str, str], dict[str, Any]] = {}

    for program, page in zip(programs, pages):
        names = [degrees.get(str(term)) for term in (program.get("degrees") or [])]
        row = {
            "program_id": program.get("id"),
            "program": program.get("post_title"),
            "degrees": [name for name in names if name],
            "url": program.get("link"),
            "campus": None,
            "requirement_key": None,
            "has_requirements": False,
            "note": None,
        }

        settings = (page or {}).get("settings")
        if not settings:
            row["note"] = (page or {}).get("error") or "no requirements widget on the program page"
            program_rows.append(row)
            continue

        campus = str(settings.get("campus") or "").lower() or None
        key = settings.get("requirement")
        row.update(campus=campus, requirement_key=key, has_requirements=bool(key))
        program_rows.append(row)

        if not key:
            continue
        if not wants(campus):
            # A program can reach this run through the finder's campus filter
            # while its page declares the other campus. Say so, rather than
            # leaving a row that claims requirements exist and points nowhere.
            row["note"] = f"program page declares the {campus} campus, out of scope for this run"
            continue

        group = groups.setdefault(
            (campus, key),
            {"campus": campus, "requirement_key": key, "name": None, "programs": [],
             "program_count": 0},
        )
        group["programs"].append(program.get("post_title"))
        group["program_count"] += 1

        for taxonomy, terms in (page.get("locations") or {}).items():
            for term in terms:
                locations.setdefault((taxonomy, term["term_id"]), {"taxonomy": taxonomy, **term})

        if page.get("ib") and (campus, key) not in ib_payloads:
            ib_payloads[(campus, key)] = page["ib"]

    location_rows = sorted(
        locations.values(), key=lambda row: (row["taxonomy"], row["name"])
    )

    summaries, requirement_rows = _fetch(http, groups, location_rows, ib_payloads)

    for group in groups.values():
        identity = (group["campus"], group["requirement_key"])
        answered = [
            row for row in summaries
            if (row["campus"], row["requirement_key"]) == identity and row["available"]
        ]
        group["programs"] = sorted(group["programs"])
        group["locations"] = len(answered)
        # UBC only names the group inside the answers, and every answer for a
        # group names it the same way, so take it from the first one that came back.
        group["name"] = next((row["specific_name"] for row in answered), None)

    base = "requirements"
    _describe(out, base)
    out.table(f"{base}/program_requirements", program_rows, source=ENDPOINT)
    out.table(f"{base}/requirement_groups", sorted(
        groups.values(), key=lambda row: -row["program_count"]), source=ENDPOINT)
    out.table(f"{base}/locations", location_rows + [{"taxonomy": IB_TAXONOMY, **IB_LOCATION}],
              source=ENDPOINT)
    out.table(f"{base}/by_location", summaries, source=ENDPOINT)
    out.table(f"{base}/required_courses", requirement_rows, source=ENDPOINT)


def _describe(out: Output, base: str) -> None:
    """Say what each table is, because the shape is a two-hop join.

    program -> requirement_key -> (key x location) is not guessable from the
    filenames, and getting it wrong means quietly reading Arts requirements for
    an Engineering applicant.
    """
    out.describe(
        f"{base}/program_requirements",
        grain="one undergraduate program in the program finder",
        columns={
            "program_id": "you.ubc.ca post id; joins to admissions/programs.json `id`",
            "program": "program name as the finder lists it",
            "degrees": "degree names this program leads to",
            "url": "the program page",
            "campus": "campus the program page declares",
            "requirement_key": "the requirement group this program uses; null if it has none",
            "has_requirements": "false for graduate-entry and second-degree programs "
                                "(Medicine, Law, Journalism...) which publish no direct-entry rules",
            "note": "why requirements are absent, when they are",
        },
        joins=[
            "requirement_key -> requirements/requirement_groups.requirement_key",
            "requirement_key -> requirements/by_location.requirement_key",
            "program_id -> admissions/programs.id",
        ],
    )
    out.describe(
        f"{base}/requirement_groups",
        grain="one set of degree-specific requirements, shared by every program that uses it",
        columns={
            "campus": "campus the group applies to",
            "requirement_key": "UBC's slug for the group, e.g. `engineering`",
            "name": "the name UBC shows, e.g. `Applied Science (Engineering)`",
            "programs": "program names mapped to this group",
            "program_count": "how many programs that is",
            "locations": "how many curriculum locations returned rules for this group",
        },
        joins=["(campus, requirement_key) -> requirements/by_location"],
    )
    out.describe(
        f"{base}/locations",
        grain="one curriculum a student can apply from",
        columns={
            "taxonomy": "requirement_province, requirement_countries or requirement_ib",
            "term_id": "UBC's term id; null for the IB row, which is not a taxonomy term",
            "slug": "stable identifier, e.g. `british-columbia`; `basic` is the fallback "
                    "UBC applies when a location is not listed separately. NOT unique on "
                    "its own -- `basic` exists in both the province and country "
                    "taxonomies, so key on (taxonomy, slug) or on term_id",
            "name": "display name",
        },
        joins=[
            "(taxonomy, slug) -> requirements/by_location.(curriculum, location_slug), "
            "with `requirement_` stripped from the taxonomy name",
            "term_id -> requirements/by_location.location_term_id",
        ],
    )
    # The five columns every requirement row is identified by. They are the ones
    # a query filters on, so both tables spell them out rather than leaving them
    # implied by the join list.
    identity = {
        "campus": "campus the rules apply to",
        "requirement_key": "the requirement group, e.g. `engineering`",
        "curriculum": "province, countries or ib -- which dropdown the location came from",
        "location": "the province, country or IB, as UBC names it",
        "location_slug": "stable identifier for the location, e.g. `british-columbia`. "
                         "Pair it with `curriculum` -- `basic` is a slug in both the "
                         "province and the country list and means a different thing in each",
        "location_term_id": "UBC's term id for the location; unique on its own, "
                            "null only for the IB row",
    }
    out.describe(
        f"{base}/by_location",
        grain="one requirement group as it applies to one curriculum location",
        columns={
            **identity,
            "available": "false when the program is not open to students from there",
            "specific_name": "the degree the rules are stated for",
            "english": "English-language competency rule, as prose",
            "general": "general admission rule (graduation, minimum English grade...)",
            "minimum": "degree-specific rule where UBC states no grade bands",
            "grade_12": "required Grade 12 (or equivalent senior) courses",
            "grade_11": "required Grade 11 courses",
            "recommended_grade_range": "the grade range UBC says is competitive. The field "
                                       "exists upstream but came back empty on all 2,520 "
                                       "combinations, so expect null",
            "suggested_subjects": "subject areas UBC recommends but does not require",
            "profile_note": "extra note UBC attaches to this location",
        },
        joins=[
            "(campus, requirement_key) -> requirements/requirement_groups",
            "(curriculum, location_slug) -> requirements/locations.(taxonomy, slug)",
            "location_term_id -> requirements/locations.term_id",
        ],
    )
    out.describe(
        f"{base}/required_courses",
        grain="one requirement line -- the exploded form of by_location, and the table "
              "to query when the question is 'what do I need to have taken?'",
        columns={
            **identity,
            "program_group": "the degree the requirement is stated for",
            "kind": "grade_12, grade_11, minimum, general, english_language, "
                    "suggested_subjects or recommended_grade_range",
            "position": "order within its kind, as UBC lists it",
            "requirement": "the requirement itself, e.g. `Pre-Calculus 12`",
            "advisory": "true for suggestions and grade ranges; false for hard requirements",
        },
        joins=[
            "(campus, requirement_key) -> requirements/requirement_groups",
            "(curriculum, location_slug) -> requirements/locations.(taxonomy, slug)",
            "location_term_id -> requirements/locations.term_id",
        ],
    )


def _read_program(http: Http, program: dict[str, Any]) -> dict[str, Any]:
    """One program page: its requirement key, its dropdowns and its IB tab."""
    link = program.get("link")
    if not link:
        return {"error": "program has no link"}
    try:
        html = http.get(link).text
    except Exception as error:
        return {"error": f"{type(error).__name__}: {error}"}
    return {
        "settings": _settings(html),
        "locations": _locations(html),
        "ib": _ib_sections(html),
    }


def _fetch(
    http: Http,
    groups: dict[tuple[str, str], dict[str, Any]],
    locations: list[dict[str, Any]],
    ib_payloads: dict[tuple[str, str], dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every (requirement group x location) pair, plus the free IB rows."""
    jobs = [(campus, key, location) for campus, key in groups for location in locations]

    def ask(job: tuple[str, str, dict[str, Any]]) -> dict[str, Any] | None:
        campus, key, location = job
        try:
            return http.post_json(ENDPOINT, {
                "action": "requirements_load_programs",
                "type": location["taxonomy"],
                "location": location["term_id"],
                "campus": campus,
                "requirement": key,
            })
        except Exception:
            return None

    summaries: list[dict[str, Any]] = []
    requirements: list[dict[str, Any]] = []

    for (campus, key, location), payload in zip(jobs, http.map(ask, jobs)):
        if payload is None:
            summary, lines = _summary(note="request failed"), []
        elif not payload.get("result"):
            summary, lines = _summary(), []
        else:
            summary, lines = _rows_from_ajax(payload)
        _record(summaries, requirements, campus, key, location, summary, lines)

    for (campus, key), payload in ib_payloads.items():
        summary, lines = _rows_from_ib(payload)
        _record(summaries, requirements, campus, key,
                {"taxonomy": IB_TAXONOMY, **IB_LOCATION}, summary, lines)

    return summaries, requirements


def _record(
    summaries: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    campus: str,
    key: str,
    location: dict[str, Any],
    summary: dict[str, Any],
    lines: list[dict[str, Any]],
) -> None:
    identity = {
        "campus": campus,
        "requirement_key": key,
        "curriculum": location["taxonomy"].removeprefix("requirement_"),
        "location": location["name"],
        "location_slug": location["slug"],
        "location_term_id": location["term_id"],
    }
    summaries.append({**identity, **summary})
    for line in lines:
        requirements.append({
            **identity,
            "program_group": summary.get("specific_name"),
            **line,
            "advisory": line["kind"] in ADVISORY,
        })
