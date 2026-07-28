"""Course Schedule: every course, every scheduled section, and the code lists.

Source: the Drupal JSON:API behind courses.students.ubc.ca (UBC's Workday-era
Course Schedule site). Sections carry days, start/end times, dates, instructors,
credits, delivery mode and status; courses carry credits, subject and course
number. Taxonomy files resolve the ids that courses and sections point at.

The API serves both campuses from one collection with no campus field, so this
collector derives one -- from the `_V`/`_O` course-code suffix, the "(UBC-V)"
term label, the "(Vancouver)" subject label, or a standard timetable's campus
relationship -- writes it as a `campus` column, and then applies the campus
selection. Records with no campus marker are always kept.
"""

from __future__ import annotations

import re
from typing import Any

from .base import Collector, Http, Output, jsonapi_collection, jsonapi_index, register, wants
from .coursetext import enrich

HOST = "courses.students.ubc.ca"

# dataset name -> JSON:API resource path
RESOURCES = {
    "courses": "node/course",
    "sections": "node/section",
    "standard_timetables": "node/standard_timetable",
    "subjects": "taxonomy_term/subject",
    "campuses": "taxonomy_term/campus",
    "terms": "taxonomy_term/term",
    # Named `year` upstream but it holds year *level* (1-4), not academic year.
    "year_levels": "taxonomy_term/year",
    "delivery_modes": "taxonomy_term/delivery_mode",
    "instructional_methods": "taxonomy_term/instructional_method",
    "departments": "taxonomy_term/department_or_unit",
    "program_areas": "taxonomy_term/program_area",
    "statuses": "taxonomy_term/status",
}

# "(Okanagan)", "UBC-O", "UBCO", and the "_O" in a code like "STAT_O 205".
OKANAGAN_RE = re.compile(r"\(Okanagan\)|UBC[-_ ]?O\b|_O(?=\s|$)", re.I)
VANCOUVER_RE = re.compile(r"\(Vancouver\)|UBC[-_ ]?V\b|_V(?=\s|$)", re.I)


def campus_of(*values: Any) -> str | None:
    """Read a campus out of whatever labels a record carries, or None."""
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        if OKANAGAN_RE.search(value):
            return "okanagan"
        if VANCOUVER_RE.search(value):
            return "vancouver"
    return None


@register
class Courses(Collector):
    name = "courses"
    title = "Course Schedule (courses, sections, instructors, meeting times)"
    description = (
        "Every course and scheduled section published on UBC's Course Schedule, "
        "including meeting days and times, start/end dates, instructors, credits, "
        "delivery mode, instructional method and section status, plus the subject, "
        "campus, term and department code lists they reference. A derived `campus` "
        "column is added to courses and sections."
    )
    sources = (f"https://{HOST}/jsonapi",)

    def collect(self, http: Http, out: Output) -> None:
        available = jsonapi_index(http, HOST)
        missing: list[str] = []
        data: dict[str, list[dict[str, Any]]] = {}

        for dataset, resource in RESOURCES.items():
            if resource.replace("/", "--") not in available:
                missing.append(resource)
                continue
            data[dataset] = jsonapi_collection(http, HOST, resource)

        if data.get("courses"):
            # `body` carries the description with prerequisites written into
            # the prose; split them into their own columns.
            enrich(data["courses"], "body")

        self._tag_and_filter(data)

        for dataset, records in data.items():
            out.table(dataset, records, source=f"https://{HOST}/jsonapi/{RESOURCES[dataset]}")

        if missing:
            out.json("_unavailable.json", missing)

    def _tag_and_filter(self, data: dict[str, list[dict[str, Any]]]) -> None:
        """Add a `campus` column everywhere it can be derived, then apply the filter."""
        # Taxonomies that name their campus directly.
        campus_by_id: dict[str, str | None] = {}
        for dataset in ("campuses", "terms", "subjects"):
            for record in data.get(dataset, []):
                record["campus"] = campus_of(record.get("name"), record.get("description"))
                campus_by_id[record["id"]] = record["campus"]

        # Courses: the "_V"/"_O" in the course code is the reliable marker.
        course_campus: dict[str, str | None] = {}
        for record in data.get("courses", []):
            related = record.get("related") or {}
            record["campus"] = campus_of(
                record.get("field_course_code"), record.get("title")
            ) or campus_by_id.get(related.get("subject"))
            course_campus[record["id"]] = record["campus"]

        # Sections: inherit from the course, else from the academic term label.
        for record in data.get("sections", []):
            related = record.get("related") or {}
            record["campus"] = (
                course_campus.get(related.get("course"))
                or campus_by_id.get(related.get("academic_term"))
                or campus_of(record.get("title"))
            )

        # Standard timetables point straight at a campus taxonomy term.
        for record in data.get("standard_timetables", []):
            related = record.get("related") or {}
            record["campus"] = campus_by_id.get(related.get("stt_campus")) or campus_by_id.get(
                related.get("stt_term")
            )

        for dataset, records in data.items():
            if records and "campus" in records[0]:
                data[dataset] = [r for r in records if wants(r.get("campus"))]
