"""University-wide events, plus the venues and organizers behind them.

Source: the REST API of The Events Calendar on events.ubc.ca, which UBC
documents for developers at https://events.ubc.ca/resources/webdev/. Venues
carry street addresses but no coordinates, so joining events to a point on the
map means matching a venue address against the geospatial address data.
"""

from __future__ import annotations

from typing import Any

from .base import Collector, Http, Output, register

BASE = "https://events.ubc.ca/wp-json/tribe/events/v1"

# The events endpoint defaults to upcoming only; reach back for the archive too.
ARCHIVE_START = "2000-01-01"
MAX_PAGES = 400


def _collection(http: Http, key: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    url = f"{BASE}/{key}"
    records: list[dict[str, Any]] = []
    page = 1
    pages = 1

    while page <= min(pages, MAX_PAGES):
        payload = http.get_json(url, params={**(params or {}), "per_page": 50, "page": page})
        batch = payload.get(key)
        if batch is None:
            # Fall back to whichever key holds the list for this endpoint.
            batch = next((v for v in payload.values() if isinstance(v, list)), [])
        if not batch:
            break
        records.extend(batch)
        pages = int(payload.get("total_pages") or 1)
        page += 1

    return records


@register
class Events(Collector):
    name = "events"
    title = "UBC events calendar"
    description = (
        "Public events with start/end times, cost, website, description and category, "
        "plus the venue directory (street address, city, coordinates) and the organizer "
        "directory (contact email, phone, website)."
    )
    sources = ("https://events.ubc.ca/resources/webdev/", f"{BASE}/events")

    def collect(self, http: Http, out: Output) -> None:
        events = _collection(http, "events", {"start_date": ARCHIVE_START})
        out.table("events", events, source=f"{BASE}/events")

        for key in ("venues", "organizers", "categories", "tags"):
            try:
                records = _collection(http, key)
            except Exception:  # endpoint is optional on some Tribe versions
                continue
            if records:
                out.table(key, records, source=f"{BASE}/{key}")

        # The iCal feed is the canonical subscribable form; keep a copy alongside.
        out.raw("events.ics", http.get("https://events.ubc.ca/?ical=1").content,
                source="https://events.ubc.ca/?ical=1")
