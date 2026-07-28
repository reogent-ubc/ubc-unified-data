"""Pull the structured bits out of a course description.

UBC ships course detail as one prose blob. The Academic Calendar *has* dedicated
`field_course_prerequisite`, `field_course_co_requistite`, `field_course_vector`
and `field_course_equivalency` fields, but they are empty on every record we
sampled -- UBC writes all of it into the description instead, exactly as its own
rendered pages show it:

    Physical and mathematical structures of computation. Boolean algebra and
    ... sequential instruction execution. [3-2-1] Prerequisite: Principles of
    Mathematics 12 or Pre-calculus 12. Corequisite: One of CPSC 107, CPSC 110.

So we parse it back out. These fields are *derived*, not fetched -- the original
text is always kept alongside so you can re-parse or check the extraction.
"""

from __future__ import annotations

import html
import re
from typing import Any

# "Prerequisite:" / "Pre-requisites:" / "Corequisite:" / "Equivalency:"
LABEL_RE = re.compile(
    r"(?<![A-Za-z-])(?P<label>pre-?requisite[s]?|co-?requisite[s]?|equivalency|equivalent)\s*:",
    re.I,
)

# Contact-hours vector, e.g. [3-2-1] or [3-0-0*] or [2.5-0-0]
VECTOR_RE = re.compile(r"\[\s*(\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?\*?){1,3})\s*\]")

# "Credit will be granted for only one of X, Y or Z."
EXCLUSION_RE = re.compile(r"credit will (?:be granted|only be granted)[^.]*\.", re.I)

TAG_RE = re.compile(r"<[^>]+>")

CANONICAL = {"pre": "prerequisite", "co": "corequisite", "eq": "equivalency"}


def _canonical(label: str) -> str:
    label = label.lower()
    if label.startswith("pre"):
        return CANONICAL["pre"]
    if label.startswith("co"):
        return CANONICAL["co"]
    return CANONICAL["eq"]


def plain_text(value: Any) -> str:
    """Drupal text fields arrive as {'value': ..., 'processed': ...} or a string."""
    if isinstance(value, dict):
        value = value.get("processed") or value.get("value") or ""
    if not isinstance(value, str):
        return ""
    text = TAG_RE.sub(" ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).replace(" ", " ").strip()


def parse_course_text(value: Any) -> dict[str, Any]:
    """Split a course description into description + prereq/coreq/vector/exclusion.

    Returns empty strings for anything not present, so the columns stay stable
    across every row.
    """
    text = plain_text(value)
    result: dict[str, Any] = {
        "description_text": "",
        "prerequisite": "",
        "corequisite": "",
        "equivalency": "",
        "credit_exclusion": "",
        "hours_vector": "",
    }
    if not text:
        return result

    vector = VECTOR_RE.search(text)
    if vector:
        result["hours_vector"] = re.sub(r"\s+", "", vector.group(1))

    exclusion = EXCLUSION_RE.search(text)
    if exclusion:
        result["credit_exclusion"] = exclusion.group(0).strip()

    labels = list(LABEL_RE.finditer(text))
    for index, match in enumerate(labels):
        end = labels[index + 1].start() if index + 1 < len(labels) else len(text)
        chunk = text[match.end() : end].strip(" .;")
        key = _canonical(match.group("label"))
        # Keep the first occurrence; later duplicates are usually cross-references.
        if chunk and not result[key]:
            result[key] = chunk + ("." if not chunk.endswith(".") else "")

    # The description is whatever comes before the first structured clause.
    cut = len(text)
    if labels:
        cut = min(cut, labels[0].start())
    if exclusion:
        cut = min(cut, exclusion.start())
    description = text[:cut]
    if vector and vector.start() < cut:
        description = description.replace(vector.group(0), " ")
    result["description_text"] = re.sub(r"\s+", " ", description).strip(" .;") or ""
    if result["description_text"]:
        result["description_text"] += "."

    return result


def enrich(records: list[dict[str, Any]], source_field: str) -> int:
    """Add the derived columns to every record, in place. Returns rows parsed."""
    parsed = 0
    for record in records:
        fields = parse_course_text(record.get(source_field))
        record.update(fields)
        if fields["prerequisite"] or fields["corequisite"] or fields["hours_vector"]:
            parsed += 1
    return parsed
