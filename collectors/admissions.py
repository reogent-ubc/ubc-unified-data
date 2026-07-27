"""Admissions: the undergraduate degree/program finder and its supporting pages.

Source: you.ubc.ca. The program finder is a client-side app whose full dataset
is inlined into /programs/ as `var programs = [...]` and `var degrees = {...}`,
so we parse those literals rather than driving the search UI. Admission
requirement, cost and deadline content lives in the WP REST pages collection.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .base import Collector, Http, Output, register, wp_collection

HOST = "you.ubc.ca"
PROGRAMS_URL = f"https://{HOST}/programs/"

VAR_RE = re.compile(r"\bvar\s+([A-Za-z_$][\w$]*)\s*=\s*[\[{]")

# Front-end plumbing that happens to be declared the same way as the data.
IGNORE_VARS = re.compile(
    r"(Loc|Settings|Bundle|Vars|Strings)$|recaptcha|ajax|dataLayer|analytics|shim|nonce",
    re.I,
)

TAXONOMIES = ("program-areas", "student-groups", "ubc-life-topics", "cycle-stages", "content-types")


def _js_literal(html: str, name: str) -> Any:
    """Read one `var <name> = [...]/{...}` literal out of a page.

    WordPress emits these with json_encode, so the body is valid JSON once the
    matching bracket is found. Scanning respects string literals and escapes.
    """
    match = re.search(rf"\bvar\s+{re.escape(name)}\s*=\s*", html)
    if not match:
        raise KeyError(name)

    start = match.end()
    while start < len(html) and html[start].isspace():
        start += 1
    opener = html[start]
    closer = {"[": "]", "{": "}"}.get(opener)
    if closer is None:
        raise ValueError(f"{name} is not an array or object literal")

    depth = 0
    in_string = False
    escaped = False
    quote = ""

    for index in range(start, len(html)):
        char = html[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue
        if char in "\"'":
            in_string = True
            quote = char
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return json.loads(html[start : index + 1])

    raise ValueError(f"unterminated literal for {name}")


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
    sources = (PROGRAMS_URL, f"https://{HOST}/wp-json/wp/v2/pages")

    def collect(self, http: Http, out: Output) -> None:
        html = http.get(PROGRAMS_URL).text

        found: list[str] = []
        for name in dict.fromkeys(VAR_RE.findall(html)):
            if IGNORE_VARS.search(name):
                continue
            try:
                rows = _as_rows(_js_literal(html, name))
            except (KeyError, ValueError, json.JSONDecodeError):
                continue
            if not rows:
                continue
            out.table(name, rows, source=PROGRAMS_URL)
            found.append(name)

        if "programs" not in found:
            raise RuntimeError("no `programs` dataset found on the program finder page")

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
