"""Bookable library spaces and when they are actually free.

Source: the Spaces module of UBC Library's LibCal (Springshare) at
libcal.library.ubc.ca. It is the only public UBC surface that publishes
room-level *occupancy* rather than room-level description, which makes it the
missing half of `learning-spaces`: that collector says what a room is, this one
says whether anyone is in it.

Three things are fetched, in the order the site's own front end does it:

- **Locations** come from the location `<select>` on `/spaces`, so a library
  added or retired by UBC is picked up rather than hard-coded.
- **Rooms** come from the `resources.push({...})` literals the page inlines for
  whichever group it is showing. That list is paginated and group-scoped, so it
  is *not* complete -- Koerner publishes 4 of its 8 rooms that way. The
  availability grid is what actually enumerates a location, and any room it
  returns that the page did not describe is filled in from `/space/<eid>`.
- **Availability** comes from `POST /spaces/availability/grid`, which returns
  15-minute slots. `itemId` on a slot is the `eid` of a room; those are the same
  identifier despite the different names, which is what lets the two join.

Two things are worked out here rather than fetched:

- **Slot state.** The grid marks a slot with a CSS class or leaves it bare, and
  bare means free. `_state` maps the classes UBC currently emits and treats
  anything unrecognised as not-free -- see the note on that function for why the
  default matters.
- **Intervals.** Consecutive slots in the same state are merged, because "free
  09:00-19:00" is the answerable form of forty consecutive quarter-hours. The
  raw grid is not written: at 22k rows a run it is thirty-odd times the size and
  says nothing the merged form does not, since every slot is recoverable by
  subdividing the interval that covers it. What that does drop is each slot's
  `checksum`, which is a per-request booking token rather than an identifier and
  is meaningless the moment the request that issued it ends.

Unlike the rest of this repo these tables are a *snapshot*, not a mirror of
something stable: a booking made a minute after the run invalidates a row. Every
availability row therefore carries `collected_at`, and `_snapshot.json` records
the window the run covered, so a stale file is recognisably stale rather than
quietly wrong.
"""

from __future__ import annotations

import html as html_module
import json
import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .base import Collector, Http, Output, register, utcnow, wants

HOST = "libcal.library.ubc.ca"
BASE = f"https://{HOST}"
SPACES = f"{BASE}/spaces"
GRID = f"{BASE}/spaces/availability/grid"

# Every LibCal space in this instance is on the Vancouver campus.
CAMPUS = "vancouver"

# How far ahead to ask for. LibCal clamps the answer to its own booking window
# (currently about a week), so asking for more than that is harmless and keeps
# working if UBC widens it -- but the endpoint 400s on a range of about a month,
# so this cannot simply be set large.
DAYS_AHEAD = 14

# Which building each LibCal location sits in, as `BLDG_CODE` in the geospatial
# buildings layer. LibCal names a *library*, not a building, and three of them
# share one: the Music/Art/Architecture branch and the Research Commons are both
# inside Irving K. Barber. Without this the rooms cannot be put on a map.
#
# Note WLIB (Woodward Library) is not IRC (P. A. Woodward Instructional
# Resources Centre) -- different buildings, similar names.
BUILDING_CODES = {
    "Irving K. Barber Learning Centre": "IBLC",
    "Music, Art & Architecture Library (IKB)": "IBLC",
    "Research Commons": "IBLC",
    "Koerner Library": "KLIB",
    "Woodward Library": "WLIB",
}

# The class LibCal puts on a slot -> what that slot is. A bare slot is bookable.
SLOT_STATES = {
    None: "free",
    "": "free",
    "s-lc-eq-checkout": "booked",
    "s-lc-eq-r-unavailable": "unavailable",
}

# `<option value="2174">Irving K. Barber Learning Centre</option>`
OPTION_RE = re.compile(r'<option[^>]*\bvalue="(\d+)"[^>]*>\s*([^<]+?)\s*</option>')

