"""Admissions: the undergraduate degree/program finder and its supporting pages.

Source: you.ubc.ca. The program finder is a client-side app whose full dataset
is inlined into /programs/ as `var programs = [...]` and `var degrees = {...}`,
so we parse those literals rather than driving the search UI. Admission
requirement, cost and deadline content lives in the WP REST pages collection.

The requirements themselves are a separate exercise -- see `admissionreqs`.
What a given program demands of you depends on where you went to school, so
they are not on the program page at all: the page ships a dropdown of provinces
and countries and fetches the matching rules over AJAX. That module drives the
same endpoint and lands the answers as tables.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import admissionreqs
from .base import Collector, Http, Output, js_literal, register, wants, wp_collection

HOST = "you.ubc.ca"
PROGRAMS_URL = f"https://{HOST}/programs/"

VAR_RE = re.compile(r"\bvar\s+([A-Za-z_$][\w$]*)\s*=\s*[\[{]")

# Front-end plumbing that happens to be declared the same way as the data.
IGNORE_VARS = re.compile(
    r"(Loc|Settings|Bundle|Vars|Strings)$|recaptcha|ajax|dataLayer|analytics|shim|nonce",
    re.I,
)

TAXONOMIES = ("program-areas", "student-groups", "ubc-life-topics", "cycle-stages", "content-types")

# Written as a nested object rather than a row per program, so flattening it to
# rows the way every other literal here is flattened produces four unlabelled
# blobs. `finances` reshapes it properly; keep the original next to that.
NESTED_VARS = {"costEstimatorData"}


def campus_ids(campuses: list[dict[str, Any]]) -> set[str]:
    """The campus term ids in scope (9 = Vancouver, 10 = Okanagan)."""
    return {
        str(term.get("term_id"))
        for term in campuses
        if wants(str(term.get("name", "")).strip().lower() or None)
    }


def on_campus(programs: list[dict[str, Any]], wanted_ids: set[str]) -> list[dict[str, Any]]:
    """Programs offered on a campus in scope; one offered on both is kept."""
    if not wanted_ids:
        return programs
    kept = []
    for program in programs:
        ids = program.get("campuses")
        ids = ids if isinstance(ids, list) else ([ids] if ids else [])
        if not ids or any(str(i) in wanted_ids for i in ids):
            kept.append(program)
    return kept


def _filter_by_campus(datasets: dict[str, list[dict[str, Any]]]) -> None:
    """Drop programs not offered on the selected campus.

    Programs reference campuses by term id, so the ids are resolved against the
    `campuses` list the page ships alongside.
    """
    campuses = datasets.get("campuses") or []
    wanted_ids = campus_ids(campuses)
    if not wanted_ids:
        return

    datasets["campuses"] = [c for c in campuses if str(c.get("term_id")) in wanted_ids]
    datasets["programs"] = on_campus(datasets.get("programs") or [], wanted_ids)


def _as_rows(value: Any) -> list[dict[str, Any]]:
    """Normalise either a list or an index-keyed object into a list of rows."""
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        return [row for row in value.values() if isinstance(row, dict)]
    return []


@register
class Admissions(Collector):
    name = "admissions"
    title = "Admissions: degree and program finder"
    description = (
        "Every undergraduate program in UBC's program finder with its degree, campus, "
        "faculty, areas of interest and links, the degree list, and the you.ubc.ca "
        "content pages covering admission requirements, costs, deadlines and how to apply."
    )
    sources = (PROGRAMS_URL, f"https://{HOST}/wp-json/wp/v2/pages", admissionreqs.ENDPOINT)

    def collect(self, http: Http, out: Output) -> None:
        html = http.get(PROGRAMS_URL).text

        datasets: dict[str, list[dict[str, Any]]] = {}
        for name in dict.fromkeys(VAR_RE.findall(html)):
            if IGNORE_VARS.search(name) or name in NESTED_VARS:
                continue
            try:
                rows = _as_rows(js_literal(html, name))
            except (KeyError, ValueError, json.JSONDecodeError):
                continue
            if rows:
                datasets[name] = rows

        if "programs" not in datasets:
            raise RuntimeError("no `programs` dataset found on the program finder page")

        _filter_by_campus(datasets)

        out.describe(
            "programs",
            grain="one undergraduate program in the program finder",
            columns={
                "id": "you.ubc.ca post id -- the key everything else here joins on",
                "post_title": "program name",
                "link": "the program page",
                "summary": "one-paragraph description",
                "degrees": "degree term ids; resolve against degrees.json",
                "campuses": "campus term ids (9 = Vancouver, 10 = Okanagan)",
                "interests": "interest term ids; resolve against interests.json",
                "topics": "topic term ids; resolve against topics.json",
                "duration": "nominal length, e.g. {amount: 4, unit: years}",
            },
            joins=[
                "id -> admissions/requirements/program_requirements.program_id",
                "id -> finances/program_cost_estimates.program_id",
                "degrees -> admissions/degrees.term_id",
                "campuses -> admissions/campuses.term_id",
            ],
        )
        for name, rows in datasets.items():
            out.table(name, rows, source=PROGRAMS_URL)

        pages = wp_collection(http, HOST, "wp/v2/pages", params={"_embed": "0"})
        out.table("pages", pages, source=f"https://{HOST}/wp-json/wp/v2/pages")

        for taxonomy in TAXONOMIES:
            try:
                terms = wp_collection(http, HOST, f"wp/v2/{taxonomy}")
            except Exception:
                continue
            if terms:
                out.table(f"taxonomies/{taxonomy}", terms,
                          source=f"https://{HOST}/wp-json/wp/v2/{taxonomy}")

        degrees = {
            str(term.get("term_id")): term.get("name")
            for term in datasets.get("degrees", ())
        }
        admissionreqs.collect(http, out, programs=datasets["programs"], degrees=degrees)
