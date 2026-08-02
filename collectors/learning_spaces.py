"""Teaching and study spaces: classrooms, lounges and the buildings holding them.

Source: the Find a Space tool on learningspaces.ubc.ca (UBC Facilities / VPFO).
The React front end talks to an admin-ajax endpoint that proxies an Airtable
base, so we drive the same three actions it does. The nonce is short-lived and
page-scoped, so it is scraped fresh on every run.

Which spaces you get back is decided by a `data-config` blob on the block that
mounts the app -- `{"campus": ..., "formal": true}` for bookable classrooms,
`formal: false` for informal study space. Those configs are discovered by
scanning the site's pages rather than hard-coded, so an added campus or tab is
picked up on the next run.

`filters.json` is the controlled vocabulary the tool filters on -- room
features, A/V and accessibility amenities, furniture styles, capacity range --
which doubles as the schema for what UBC records about a space.

Two things are worked out here rather than fetched:

- **Floor.** Airtable has no floor field. Informal spaces usually say it in
  words ("B Block 2nd Floor Lounge"); classrooms encode it in the room number.
  `_floor` reads the first, falls back to the second, and leaves the rest null
  rather than guessing -- see the note on that function for why IRC and FNH
  cannot be resolved.
- **Buildings carry their rooms.** The buildings endpoint returns counts and a
  name and nothing else, so each building is given the rooms collected for it,
  their floors and capacities, and a cover photo, which is what makes the table
  answerable on its own.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from typing import Any

from .base import Collector, Http, Output, register, wants, wp_collection

HOST = "learningspaces.ubc.ca"
BASE = f"https://{HOST}"
AJAX = f"{BASE}/wp-admin/admin-ajax.php"
FIND_A_SPACE = f"{BASE}/find-a-space/"

NONCE_RE = re.compile(r'find_a_space_script_vars\s*=\s*\{.*?"_nonce"\s*:\s*"([A-Za-z0-9]+)"', re.S)
CONFIG_RE = re.compile(r'id="ubc-vpfo-find-a-space-root"[^>]*data-config="([^"]*)"')

# Fallback if page discovery turns up nothing (e.g. the block markup changes).
DEFAULT_CONFIGS = ({"campus": "vancouver", "formal": True}, {"campus": "vancouver", "formal": False})

# Airtable hands back ~10 rows per call; this bounds a runaway cursor.
MAX_PAGES = 500


def _nonce(http: Http) -> str:
    page = http.get(FIND_A_SPACE).text
    match = NONCE_RE.search(page)
    if not match:
        raise RuntimeError("could not find the find_a_space nonce; the page markup changed")
    return match.group(1)


def _configs(http: Http) -> list[dict[str, Any]]:
    """Find every Find a Space instance published on the site, deduplicated."""
    found: dict[str, dict[str, Any]] = {}
    try:
        pages = wp_collection(http, HOST, "wp/v2/pages")
    except Exception:
        pages = []

    for page in pages:
        rendered = (page.get("content") or {}).get("rendered") or ""
        for raw in CONFIG_RE.findall(rendered):
            try:
                config = json.loads(html.unescape(raw))
            except json.JSONDecodeError:
                continue
            if isinstance(config, dict):
                found.setdefault(json.dumps(config, sort_keys=True), config)

    configs = list(found.values()) or list(DEFAULT_CONFIGS)
    return [c for c in configs if wants(str(c.get("campus", "")).lower() or None)]


def _params(config: dict[str, Any]) -> dict[str, Any]:
    """admin-ajax reads booleans as the strings JavaScript would have sent."""
    return {k: ("true" if v is True else "false" if v is False else v) for k, v in config.items()}


def _call(http: Http, action: str, nonce: str, data: dict[str, Any]) -> Any:
    body = {"action": action, "_nonce": nonce}
    body.update({f"data[{key}]": value for key, value in data.items()})
    payload = http.post_json(AJAX, body)
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(f"{action} failed: {payload.get('data')}")
    return payload


def _body(payload: Any) -> dict[str, Any]:
    """Rooms come back at the top level; buildings and meta nest under `data`."""
    if isinstance(payload, dict):
        inner = payload.get("data")
        if isinstance(inner, dict) and ("records" in inner or "offset" in inner):
            return inner
        return payload
    return {}


# "2nd Floor Lounge", "B Block 3rd Floor Lounge" -- the floor written out.
ORDINAL_FLOOR_RE = re.compile(r"(\d+)\s*(?:st|nd|rd|th)\s*floor", re.I)

# A room number in UBC's usual scheme: an optional block or wing letter, three
# or four digits whose first is the floor, an optional suffix. "B101" is Block
# B room 101 on floor 1, not a basement -- Buchanan alone has 61 of those.
ROOM_NUMBER_RE = re.compile(r"^[A-Z]?(\d{3,4})[A-Z]?$", re.I)

# "(Room 101)" inside a longer informal name.
EMBEDDED_ROOM_RE = re.compile(r"\broom\s+(\d{3,4})\b", re.I)

BASEMENT_RE = re.compile(r"\bbasement\b", re.I)
GROUND_RE = re.compile(r"\bground floor\b|\bmain floor\b", re.I)

# Airtable signs attachment URLs with an expiry epoch in the path.
EXPIRY_RE = re.compile(r"/(\d{13})/")


def _floor(*texts: str) -> tuple[int | None, str | None]:
    """Which floor a space is on, and how that was worked out.

    Read in order of how directly UBC states it: the words "3rd floor" beat a
    room number, which beats nothing. Anything that does not match is left null
    rather than guessed, which is why IRC comes back empty -- its rooms are
    numbered 1-6, G41-G66 and B75-B79 on a scheme of their own, and FNH 30/40/
    50/60 is too short to tell a floor from a room. Better a null than a
    confident wrong floor for a student looking for a room.
    """
    for text in texts:
        text = (text or "").strip()
        if not text:
            continue
        if match := ORDINAL_FLOOR_RE.search(text):
            return int(match.group(1)), "stated"
        if BASEMENT_RE.search(text):
            return -1, "stated"
        if GROUND_RE.search(text):
            return 0, "stated"

    for text in texts:
        text = (text or "").strip()
        match = ROOM_NUMBER_RE.match(text) or EMBEDDED_ROOM_RE.search(text)
        if match:
            return int(match.group(1)[0]), "room_number"

    return None, None


def _photo(gallery: Any) -> dict[str, Any]:
    """The first image UBC lists for a space, as flat columns.

    The URL is a signed Airtable link that expires within a day or so -- UBC's
    own pages serve the same short-lived links, so there is no stable one to
    store instead, and mirroring the bytes is not this project's job any more
    than mirroring the PDFs in `reports` is. What makes that workable is saying
    so precisely: `cover_photo_expires` gives the deadline and
    `cover_photo_id` outlives it, so a stale row is recognisably stale rather
    than silently broken, and `Room Link` re-renders the image on demand.
    """
    images = gallery if isinstance(gallery, list) else []
    photo: dict[str, Any] = {
        "photo_count": len(images),
        "cover_photo_url": None,
        "cover_photo_thumbnail_url": None,
        "cover_photo_id": None,
        "cover_photo_filename": None,
        "cover_photo_width": None,
        "cover_photo_height": None,
        "cover_photo_expires": None,
    }
    first = next((image for image in images if isinstance(image, dict)), None)
    if not first:
        return photo

    thumbnails = first.get("thumbnails") or {}
    large = thumbnails.get("large") or thumbnails.get("full") or {}
    url = first.get("url")
    photo.update(
        cover_photo_url=url,
        cover_photo_thumbnail_url=large.get("url"),
        cover_photo_id=first.get("id"),
        cover_photo_filename=html.unescape(first.get("filename") or "") or None,
        cover_photo_width=first.get("width"),
        cover_photo_height=first.get("height"),
        cover_photo_expires=_expiry(url),
    )
    return photo


def _expiry(url: str | None) -> str | None:
    match = EXPIRY_RE.search(url or "")
    if not match:
        return None
    stamp = datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _flatten(record: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Airtable rows nest everything under `fields`; lift it to the top level."""
    flat: dict[str, Any] = {
        "id": record.get("id"),
        "campus": config.get("campus"),
        "formal": config.get("formal"),
        "space_type": "classroom" if config.get("formal") else "study space",
        "created_time": record.get("createdTime"),
    }
    for key, value in (record.get("fields") or {}).items():
        # Airtable linked-record fields arrive as single-item lists.
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], (str, int, float)):
            value = value[0]
        flat[key] = value
    return flat