# The location dropdown ends at this entry; the options after it belong to the
# group and capacity filters and are not locations.
LOCATIONS_END = "View All Locations"

RESOURCE_RE = re.compile(r"resources\.push\(")
H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
CAPACITY_RE = re.compile(r"Capacity:\s*(\d+)")
PARENTHETICAL_RE = re.compile(r"\(([^)]+)\)")


def _balanced(text: str, start: int) -> str | None:
    """The `{...}` beginning at `start`, respecting strings and escapes."""
    depth = 0
    in_string = False
    escaped = False
    quote = ""

    for index in range(start, len(text)):
        char = text[index]
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
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _js_object(body: str) -> dict[str, Any]:
    """Parse one JS object literal that is JSON apart from its syntax sugar.

    LibCal writes these with bare keys and a trailing comma, neither of which
    JSON allows. `base.js_literal` cannot be used because these arrive as
    `resources.push({...})` calls rather than one assignable literal.
    """
    body = re.sub(r"(?m)^\s*(\w+)\s*:", lambda m: f'"{m.group(1)}":', body)
    body = re.sub(r",(\s*[}\]])", r"\1", body)
    return json.loads(body)


def _resources(page: str) -> list[dict[str, Any]]:
    """Every room the page describes inline, in the order it lists them."""
    found = []
    for match in RESOURCE_RE.finditer(page):
        opener = page.find("{", match.end())
        if opener < 0:
            continue
        body = _balanced(page, opener)
        if body:
            try:
                found.append(_js_object(body))
            except json.JSONDecodeError:
                continue
    return found


def _locations(page: str) -> list[dict[str, Any]]:
    """The libraries the location dropdown offers, in page order."""
    found: list[dict[str, Any]] = []
    for value, label in OPTION_RE.findall(page):
        label = html_module.unescape(label).strip()
        if label == LOCATIONS_END:
            break
        if value == "0" or any(item["lid"] == value for item in found):
            continue
        found.append({
            "lid": value,
            "name": label,
            "campus": CAMPUS,
            "building_code": BUILDING_CODES.get(label),
            "url": f"{SPACES}?lid={value}",
        })
    return found


def _state(class_name: str | None) -> str:
    """What a slot's CSS class means.

    Anything UBC emits that is not in the table is reported as `unavailable`
    rather than `free`. The two errors are not symmetric: hiding a room that was
    actually free wastes a walk across campus, while showing an occupied room as
    free sends someone into a seminar in progress. A new marker should degrade
    to the cautious answer until it is understood.
    """
    if class_name in SLOT_STATES:
        return SLOT_STATES[class_name]
    return "unavailable"


def _time(stamp: str) -> datetime | None:
    try:
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _room_from_page(page: str, eid: int) -> dict[str, Any]:
    """Name and capacity for a room the browsable list left out.

    `/space/<eid>` puts all three facts in its `<h1>`: the room name on the
    first line, its library in parentheses, and the capacity after a label.
    Rooms that are not individually bookable (a seating area, a floor lounge)
    carry only the name, so capacity is left null rather than guessed.
    """
    match = H1_RE.search(page)
    heading = re.sub(r"<[^>]+>", "", match.group(1)) if match else ""
    heading = html_module.unescape(heading)

    name = heading.strip().splitlines()[0].strip() if heading.strip() else None
    capacity = CAPACITY_RE.search(heading)
    location = PARENTHETICAL_RE.search(heading)

    return {
        "eid": eid,
        "title": name,
        "capacity": int(capacity.group(1)) if capacity else None,
        "grouping": location.group(1).strip() if location else None,
        "url": f"{BASE}/space/{eid}",
        "described_by": "space_page",
    }


