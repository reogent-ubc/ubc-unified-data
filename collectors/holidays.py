"""Statutory holidays, which UBC publishes from HR rather than the calendar.

There is no API and no feed for these -- hr.ubc.ca renders them as a page, and
the page is a year heading, then a month heading, then a two-column table for
each month that has one:

    <h2>2026 statutory holidays</h2>
    <h4>JANUARY</h4>
    <table><tr><td>Thursday, January 1</td><td>New Year's Day</td></tr></table>
    <h4>MARCH</h4>
    <p><em>N/A</em></p>

The year comes from the last heading that carried one; month and day come from
the date cell itself, which names its month and occasionally its year too. The
month headings are only navigation -- a row already says "January 1" -- so they
are read past. Months with no holiday have no table, which needs no handling:
there is simply nothing to read.

Two of these are UBC's own rather than BC's (Easter Monday and Boxing Day, both
marked with an asterisk upstream). The asterisk is stripped from the name and
recorded as `ubc_specific` instead of being thrown away.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from . import htmldoc
from .base import Http

URL = "https://hr.ubc.ca/working-ubc/statutory-holidays"

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

WEEKDAYS = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)

# "Thursday, January 1" / "Monday, December 28, 2026"
DATE_RE = re.compile(
    rf"(?:(?P<weekday>[A-Za-z]+day),\s*)?"
    rf"(?P<month>{'|'.join(MONTHS)})\s+(?P<day>\d{{1,2}})"
    rf"(?:,\s*(?P<year>\d{{4}}))?",
    re.IGNORECASE,
)

YEAR_RE = re.compile(r"\b(20\d{2})\b")


def parse(html: str) -> list[dict[str, Any]]:
    """Read the holiday tables into one row per holiday."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    year: int | None = None

    for block in htmldoc.blocks(html):
        if isinstance(block, htmldoc.Heading):
            # Only a heading that carries a year matters, and it stays in force
            # until another one replaces it -- the month headings and the
            # "Related resources" heading at the foot are both read past.
            found_year = YEAR_RE.search(block.text)
            if found_year:
                year = int(found_year.group(1))
            continue

        for row in block.rows:
            if len(row) < 2:
                continue
            date_text, name = row[0], row[1]
            if not date_text or not name:
                continue
            match = DATE_RE.search(date_text)
            if match is None:
                continue

            cell_year = int(match.group("year")) if match.group("year") else year
            if cell_year is None:
                continue
            cell_month = MONTHS[match.group("month").lower()]
            day = int(match.group("day"))
            try:
                observed = date(cell_year, cell_month, day)
            except ValueError:
                continue

            # The asterisk marks a holiday UBC observes beyond BC's statutory
            # list; keep the fact, drop it from the name.
            ubc_specific = "*" in name
            name = re.sub(r"\s*\*+\s*$", "", name).strip()
            key = (observed.isoformat(), name.lower())
            if not name or key in seen:
                continue
            seen.add(key)

            rows.append(
                {
                    "name": name,
                    "date": observed.isoformat(),
                    "weekday": WEEKDAYS[observed.weekday()],
                    "year": observed.year,
                    "month": observed.month,
                    "date_text": date_text,
                    "ubc_specific": ubc_specific,
                    "source_url": URL,
                }
            )

    rows.sort(key=lambda row: row["date"])
    return rows


def fetch(http: Http) -> list[dict[str, Any]]:
    return parse(http.get(URL).text)


fetch.source = URL  # type: ignore[attr-defined]
