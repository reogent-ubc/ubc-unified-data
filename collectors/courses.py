"""Course Schedule: every course, every scheduled section, and the code lists.

Source: the Drupal JSON:API behind courses.students.ubc.ca (UBC's Workday-era
Course Schedule site). Sections carry days, start/end times, dates, instructors,
credits, delivery mode and status; courses carry credits, subject and course
number. Taxonomy files resolve the ids that courses and sections point at.
"""

from __future__ import annotations

from .base import Collector, Http, Output, jsonapi_collection, jsonapi_index, register

HOST = "courses.students.ubc.ca"

# dataset name -> JSON:API resource path
RESOURCES = {
    "courses": "node/course",
    "sections": "node/section",
    "standard_timetables": "node/standard_timetable",
    "subjects": "taxonomy_term/subject",
    "campuses": "taxonomy_term/campus",
    "terms": "taxonomy_term/term",
    "years": "taxonomy_term/year",
    "delivery_modes": "taxonomy_term/delivery_mode",
    "instructional_methods": "taxonomy_term/instructional_method",
    "departments": "taxonomy_term/department_or_unit",
    "program_areas": "taxonomy_term/program_area",
    "statuses": "taxonomy_term/status",
}


@register
class Courses(Collector):
    name = "courses"
    title = "Course Schedule (courses, sections, instructors, meeting times)"
    description = (
        "Every course and scheduled section published on UBC's Course Schedule, "
        "including meeting days and times, start/end dates, instructors, credits, "
        "delivery mode, instructional method and section status, plus the subject, "
        "campus, term, year and department code lists they reference."
    )
    sources = (f"https://{HOST}/jsonapi",)

    def collect(self, http: Http, out: Output) -> None:
        available = jsonapi_index(http, HOST)
        missing: list[str] = []

        for dataset, resource in RESOURCES.items():
            if resource.replace("/", "--") not in available:
                missing.append(resource)
                continue
            records = jsonapi_collection(http, HOST, resource)
            out.table(dataset, records, source=f"https://{HOST}/jsonapi/{resource}")

        if missing:
            out.json("_unavailable.json", missing)