def _room(resource: dict[str, Any], location: dict[str, Any]) -> dict[str, Any]:
    """One row of the rooms table, from a resource literal."""
    return {
        "eid": resource.get("eid"),
        "lid": location["lid"],
        "location": location["name"],
        "campus": CAMPUS,
        "building_code": location["building_code"],
        "gid": resource.get("gid"),
        "grouping": resource.get("grouping"),
        "title": resource.get("title"),
        "capacity": resource.get("capacity"),
        "url": BASE + resource["url"] if resource.get("url") else None,
        "thumbnail": resource.get("thumbnail"),
        "filter_ids": resource.get("filterIds"),
        "booking_selectable_time": resource.get("gBookingSelectableTime"),
        "described_by": "spaces_page",
    }


def _merge(slots: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse a room's consecutive same-state slots into the intervals they form.

    The grid's grain is a quarter hour, which is the right grain to publish and
    the wrong one to store: forty rows say the same thing as "free 09:00-19:00".
    Every state is merged, not just `free` -- an interval a room is booked for is
    exactly as much of an answer as one it is open for, and keeping both is what
    lets this table stand in for the raw grid rather than summarise it.

    Slots join only where one ends exactly where the next begins *and* both are
    in the same state, so a booking still splits the free time around it, and a
    gap where UBC published nothing at all stays a gap rather than being bridged.
    """
    intervals: list[dict[str, Any]] = []
    ordered = sorted(slots, key=lambda slot: (slot["eid"], slot["state"], slot["start"]))

    for slot in ordered:
        start, end = _time(slot["start"]), _time(slot["end"])
        if not start or not end:
            continue
        last = intervals[-1] if intervals else None
        if (
            last
            and last["eid"] == slot["eid"]
            and last["state"] == slot["state"]
            and last["_end"] == start
        ):
            last["_end"] = end
            last["end"] = slot["end"]
            last["minutes"] = int((end - last["_start"]).total_seconds() // 60)
            continue
        intervals.append({
            "eid": slot["eid"],
            "lid": slot["lid"],
            "location": slot["location"],
            "building_code": slot["building_code"],
            "campus": CAMPUS,
            "room": slot.get("room"),
            "capacity": slot.get("capacity"),
            "state": slot["state"],
            "date": slot["start"][:10],
            "start": slot["start"],
            "end": slot["end"],
            "minutes": int((end - start).total_seconds() // 60),
            "collected_at": slot["collected_at"],
            "_start": start,
            "_end": end,
        })

    for interval in intervals:
        interval.pop("_start", None)
        interval.pop("_end", None)
    return sorted(intervals, key=lambda i: (i["lid"], i["eid"], i["start"]))


@register
class RoomBookings(Collector):
    name = "bookings"
    folder = "room-bookings"
    title = "Library room availability (bookable spaces and free time)"
    description = (
        "Every bookable space in UBC Library's LibCal with its location, capacity and "
        "photo, and when each one is free, booked or unavailable over UBC's booking "
        "window -- the 15-minute grid UBC publishes, merged into intervals. Availability "
        "is a snapshot taken at `collected_at`, not a standing fact; see _snapshot.json "
        "for the window covered."
    )
    sources = (SPACES,)

    def collect(self, http: Http, out: Output) -> None:
        if not wants(CAMPUS):
            out.json("_snapshot.json", {"skipped": f"campus filter excludes {CAMPUS}"})
            return

        _describe(out)
        collected_at = utcnow()
        today = datetime.now(timezone.utc).date()
        window_end = today + timedelta(days=DAYS_AHEAD)

        locations = _locations(http.get(SPACES).text)
        if not locations:
            raise RuntimeError("no locations in the LibCal dropdown; the page markup changed")

        rooms = self._rooms(http, locations)
        slots = self._slots(http, locations, rooms, today, window_end, collected_at)

        # Rooms the grid enumerated but the browsable list never described.
        self._fill_gaps(http, rooms, slots, locations)

        for location in locations:
            held = [room for room in rooms.values() if room["lid"] == location["lid"]]
            location["rooms_collected"] = len(held)
            location["total_capacity"] = sum(
                room["capacity"] for room in held if isinstance(room.get("capacity"), int)
            ) or None

        availability = _merge(slots)
        served = sorted({slot["start"][:10] for slot in slots})

        out.table("locations", locations, source=SPACES)
        out.table(
            "rooms",
            sorted(rooms.values(), key=lambda room: (room["lid"], str(room.get("title")))),
            source=SPACES,
        )
        out.table("availability", availability, source=GRID)
        out.json("_snapshot.json", {
            "collected_at": collected_at,
            "requested_from": today.isoformat(),
            "requested_to": window_end.isoformat(),
            # What UBC actually served, which is its booking window and is
            # normally shorter than the range asked for.
            "days_served": served,
            "locations": len(locations),
            "rooms": len(rooms),
            "intervals": len(availability),
            # The grid rows those intervals were merged from. The raw grid is not
            # written -- these counts are what is left of it, so a run that
            # collected far fewer slots than usual is still visible here.
            "slots_seen": len(slots),
            "free_slots_seen": sum(1 for slot in slots if slot["state"] == "free"),
            "slot_minutes": 15,
            "note": (
                "Availability is a point-in-time snapshot. A booking made after "
                "collected_at is not reflected here; re-run before relying on it."
            ),
        }, source=GRID)

    def _rooms(
        self, http: Http, locations: list[dict[str, Any]]
    ) -> dict[int, dict[str, Any]]:
        """Every room the location pages describe, keyed by eid."""
        def fetch(location: dict[str, Any]) -> tuple[dict[str, Any], str]:
            return location, http.get(SPACES, params={"lid": location["lid"], "gid": 0}).text

        rooms: dict[int, dict[str, Any]] = {}
        for location, page in http.map(fetch, locations):
            for resource in _resources(page):
                room = _room(resource, location)
                if room["eid"] is not None:
                    rooms.setdefault(room["eid"], room)
        return rooms

    def _slots(
        self,
        http: Http,
        locations: list[dict[str, Any]],
        rooms: dict[int, dict[str, Any]],
        start: date,
        end: date,
        collected_at: str,
    ) -> list[dict[str, Any]]:
        """The availability grid for every location, one request each."""
        def fetch(location: dict[str, Any]) -> tuple[dict[str, Any], Any]:
            payload = http.post_json(GRID, {
                "lid": location["lid"],
                "gid": 0,
                "eid": -1,
                "seat": 0,
                "seatId": 0,
                "zone": 0,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "pageIndex": 0,
                "pageSize": 500,
            }, headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{SPACES}?lid={location['lid']}",
            })
            return location, payload

        slots: list[dict[str, Any]] = []
        for location, payload in http.map(fetch, locations):
            for slot in (payload or {}).get("slots") or []:
                eid = slot.get("itemId")
                room = rooms.get(eid) or {}
                slots.append({
                    "eid": eid,
                    "lid": location["lid"],
                    "location": location["name"],
                    "building_code": location["building_code"],
                    "campus": CAMPUS,
                    "room": room.get("title"),
                    "capacity": room.get("capacity"),
                    "date": (slot.get("start") or "")[:10],
                    "start": slot.get("start"),
                    "end": slot.get("end"),
                    "state": _state(slot.get("className")),
                    # Kept as UBC sent it, but it is a per-request booking token
                    # rather than an identifier: it is meaningless once the page
                    # that issued it is gone.
                    "checksum": slot.get("checksum"),
                    "collected_at": collected_at,
                })
        return slots

    def _fill_gaps(
        self,
        http: Http,
        rooms: dict[int, dict[str, Any]],
        slots: list[dict[str, Any]],
        locations: list[dict[str, Any]],
    ) -> None:
        """Describe the rooms only the grid knew about, and label the slots.

        The location pages paginate their resource list inside one group, so a
        library with more rooms than fit on a page -- or with rooms in a group
        the dropdown does not offer -- publishes availability for rooms it never
        named. Those are fetched one page each and folded in, so no slot is left
        pointing at an anonymous room.
        """
        by_lid = {location["lid"]: location for location in locations}
        missing = sorted({
            slot["eid"] for slot in slots
            if slot["eid"] is not None and slot["eid"] not in rooms
        })
        if not missing:
            return

        def fetch(eid: int) -> tuple[int, str | None]:
            try:
                return eid, http.get(f"{BASE}/space/{eid}").text
            except Exception:
                return eid, None

        lid_of = {slot["eid"]: slot["lid"] for slot in slots}
        for eid, page in http.map(fetch, missing):
            location = by_lid.get(lid_of.get(eid), {})
            room = {
                "eid": eid,
                "lid": location.get("lid"),
                "location": location.get("name"),
                "campus": CAMPUS,
                "building_code": location.get("building_code"),
                "gid": None,
                "grouping": None,
                "title": None,
                "capacity": None,
                "url": f"{BASE}/space/{eid}",
                "thumbnail": None,
                "filter_ids": None,
                "booking_selectable_time": None,
                "described_by": "space_page",
            }
            if page:
                room.update({k: v for k, v in _room_from_page(page, eid).items() if v is not None})
            rooms[eid] = room

        for slot in slots:
            if slot.get("room") is None:
                room = rooms.get(slot["eid"]) or {}
                slot["room"] = room.get("title")
                slot["capacity"] = room.get("capacity")


def _describe(out: Output) -> None:
    out.describe(
        "locations",
        grain="One LibCal location -- a UBC library or branch that publishes bookable space.",
        columns={
            "lid": "LibCal location id; the key rooms and slots carry.",
            "name": "Library name as UBC's own dropdown gives it.",
            "building_code": "BLDG_CODE of the building it sits in, for the geospatial join. "
                             "Three locations share IBLC.",
            "rooms_collected": "Rooms found for this location on this run.",
            "total_capacity": "Seats across those rooms, where capacity is published.",
        },
        joins=[
            "geospatial/ubcv/locations/geojson/ubcv_buildings.geojson on building_code = BLDG_CODE",
            "learning-spaces/buildings on building_code = `Building Code`",
        ],
    )
    out.describe(
        "rooms",
        grain="One bookable space -- a group study room, project room or seating area.",
        columns={
            "eid": "LibCal space id. The same number the availability grid calls `itemId`.",
            "lid": "The location this room belongs to.",
            "capacity": "Seats, where UBC publishes them; null for spaces that are not "
                        "individually bookable.",
            "building_code": "Inherited from the location, for the geospatial join.",
            "described_by": "`spaces_page` if the browsable list described this room, "
                            "`space_page` if it only appeared in the grid and was filled "
                            "in from its own page.",
            "filter_ids": "LibCal's internal amenity filter ids, kept as sent.",
        },
        joins=["slots on eid", "openings on eid", "locations on lid"],
    )
    out.describe(
        "availability",
        grain=(
            "One uninterrupted stretch of one room in one state, as it stood at "
            "`collected_at` -- UBC's consecutive 15-minute slots merged."
        ),
        columns={
            "eid": "The room. Joins to rooms.eid.",
            "state": "`free`, `booked`, or `unavailable`. Anything UBC marks in a way this "
                     "collector does not recognise is reported `unavailable`, never `free`. "
                     "Filter on `free` to find bookable time.",
            "start": "First moment in this state, `YYYY-MM-DD HH:MM:SS` local, as UBC sends "
                     "it; `end` is the first moment no longer in it.",
            "minutes": "Length of the stretch, so a usable block can be picked without "
                       "subtracting timestamps.",
            "collected_at": "When this snapshot was taken. Availability moves; this does not.",
        },
        joins=["rooms on eid", "locations on lid"],
    )
