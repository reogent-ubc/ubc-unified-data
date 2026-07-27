"""Academic Calendar: the official course catalogue and academic structure.

Source: the Drupal JSON:API behind vancouver.calendar.ubc.ca and
okanagan.calendar.ubc.ca. This is the catalogue rather than the schedule -- it
carries course descriptions, credit values, prerequisites, corequisites,
equivalencies and the hours vector, plus the faculty / school / department
hierarchy. Pair it with the `courses` collector to know both what a course *is*
and when it is actually offered.
"""

from __future__ import annotations

from .base import Collector, Http, Output, jsonapi_collection, jsonapi_index, register

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
        "Official course catalogue entries with descriptions, credits, prerequisites, "
        "corequisites, equivalencies and hours vectors, plus the faculty, school, "
        "department and subject hierarchy for both campuses."
    )
    sources = tuple(f"https://{host}/jsonapi" for host in CAMPUSES.values())

    def collect(self, http: Http, out: Output) -> None:
        unavailable: dict[str, list[str]] = {}

        for campus, host in CAMPUSES.items():
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
                out.table(f"{campus}/{dataset}", records, source=f"https://{host}/jsonapi/{resource}")

            if missing:
                unavailable[campus] = missing

        if unavailable:
            out.json("_unavailable.json", unavailable)
