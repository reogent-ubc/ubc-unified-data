"""Academic Calendar: the official course catalogue and academic structure.

Source: the Drupal JSON:API behind vancouver.calendar.ubc.ca and
okanagan.calendar.ubc.ca. This is the catalogue rather than the schedule -- what
a course *is*, not when it runs. Pair it with the `courses` collector for both.

Caveat worth knowing: the dedicated `field_course_prerequisite`,
`field_course_co_requistite`, `field_course_vector` and
`field_course_equivalency` fields exist in the schema but are empty on every
record sampled. UBC writes all of that into `field_course_description` as prose,
which is also how its own pages render it. `coursetext.parse_course_text` splits
it back out into `prerequisite`, `corequisite`, `equivalency`,
`credit_exclusion` and `hours_vector` columns; the raw description is kept.

The calendar is more than courses, though. Everything else it publishes --
degree and specialization requirements, faculty and school overviews, admission
rules, academic regulations, the dates-and-deadlines tables -- is a `ubc_page`
node with a body of HTML. Those come down here too, and `calendarpages` works
out what each one is and where it sits in the hierarchy.
"""

from __future__ import annotations

from typing import Any

from . import calendarpages
from .base import Collector, Http, Output, jsonapi_collection, jsonapi_index, register, wants
from .coursetext import enrich

CAMPUSES = {
    "vancouver": "vancouver.calendar.ubc.ca",
    "okanagan": "okanagan.calendar.ubc.ca",
}

RESOURCES = {
    "courses": "node/course",
    "academic_years": "node/academic_year",
    "pages": "node/ubc_page",
    "landing_pages": "node/ubc_landing_page",
    "faculties": "taxonomy_term/faculty",
    "schools": "taxonomy_term/school",
    "departments": "taxonomy_term/department",
    "departments_or_units": "taxonomy_term/department_or_unit",
    "subjects": "taxonomy_term/subject",
    "course_codes": "taxonomy_term/course_code",
    "academic_year_terms": "taxonomy_term/academic_year",
}

# Both node types are calendar prose hung off a path alias, and a landing page
# is often the parent of an ordinary one, so they are enriched as one pool --
# otherwise half the hierarchy would have gaps where the parent isn't in scope.
PAGE_DATASETS = ("pages", "landing_pages")


def _subject_codes(fetched: dict[str, list[dict[str, Any]]]) -> set[str] | None:
    """This campus's subject codes, e.g. {"CPSC", "MATH"}.

    The subject vocabulary names them with a campus suffix ("AANB_V"). Holding
    the real list is what lets the page parser tell a course reference from a
    word that merely looks like one. `None` when the vocabulary didn't load, so
    the parser falls back to its own heuristic rather than rejecting everything.
    """
    codes = {
        (record.get("name") or "").split("_", 1)[0].strip()
        for record in fetched.get("subjects", ())
    }
    codes.discard("")
    return codes or None


