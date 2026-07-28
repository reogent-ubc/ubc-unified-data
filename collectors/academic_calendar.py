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
"""

from __future__ import annotations

from .base import Collector, Http, Output, jsonapi_collection, jsonapi_index, register, wants
from .coursetext import enrich

CAMPUSES = {
    "vancouver": "vancouver.calendar.ubc.ca",
    "okanagan": "okanagan.calendar.ubc.ca",
}

RESOURCES = {
    "courses": "node/course",
    "academic_years": "node/academic_year",
    "faculties": "taxonomy_term/faculty",
    "schools": "taxonomy_term/school",
    "departments": "taxonomy_term/department",
    "departments_or_units": "taxonomy_term/department_or_unit",
    "subjects": "taxonomy_term/subject",
    "course_codes": "taxonomy_term/course_code",
    "academic_year_terms": "taxonomy_term/academic_year",
}


@register
class AcademicCalendar(Collector):
    name = "calendar"
    folder = "academic-calendar"
    title = "Academic Calendar (course catalogue, faculties, departments)"
    description = (
        "Official course catalogue entries with descriptions and credit values, plus "
        "prerequisites, corequisites, equivalencies, credit exclusions and hours "
        "vectors parsed out of the description prose, and the faculty, school, "
        "department and subject hierarchy for both campuses."
    )
    sources = tuple(f"https://{host}/jsonapi" for host in CAMPUSES.values())

    def collect(self, http: Http, out: Output) -> None:
        unavailable: dict[str, list[str]] = {}

        for campus, host in CAMPUSES.items():
            if not wants(campus):
                continue
            available = jsonapi_index(http, host)
            missing: list[str] = []

            for dataset, resource in RESOURCES.items():
                if resource.replace("/", "--") not in available:
                    missing.append(resource)
                    continue
                records = jsonapi_collection(http, host, resource)
                if not records:
                    continue
                for record in records:
                    record["campus"] = campus
                if dataset == "courses":
                    # UBC leaves field_course_prerequisite empty and writes the
                    # prerequisites, corequisites and hours vector into the
                    # description prose instead; recover them into columns.
                    enrich(records, "field_course_description")
                out.table(f"{campus}/{dataset}", records, source=f"https://{host}/jsonapi/{resource}")

            if missing:
                unavailable[campus] = missing

        if unavailable:
            out.json("_unavailable.json", unavailable)