# What each room contributes to its building's `rooms` list. Enough to pick a
# room without opening the rooms table; the full record is still there on it.
ROOM_SUMMARY = (
    ("name", "Name"),
    ("room_number", "Room Number"),
    ("capacity", "Capacity"),
    ("space_type", "space_type"),
    ("room_link", "Room Link"),
)


def _enrich_room(room: dict[str, Any]) -> None:
    """Add the derived floor and cover photo to a room, in place."""
    room["floor"], room["floor_source"] = _floor(
        room.get("Room Number"), room.get("Name"), room.get("Title")
    )
    room.update(_photo(room.get("Image Gallery")))


def _attach_rooms(
    buildings: list[dict[str, Any]], rooms: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Give each building the rooms collected for it, plus what they add up to.

    The endpoint returns a name and two counts, which is not enough to answer
    anything on its own. UBC's own `Formal Count`/`Informal Count` are kept
    beside the collected counts rather than replaced -- they are UBC's numbers
    for the whole building and can legitimately differ from what the tool
    published on the day of the run.
    """
    by_code: dict[str, list[dict[str, Any]]] = {}
    for room in rooms:
        by_code.setdefault(room.get("Building Code"), []).append(room)

    for building in buildings:
        held = sorted(
            by_code.get(building.get("Building Code"), []),
            # Floors first where known, then the biggest rooms, so the head of
            # the list is the part of the building someone is most likely after.
            key=lambda room: (room.get("floor") is None, room.get("floor") or 0,
                              -(room.get("Capacity") or 0)),
        )
        capacities = [room["Capacity"] for room in held if isinstance(room.get("Capacity"), int)]
        floors = sorted({room["floor"] for room in held if room.get("floor") is not None})

        building["rooms"] = [
            {key: room.get(field) for key, field in ROOM_SUMMARY}
            | {"floor": room.get("floor"), "cover_photo_url": room.get("cover_photo_url")}
            for room in held
        ]
        building["rooms_collected"] = len(held)
        building["formal_collected"] = sum(1 for room in held if room.get("formal"))
        building["informal_collected"] = sum(1 for room in held if not room.get("formal"))
        building["total_capacity"] = sum(capacities) or None
        building["largest_room_capacity"] = max(capacities, default=None)
        building["smallest_room_capacity"] = min(capacities, default=None)
        building["floors"] = floors
        building["building_link"] = next(
            (room.get("Building Link") for room in held if room.get("Building Link")), None
        )

        # The building's cover photo is its largest room's, which is the shot
        # that actually shows the space rather than a corner of a corridor.
        best = max(
            (room for room in held if room.get("cover_photo_url")),
            key=lambda room: room.get("Capacity") or 0,
            default=None,
        )
        building["cover_photo_url"] = (best or {}).get("cover_photo_url")
        building["cover_photo_thumbnail_url"] = (best or {}).get("cover_photo_thumbnail_url")
        building["cover_photo_expires"] = (best or {}).get("cover_photo_expires")
        building["cover_photo_room"] = (best or {}).get("Name")

    return sorted(buildings, key=lambda b: (b.get("Building Code") or ""))


@register
class LearningSpaces(Collector):
    name = "spaces"
    folder = "learning-spaces"
    title = "Learning spaces (classrooms, study space, bookable rooms)"
    description = (
        "Bookable classrooms and informal study spaces with capacity, building code, "
        "room number, photos and links, the per-building formal/informal room counts, "
        "and the full filter vocabulary UBC records for a space (A/V, accessibility, "
        "furniture style, room features)."
    )
    sources = (FIND_A_SPACE, f"{BASE}/find-a-space-informal/")

    def collect(self, http: Http, out: Output) -> None:
        nonce = _nonce(http)
        configs = _configs(http)

        rooms: list[dict[str, Any]] = []
        filters: dict[str, Any] = {}
        seen_rooms: set[str] = set()

        for config in configs:
            params = _params(config)
            for room in self._rooms(http, nonce, params, config):
                key = f"{room.get('id')}|{room.get('space_type')}"
                if key in seen_rooms:
                    continue
                seen_rooms.add(key)
                rooms.append(room)

            label = f"{config.get('campus', 'unknown')}-{'formal' if config.get('formal') else 'informal'}"
            meta = _call(http, "find_a_space_meta", nonce, params)
            filters[label] = _body(meta).get("data") or {}

        for room in rooms:
            _enrich_room(room)

        buildings: list[dict[str, Any]] = []
        seen_buildings: set[str] = set()
        # Every config, not one per campus: the endpoint answers for whichever
        # tab it is asked about, so asking only once returned the 38 buildings
        # with informal space and none of the 12 that only hold classrooms.
        # Airtable ids are stable across the two tabs, so the dedup still holds.
        for config in configs:
            payload = _call(http, "find_a_space_buildings", nonce, _params(config))
            for record in _body(payload).get("records") or []:
                if record.get("id") in seen_buildings:
                    continue
                seen_buildings.add(record.get("id"))
                buildings.append(_flatten(record, config))

        buildings = _attach_rooms(buildings, rooms)

        out.describe(
            "rooms",
            grain="one bookable classroom or informal study space",
            columns={
                "Name": "short name, e.g. `AERL 120`",
                "Title": "room title as Find a Space shows it",
                "Building Code": "e.g. `SWNG`; the building this room is in",
                "Room Number": "room number within the building, verbatim",
                "Capacity": "seats",
                "floor": "derived: -1 basement, 0 ground, 1 and up. Null where UBC's "
                         "numbering does not say -- IRC and FNH number rooms on schemes "
                         "of their own and are left null rather than guessed",
                "floor_source": "`stated` if UBC wrote the floor in words, `room_number` "
                                "if it came from the leading digit, null if unknown",
                "formal": "true for bookable classrooms, false for informal study space",
                "space_type": "the tab it came from",
                "Formatted_Furniture": "furniture, as published",
                "Formatted_Room_Layout_Type": "layout style",
                "photo_count": "how many images UBC publishes for the room",
                "cover_photo_url": "first image. A SIGNED URL THAT EXPIRES -- see "
                                   "cover_photo_expires. UBC's own pages serve the same "
                                   "short-lived links, so there is no stable one to store",
                "cover_photo_thumbnail_url": "the ~768px version of the same image",
                "cover_photo_expires": "when cover_photo_url stops working, UTC. Past this, "
                                       "re-run the collector or open `Room Link`",
                "cover_photo_id": "Airtable attachment id; stable, unlike the URL",
                "cover_photo_filename": "original filename",
                "cover_photo_width": "pixels",
                "cover_photo_height": "pixels",
                "Room Link": "the page for this room. The durable way to an image -- it "
                             "re-renders a fresh signed URL on load, for the 409 of 411 "
                             "rooms that have a photo at all",
                "Image Gallery": "every image, with all thumbnail sizes, as UBC returns it",
            },
            joins=[
                "Building Code -> spaces/buildings.Building Code",
                "Building Code ~ geospatial/ubcv/locations building code",
            ],
        )
        out.describe(
            "buildings",
            grain="one building with teaching or study space, and the rooms in it",
            columns={
                "Building Code": "e.g. `IKBLC`; unique within this table",
                "Building Name": "the building's name",
                "rooms": "the rooms collected for this building, floor order then largest "
                         "first: name, room_number, floor, capacity, space_type, "
                         "cover_photo_url, room_link",
                "rooms_collected": "how many rooms that is",
                "formal_collected": "of those, bookable classrooms",
                "informal_collected": "of those, informal study spaces",
                "Formal Count": "UBC's own classroom count for the building; can differ "
                                "from formal_collected, which counts what the run returned",
                "Informal Count": "UBC's own informal count, same caveat",
                "floors": "distinct floors with a space on them, ascending; excludes rooms "
                          "whose floor could not be derived",
                "total_capacity": "seats across the collected rooms",
                "largest_room_capacity": "the biggest room in the building",
                "smallest_room_capacity": "the smallest",
                "cover_photo_url": "the largest room's first image. EXPIRES -- see "
                                   "cover_photo_expires",
                "cover_photo_expires": "when that URL stops working, UTC",
                "cover_photo_room": "which room the cover photo is of",
                "building_link": "the building's page on learningspaces.ubc.ca",
                "formal": "which tab the building record was first seen on; not a property "
                          "of the building -- use formal_collected/Formal Count instead",
            },
            joins=["Building Code <- spaces/rooms.Building Code"],
        )
        out.table("rooms", rooms, source=FIND_A_SPACE)
        out.table("buildings", buildings, source=FIND_A_SPACE)
        out.json("filters.json", filters, source=FIND_A_SPACE)
        out.json("_configs.json", configs, source=FIND_A_SPACE)

    def _rooms(
        self, http: Http, nonce: str, params: dict[str, Any], config: dict[str, Any]
    ) -> list[dict[str, Any]]:
        rooms: list[dict[str, Any]] = []
        offset: str | None = None
        seen_offsets: set[str] = set()

        for _ in range(MAX_PAGES):
            data = dict(params)
            if offset:
                data["offset"] = offset
            body = _body(_call(http, "find_a_space_rooms", nonce, data))

            batch = body.get("records") or []
            rooms.extend(_flatten(record, config) for record in batch)

            offset = body.get("offset")
            if not offset or not batch or offset in seen_offsets:
                break
            seen_offsets.add(offset)

        return rooms
