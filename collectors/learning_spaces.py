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
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

from .base import Collector, Http, Output, register, wp_collection

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

    return list(found.values()) or list(DEFAULT_CONFIGS)


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

        buildings: list[dict[str, Any]] = []
        seen_buildings: set[str] = set()
        for config in {c.get("campus"): c for c in configs}.values():
            payload = _call(http, "find_a_space_buildings", nonce, _params(config))
            for record in _body(payload).get("records") or []:
                if record.get("id") in seen_buildings:
                    continue
                seen_buildings.add(record.get("id"))
                buildings.append(_flatten(record, config))

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
