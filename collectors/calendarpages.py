"""Make the Academic Calendar's page nodes queryable.

`node--ubc_page` is where UBC keeps everything the calendar says that isn't a
course: degree and specialization requirements, faculty and school overviews,
admission rules, academic regulations, and the dates-and-deadlines tables. The
API hands back a title, a path alias and a blob of body HTML -- no hierarchy, no
breadcrumbs, no indication of what kind of page you are holding.

The alias is the hierarchy, though. It is what UBC's own breadcrumb trail is
rendered from:

    /faculties-colleges-and-schools
      /faculty-forestry-and-environmental-stewardship
        /bsc-degrees-students-who-started-prior-september-2024
          /bsf-bachelor-science-forestry
            /forest-resources-management-major

So ancestry is recovered by matching each alias prefix back to the page that
owns it, which yields real titles rather than de-slugified guesses -- "The
Faculty of Forestry and Environmental Stewardship", not
"faculty-forestry-and-environmental-stewardship". Everything else here (`kind`,
`level`, `faculty`, `program`, `referenced_courses`) is derived from that chain
plus the title.

Every derived column is *derived, not fetched*. The body HTML and the alias it
all came from are kept on the record, so any of it can be re-derived or
spot-checked.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable

from . import htmldoc
from .coursetext import plain_text

FACULTIES_SECTION = "faculties-colleges-and-schools"

# --------------------------------------------------------------------------
# Degree detection
# --------------------------------------------------------------------------

# UBC mixes long forms ("Bachelor of Science") with abbreviations ("M.Sc.",
# "Ph.D.", "MBA"), so a degree title is matched against both.
DEGREE_TITLE_RE = re.compile(
    r"^("
    r"Bachelor of\b|UBC Bachelor of\b|"
    r"Master of\b|UBC Master of\b|"
    r"Doctor of\b|"
    r"(?:Graduate|Undergraduate) Certificate\b|"
    r"Certificate in\b|Diploma in\b|"
    r"B\.[A-Z]+(?:\.[A-Z]+)*\.|"
    r"M\.[A-Z]+(?:\.[A-Z]+)*\.|"
    r"D\.[A-Z]+(?:\.[A-Z]+)*\.|"
    r"Ph\.?\s?D\.?|"
    r"MBA\b|MFA\b|MEd\b|MEng\b|MASc\b|MSc\b|MA\b|MArch\b"
    r")",
    re.IGNORECASE,
)

# "Data Science (M.D.S.)" -- the degree trails the program name instead of
# leading it, so a title-prefix match alone would miss the page.
TRAILING_ABBREV_RE = re.compile(
    r"\(\s*(B|M|D|Ph)\.?\s?[A-Z]+(?:\.\s?[A-Z]+)*\.?\s*\)\s*$", re.IGNORECASE
)

# Slug prefixes that mark a degree *root* page. Gating on these stops a child
# page ("honours", "admission") from being adopted as a degree of its own.
DEGREE_SLUG_PREFIXES = (
    "bachelor-", "basc-", "bsc-", "bsf-", "bils-", "b-i-l-s", "buf-", "b-u-f",
    "master-", "masters-", "mba-", "mfa-", "med-", "meng-", "masc-",
    "ma-", "msc-", "m-arch", "m-ed", "m-eng", "m-sc", "m-a", "m-asc",
    "doctor-", "doctoral-", "phd-", "ph-d", "d-m-a",
    "graduate-certificate-", "undergraduate-certificate-",
    "certificate-", "diploma-",
)

# The other UBC convention: program name first, degree abbreviation trailing
# ("data-science-mds"). Doubles as the level when the title doesn't say.
DEGREE_SLUG_SUFFIXES_BY_LEVEL: tuple[tuple[str, str], ...] = (
    ("-mds", "masters"), ("-msc", "masters"), ("-meng", "masters"),
    ("-masc", "masters"), ("-mba", "masters"), ("-mfa", "masters"),
    ("-med", "masters"), ("-march", "masters"), ("-mmm", "masters"),
    ("-mpp", "masters"), ("-mppga", "masters"), ("-mph", "masters"),
    ("-mhsc", "masters"), ("-mn", "masters"), ("-mlis", "masters"),
    ("-mhlp", "masters"), ("-mhrm", "masters"),
    ("-phd", "doctoral"), ("-dma", "doctoral"), ("-edd", "doctoral"),
)

LEVEL_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(bachelor|ubc bachelor|b\.[a-z])", re.I), "undergraduate"),
    (re.compile(r"^(undergraduate certificate)", re.I), "undergraduate"),
    (re.compile(r"^(master|ubc master|m\.[a-z]|mba|mfa|med|meng|masc|msc|ma|march)\b", re.I), "masters"),
    (re.compile(r"^(graduate certificate)", re.I), "masters"),
    (re.compile(r"^(doctor|ph\.?\s?d|d\.[a-z])", re.I), "doctoral"),
    (re.compile(r"^(certificate|diploma) ", re.I), "certificate"),
)

# What sort of page this is within a degree subtree. Most specific first.
KIND_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"degree-requirements?\b|graduation-requirements?\b"), "degree_requirements"),
    (re.compile(r"\bhonours\b"), "honours"),
    (re.compile(r"\bminor[s]?\b"), "minor"),
    (re.compile(r"\bmajor[s]?\b"), "major"),
    (re.compile(r"\bspecializ(ation|ed)\b"), "specialization"),
    (re.compile(r"\b(combined|dual)[-_]?degree\b"), "dual_degree"),
    (re.compile(r"co-?operative|co-?op"), "coop"),
    (re.compile(r"\badmission|transfer\b"), "admission"),
    (re.compile(r"academic-regulations|general-academic|promotion|standing"), "regulations"),
    (re.compile(r"thesis|comprehensive-exam|qualifying-exam|candidacy"), "regulations"),
    (re.compile(r"curriculum|first-year|year-one|second-year"), "curriculum"),
    (re.compile(r"advising|academic-information"), "advising"),
)

# Subject codes are 2-4 letters (AI, BA ... CPSC, WRDS), never 5+, followed by a
# three-digit number and an optional letter. The campus suffix is stripped.
COURSE_CODE_RE = re.compile(r"\b([A-Z]{2,4})(?:_[VO])?\s?(\d{3}[A-Z]?)\b")


def _slug(alias: str) -> str:
    return alias.rsplit("/", 1)[-1].lower()


def _degree(alias: str, title: str) -> tuple[bool, str]:
    """Is this page a degree root, and at what level?

    Both the slug and the title have to look like a degree. Either gate alone
    is too loose: "minor-statistics" lives under a Bachelor without being one,
    and a page titled "Honours" is a section of a degree rather than a degree.
    """
    slug = _slug(alias)
    prefix_hit = slug.startswith(DEGREE_SLUG_PREFIXES)
    suffix_level = next(
        (level for suffix, level in DEGREE_SLUG_SUFFIXES_BY_LEVEL if slug.endswith(suffix)),
        None,
    )
    if not prefix_hit and suffix_level is None:
        return False, ""
    if not title or (not DEGREE_TITLE_RE.match(title) and not TRAILING_ABBREV_RE.search(title)):
        return False, ""
    for pattern, level in LEVEL_RULES:
        if pattern.match(title):
            return True, level
    return True, suffix_level or "other"


def _kind(alias: str, title: str, *, is_degree: bool, depth: int, section: str) -> str:
    if is_degree:
        return "degree_overview"
    if depth == 2 and section == FACULTIES_SECTION:
        lowered = title.lower()
        if "school" in lowered:
            return "school_overview"
        if "college" in lowered:
            return "college_overview"
        return "faculty_overview"
    haystack = f"{alias} {title}".lower()
    for pattern, kind in KIND_RULES:
        if pattern.search(haystack):
            return kind
    return "other"


def course_codes(text: str, subjects: set[str] | None = None) -> list[str]:
    """Course codes mentioned in a page, in the order they appear.

    Order is worth keeping -- in a requirements table it is the order UBC lists
    the courses in. When the run has the campus's own subject list, codes are
    validated against it, which is what keeps English words out ("OR 100") while
    keeping the real two-letter subjects (AI, BA).
    """
    found: dict[str, None] = {}
    for subject, number in COURSE_CODE_RE.findall(text):
        if subjects is not None and subject not in subjects:
            continue
        if subjects is None and len(subject) < 3:
            continue
        found.setdefault(f"{subject} {number}", None)
    return list(found)


def enrich(
    pages: list[dict[str, Any]],
    *,
    host: str,
    subjects: set[str] | None = None,
) -> None:
    """Add the derived hierarchy and classification columns, in place."""
    # Alias -> page. First writer wins: aliases are unique in practice, and a
    # duplicate would be a redirect stub we'd rather not treat as the ancestor.
    by_alias: dict[str, dict[str, Any]] = {}
    for page in pages:
        alias = page.get("alias") or ""
        if alias and alias not in by_alias:
            by_alias[alias] = page

    # Degree roots first and separately: resolving a page's program means
    # asking its ancestors whether they are one, and they may not have been
    # visited yet.
    for page in pages:
        is_degree, level = _degree(page.get("alias") or "", (page.get("title") or "").strip())
        page["is_degree_root"] = is_degree
        page["level"] = level if is_degree else ""

    for page in pages:
        alias = page.get("alias") or ""
        title = (page.get("title") or "").strip()
        segments = [part for part in alias.split("/") if part]

        ancestors = [
            by_alias[candidate]
            for candidate in ("/" + "/".join(segments[:n]) for n in range(1, len(segments)))
            if candidate in by_alias
        ]

        # The nearest enclosing degree, which for a degree root is itself.
        program_page = page if page["is_degree_root"] else None
        if program_page is None:
            for ancestor in reversed(ancestors):
                if ancestor.get("is_degree_root"):
                    program_page = ancestor
                    break

        faculty = ""
        if segments and segments[0] == FACULTIES_SECTION and len(segments) >= 2:
            root = by_alias.get(f"/{segments[0]}/{segments[1]}")
            if root is not None:
                faculty = root.get("title") or ""

        text = plain_text(page.get("body"))
        parent = ancestors[-1] if ancestors else None

        page["url"] = f"https://{host}{alias}" if alias else ""
        page["section"] = segments[0] if segments else ""
        page["depth"] = len(segments)
        page["parent_title"] = (parent or {}).get("title") or ""
        page["parent_url"] = f"https://{host}{(parent or {}).get('alias')}" if parent else ""
        page["breadcrumbs"] = [ancestor.get("title") or "" for ancestor in ancestors]
        page["faculty"] = faculty
        page["program"] = (program_page or {}).get("title") or ""
        page["program_url"] = (
            f"https://{host}{program_page['alias']}" if program_page else ""
        )
        # A page inside a degree subtree inherits that degree's level.
        page["level"] = (program_page or {}).get("level") or ""
        page["kind"] = _kind(
            alias,
            title,
            is_degree=page["is_degree_root"],
            depth=len(segments),
            section=page["section"],
        )
        page["headings"] = [heading.text for heading in htmldoc.headings(_body_html(page))]
        page["text"] = text
        page["word_count"] = len(text.split())
        page["referenced_courses"] = course_codes(text, subjects)


def _body_html(page: dict[str, Any]) -> str:
    body = page.get("body")
    if isinstance(body, dict):
        return body.get("processed") or body.get("value") or ""
    return body if isinstance(body, str) else ""


# --------------------------------------------------------------------------
# Dates and deadlines
# --------------------------------------------------------------------------

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
    "|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)

# "Tuesday, May 12, 2026" / "May 12-August 14, 2026" / "May 12 to June 2, 2026"
#
# Days are matched three digits wide even though no day needs three, because
# UBC glues footnote markers straight onto the number ("November 9 - 111"). Two
# digits would fail to match the cell at all rather than mis-read the day;
# `_day` peels the marker back off.
DATE_RE = re.compile(
    rf"(?P<m1>{_MONTHS})\s+(?P<d1>\d{{1,3}})"
    rf"(?:\s*(?:[–—\-]|to)\s*(?:(?P<m2>{_MONTHS})\s+)?(?P<d2>\d{{1,3}}))?"
    rf",?\s+(?P<y>\d{{4}})",
    re.IGNORECASE,
)

MONTH_NUMBERS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# Footnote markers UBC renders as superscripts next to a date.
SUPERSCRIPTS = re.compile(r"[¹²³⁴⁵⁶⁷⁸⁹⁰*†‡]+")


def _year_in(text: str) -> int | None:
    match = re.search(r"(20\d{2})", text or "")
    return int(match.group(1)) if match else None


def _iso(year: int, month: int, day: int) -> str:
    return f"{year:04d}-{month:02d}-{day:02d}"


def _day(digits: str) -> int | None:
    """Read a day-of-month, peeling off a footnote marker glued to the number.

    UBC renders some footnotes as a bare digit appended with no separator, so
    "November 9 - 11" with footnote 1 arrives as "November 9 - 111". Nothing
    past 31 can be a day, so digits come off the right until one can.
    """
    day = int(digits)
    while day > 31 and len(digits) > 1:
        digits = digits[:-1]
        day = int(digits)
    return day if 1 <= day <= 31 else None


def _parse_date_cell(cell: str, fallback_year: int | None) -> tuple[str, str] | None:
    """Parse a date or date range out of one cell. Returns (start, end)."""
    cell = SUPERSCRIPTS.sub("", cell).strip()
    match = DATE_RE.search(cell)
    if match is None and fallback_year is not None:
        # Cells often omit the year because the column header carries it.
        match = DATE_RE.search(f"{cell}, {fallback_year}")
    if match is None:
        return None

    year = int(match.group("y"))
    month = MONTH_NUMBERS[match.group("m1").lower()]
    day = _day(match.group("d1"))
    if day is None:
        return None

    if not match.group("d2"):
        return _iso(year, month, day), ""

    end_month = MONTH_NUMBERS[(match.group("m2") or match.group("m1")).lower()]
    end_day = _day(match.group("d2"))
    if end_day is None:
        return _iso(year, month, day), ""
    # A range written "December 20 - January 3, 2027" states the year once, at
    # the end, and it belongs to the end -- the range started the year before.
    start_year = year - 1 if end_month < month else year
    return _iso(start_year, month, day), _iso(year, end_month, end_day)


def _span_days(start: str, end: str) -> int | None:
    if not end:
        return None
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return None


def parse_dates(page: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn the dates-and-deadlines page into one row per date UBC publishes.

    The page is a set of wide tables where a cell's meaning is the pair of
    labels crossing at it -- and the two families of table are transposed
    relative to each other:

        Term Dates          columns are terms ("Term 1 (September - December,
                            2026)"), rows are events ("Start", "Exams Finish")
        Drop/Withdrawal     columns are events ("Last day to drop without a W
                            standing"), rows are cohorts ("Term 1 Course")

    Which way round a table is falls out of the headers: only a term names a
    year. So `applies_to` and `event` come out consistently oriented either way,
    rather than meaning one thing in half the rows and the reverse in the rest.

    Every parsed cell is kept, multi-week windows included -- they are real
    dates UBC publishes, and `span_days` is there so a caller who wants only
    single-day deadlines can filter on it.
    """
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    outline: dict[int, str] = {}

    for block in htmldoc.blocks(_body_html(page)):
        if isinstance(block, htmldoc.Heading):
            outline = {level: text for level, text in outline.items() if level < block.level}
            outline[block.level] = block.text
            continue

        headers = block.headers or []
        if len(headers) < 2:
            continue
        section = outline[min(outline)] if outline else ""
        table_heading = outline[max(outline)] if outline else ""
        # A term states its year; a deadline description doesn't.
        columns_are_terms = any(_year_in(header) for header in headers[1:])

        for row in block.rows:
            if len(row) < 2 or not row[0]:
                continue
            for index, cell in enumerate(row[1:], start=1):
                if index >= len(headers) or not cell:
                    continue
                header = headers[index]
                applies_to, event = (header, row[0]) if columns_are_terms else (row[0], header)
                parsed = _parse_date_cell(
                    cell, _year_in(applies_to) or _year_in(table_heading)
                )
                if parsed is None:
                    continue
                start, end = parsed
                key = (applies_to, event, start)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "event": event,
                        "applies_to": applies_to,
                        "section": section,
                        "table": table_heading,
                        "date_text": cell,
                        "start": start,
                        "end": end,
                        "span_days": _span_days(start, end),
                        "source_url": page.get("url") or "",
                    }
                )

    rows.sort(key=lambda row: (row["start"], row["applies_to"], row["event"]))
    return rows


def dates_pages(pages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The calendar pages that hold the deadline tables, whatever they're called.

    Matched on the alias rather than hard-coded, so the Okanagan calendar's
    equivalent page is picked up without a second rule.
    """
    return [
        page
        for page in pages
        if "dates-and-deadlines" in (page.get("alias") or "")
        or "dates-deadlines" in (page.get("alias") or "")
    ]