@register
class AcademicCalendar(Collector):
    name = "calendar"
    folder = "academic-calendar"
    title = "Academic Calendar (course catalogue, programs, faculties, deadlines)"
    description = (
        "Official course catalogue entries with descriptions and credit values, plus "
        "prerequisites, corequisites, equivalencies, credit exclusions and hours "
        "vectors parsed out of the description prose; every calendar page with its "
        "body text, place in the hierarchy and the courses it references; the degree "
        "and specialization requirement pages on their own; the term dates and "
        "drop deadlines; and the faculty, school, department and subject hierarchy "
        "for both campuses."
    )
    sources = tuple(f"https://{host}/jsonapi" for host in CAMPUSES.values())

    def collect(self, http: Http, out: Output) -> None:
        unavailable: dict[str, list[str]] = {}

        for campus, host in CAMPUSES.items():
            if not wants(campus):
                continue
            _describe(out, campus)
            available = jsonapi_index(http, host)
            missing: list[str] = []
            fetched: dict[str, list[dict[str, Any]]] = {}

            for dataset, resource in RESOURCES.items():
                if resource.replace("/", "--") not in available:
                    missing.append(resource)
                    continue
                records = jsonapi_collection(http, host, resource)
                if not records:
                    continue
                for record in records:
                    record["campus"] = campus
                fetched[dataset] = records

            if "courses" in fetched:
                # UBC leaves field_course_prerequisite empty and writes the
                # prerequisites, corequisites and hours vector into the
                # description prose instead; recover them into columns.
                enrich(fetched["courses"], "field_course_description")

            pages = [record for name in PAGE_DATASETS for record in fetched.get(name, ())]
            if pages:
                calendarpages.enrich(pages, host=host, subjects=_subject_codes(fetched))

            for dataset, records in fetched.items():
                out.table(
                    f"{campus}/{dataset}",
                    records,
                    source=f"https://{host}/jsonapi/{RESOURCES[dataset]}",
                )

            self._derived(out, pages, campus=campus, host=host)

            if missing:
                unavailable[campus] = missing

        if unavailable:
            out.json("_unavailable.json", unavailable)

    def _derived(
        self,
        out: Output,
        pages: list[dict[str, Any]],
        *,
        campus: str,
        host: str,
    ) -> None:
        """Two cuts of the page pool that are worth having as tables of their own.

        Both are subsets or reshapes of `pages`, not extra requests -- `programs`
        is the pages that sit inside a degree, and `dates` is the deadline
        tables parsed out of the page that holds them.
        """
        if not pages:
            return

        programs = [page for page in pages if page.get("program")]
        if programs:
            out.table(
                f"{campus}/programs",
                programs,
                source=f"https://{host}/{calendarpages.FACULTIES_SECTION}",
            )

        for page in calendarpages.dates_pages(pages):
            rows = calendarpages.parse_dates(page)
            if rows:
                out.table(f"{campus}/dates", rows, source=page.get("url") or f"https://{host}")
                break


def _describe(out: Output, campus: str) -> None:
    """The calendar's three most-queried tables.

    `pages` is the one worth explaining: it is the whole calendar site as flat
    nodes, and everything that makes it navigable -- faculty, program, kind,
    breadcrumbs -- is recovered from the path alias rather than published.
    """
    out.describe(
        f"{campus}/courses",
        grain="one course in the catalogue -- what a course IS, not when it runs. "
              "Join to courses/sections for offerings",
        columns={
            "field_course_title": "course title",
            "field_course_number": "the number, e.g. `110`",
            "field_course_credit": "credit value",
            "field_course_description": "the description, with requirement prose inside it",
            "description_text": "derived: the description with that prose removed",
            "prerequisite": "derived from the description; UBC's own field is empty on every row",
            "corequisite": "derived from the description",
            "equivalency": "derived from the description",
            "credit_exclusion": "derived from the description",
            "hours_vector": "derived, e.g. `[3-2-1]` -- lecture/lab/tutorial hours",
            "alias": "path on the calendar host; prefix the host for the URL",
        },
        joins=["related.subject -> academic-calendar/*/subjects.id"],
    )
    out.describe(
        f"{campus}/pages",
        grain="one calendar page -- degree requirements, faculty overviews, admission "
              "rules, regulations. Everything the calendar publishes that is not a course",
        columns={
            "title": "page title",
            "kind": "derived: degree_requirements, major, minor, admission, regulations, "
                    "coop, advising, faculty_overview... what the page is",
            "level": "derived: undergraduate, masters, doctoral or certificate",
            "faculty": "derived from the alias chain",
            "program": "derived: the degree this page sits inside, if any",
            "breadcrumbs": "derived: the ancestor titles, recovered by matching alias prefixes",
            "parent_url": "the page above this one",
            "referenced_courses": "course codes the page mentions, in order, validated "
                                  "against this campus's subject vocabulary",
            "text": "body as plain text",
            "headings": "the page's heading outline",
            "body": "body HTML",
            "url": "the public URL",
        },
        joins=[
            "referenced_courses ~ academic-calendar/*/courses (subject + number)",
            "parent_url -> academic-calendar/*/pages.url",
        ],
    )
    out.describe(
        f"{campus}/dates",
        grain="one dated event -- a term boundary, exam period or drop/withdrawal deadline",
        columns={
            "event": "what happens, e.g. `Start`, `Last day to withdraw`",
            "applies_to": "which term or session it applies to",
            "start": "ISO date",
            "end": "ISO date; set only for multi-day windows",
            "span_days": "length of the window; null for single-day deadlines",
            "table": "the table on the page it came from",
        },
        joins=[],
    )
