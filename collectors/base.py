"""Shared plumbing for every collector: HTTP, pagination helpers, and disk output.

Collectors subclass `Collector`, register themselves with `@register`, and write
whatever they gather through the `Output` handle they are given. Everything a
collector writes is tracked so `update.py` can rebuild `data/manifest.json`.
"""

from __future__ import annotations

import csv
import json
import re
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

USER_AGENT = "ubc-data/1.0 (public UBC institutional open-data mirror)"

# Above this many records a JSON file is written compact rather than indented,
# so the multi-megabyte course dumps stay a sane size on disk.
PRETTY_LIMIT = 2000


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Campus selection
# --------------------------------------------------------------------------

CAMPUS_CHOICES = ("vancouver", "okanagan", "both")

# Which campus the dataset covers. `update.py` sets this from --campus.
# Collectors translate their own campus representation (a `ubco/` directory, a
# `_O` course-code suffix, a "(UBC-O)" term label) into one of these names and
# ask `wants()`, so the rule lives in exactly one place.
_selected = "vancouver"


def set_campus(value: str) -> None:
    if value not in CAMPUS_CHOICES:
        raise ValueError(f"campus must be one of {CAMPUS_CHOICES}, got {value!r}")
    global _selected
    _selected = value


def selected_campus() -> str:
    return _selected


def wants(campus: str | None) -> bool:
    """True if data belonging to `campus` should be collected.

    `None` means the record carries no campus marker, which is always kept --
    dropping unlabelled rows would silently lose data on a naming change.
    """
    if campus is None or _selected == "both":
        return True
    return campus == _selected


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Http:
    """Thread-safe HTTP client with retry/backoff and optional global throttling."""

    def __init__(
        self,
        *,
        timeout: int = 60,
        retries: int = 4,
        min_interval: float = 0.0,
        workers: int = 8,
    ):
        self.timeout = timeout
        self.retries = retries
        self.min_interval = min_interval
        self.workers = max(1, workers)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._next_slot = 0.0

    @property
    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            retry = Retry(
                total=self.retries,
                backoff_factor=1.0,
                status_forcelist=(408, 429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET", "POST"}),
                respect_retry_after_header=True,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_maxsize=32)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            session.headers["User-Agent"] = USER_AGENT
            self._local.session = session
        return session

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_slot - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_slot = now + self.min_interval

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self._throttle()
        kwargs.setdefault("timeout", self.timeout)
        response = self.session.request(method, url, **kwargs)
        response.raise_for_status()
        return response

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.get(url, **kwargs).json()

    def post_json(self, url: str, data: dict[str, Any], **kwargs: Any) -> Any:
        return self.request("POST", url, data=data, **kwargs).json()

    def map(
        self, fn: Callable[[Any], Any], items: Iterable[Any], workers: int | None = None
    ) -> list[Any]:
        """Run `fn` over `items` in a thread pool, preserving input order."""
        items = list(items)
        if not items:
            return []
        limit = max(1, min(workers or self.workers, len(items)))
        with ThreadPoolExecutor(max_workers=limit) as pool:
            return list(pool.map(fn, items))


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


@dataclass
class Dataset:
    path: str
    records: int | None
    bytes: int
    source: str | None = None
    grain: str | None = None
    columns: dict[str, str] | None = None
    joins: list[str] | None = None


def _scalar(value: Any) -> Any:
    """Collapse nested values so they survive a trip through CSV."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, ensure_ascii=False)


class Output:
    """Writes a collector's files under `data/<folder>/` and tracks what it wrote."""

    def __init__(self, folder: str, root: Path = DATA_DIR):
        self.root = root
        self.base = root / folder
        self.datasets: list[Dataset] = []
        self.docs: dict[str, dict[str, Any]] = {}

    def describe(
        self,
        stem: str,
        *,
        grain: str,
        columns: dict[str, str],
        joins: Sequence[str] = (),
    ) -> None:
        """Record what a dataset means, so the manifest can say so.

        `grain` is the one sentence that matters most to anything reading the
        data cold -- what a single row *is*. `joins` name the other datasets a
        row can be linked to and on which column, because the fastest way to
        misread this repo is to guess at a foreign key. Call it with the same
        stem passed to `table()`; both the .json and .csv pick the doc up.
        """
        self.docs[stem] = {"grain": grain, "columns": dict(columns), "joins": list(joins)}

    def _target(self, relpath: str) -> Path:
        path = self.base / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _track(self, path: Path, records: int | None, source: str | None) -> None:
        relative = path.relative_to(self.root).as_posix()
        # Docs are registered per table stem; both `x.json` and `x.csv` are the
        # same table, so strip the suffix and the folder prefix to look it up.
        stem = relative.removeprefix(f"{self.base.relative_to(self.root).as_posix()}/")
        doc = self.docs.get(stem.rsplit(".", 1)[0], {})
        self.datasets.append(
            Dataset(
                path=relative,
                records=records,
                bytes=path.stat().st_size,
                source=source,
                grain=doc.get("grain"),
                columns=doc.get("columns"),
                joins=doc.get("joins"),
            )
        )

    def json(self, relpath: str, payload: Any, *, source: str | None = None) -> Path:
        path = self._target(relpath)
        indent = 2 if not isinstance(payload, list) or len(payload) <= PRETTY_LIMIT else None
        separators = None if indent else (",", ":")
        path.write_text(
            json.dumps(payload, indent=indent, separators=separators, ensure_ascii=False),
            encoding="utf-8",
        )
        self._track(path, len(payload) if isinstance(payload, list) else None, source)
        return path

    def csv(
        self,
        relpath: str,
        rows: Sequence[dict[str, Any]],
        *,
        columns: Sequence[str] | None = None,
        source: str | None = None,
    ) -> Path:
        path = self._target(relpath)
        if columns is None:
            seen: dict[str, None] = {}
            for row in rows:
                for key in row:
                    seen.setdefault(key, None)
            columns = list(seen)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: _scalar(row.get(key)) for key in columns})
        self._track(path, len(rows), source)
        return path

    def table(self, relpath_stem: str, rows: Sequence[dict[str, Any]], *, source: str | None = None) -> None:
        """Write the same records as both JSON (fidelity) and CSV (convenience)."""
        self.json(f"{relpath_stem}.json", list(rows), source=source)
        if rows:
            self.csv(f"{relpath_stem}.csv", rows, source=source)

    def raw(self, relpath: str, content: bytes, *, source: str | None = None) -> Path:
        path = self._target(relpath)
        path.write_bytes(content)
        self._track(path, None, source)
        return path

    def prune(self) -> list[str]:
        """Delete files in this folder that the current run did not write.

        Without this, a renamed or retired dataset lingers on disk forever and
        reads as current -- the manifest says `year_levels.json` while a stale
        `years.json` sits next to it. Only ever called after a collector
        succeeds, so a failed run never deletes the previous good data.
        """
        if not self.base.exists():
            return []

        written = {(self.root / dataset.path).resolve() for dataset in self.datasets}
        removed: list[str] = []

        for path in sorted(self.base.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_file():
                if path.resolve() not in written:
                    removed.append(path.relative_to(self.root).as_posix())
                    path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()

        return removed


# --------------------------------------------------------------------------
# Collector registry
# --------------------------------------------------------------------------


class Collector:
    """One logical group of UBC data."""

    name: str = ""
    folder: str = ""
    title: str = ""
    description: str = ""
    sources: Sequence[str] = ()

    def collect(self, http: Http, out: Output) -> None:  # pragma: no cover - interface
        raise NotImplementedError


REGISTRY: dict[str, type[Collector]] = {}


def register(cls: type[Collector]) -> type[Collector]:
    if not cls.name:
        raise ValueError(f"{cls.__name__} needs a name")
    cls.folder = cls.folder or cls.name
    REGISTRY[cls.name] = cls
    return cls


# --------------------------------------------------------------------------
# Drupal JSON:API
# --------------------------------------------------------------------------

# Editorial/revision noise that adds bulk without telling a student anything.
# `path` is dropped but not lost: its `alias` is promoted to a column of its
# own, because it is the only thing in a Drupal record that says where the page
# lives on the site -- the public URL, and the page hierarchy with it.
JSONAPI_DROP = {
    "metatag",
    "path",
    "promote",
    "sticky",
    "default_langcode",
    "revision_translation_affected",
    "revision_timestamp",
    "vid",
    "publish_on",
    "unpublish_on",
    "langcode",
}


def simplify_jsonapi(record: dict[str, Any]) -> dict[str, Any]:
    """Flatten a JSON:API resource object into a plain, readable dict."""
    out: dict[str, Any] = {"id": record.get("id"), "type": record.get("type")}
    for key, value in (record.get("attributes") or {}).items():
        key = key.removeprefix("drupal_internal__")
        if key == "path":
            alias = (value or {}).get("alias") if isinstance(value, dict) else None
            if alias:
                out["alias"] = alias
            continue
        if key in JSONAPI_DROP:
            continue
        out[key] = value

    related: dict[str, Any] = {}
    for key, value in (record.get("relationships") or {}).items():
        data = (value or {}).get("data")
        if data is None:
            continue
        key = key.removeprefix("field_")
        if isinstance(data, list):
            related[key] = [item.get("id") for item in data if isinstance(item, dict)]
        elif isinstance(data, dict):
            related[key] = data.get("id")
    if related:
        out["related"] = related
    return out


def jsonapi_index(http: Http, host: str) -> set[str]:
    """Resource types a Drupal site exposes, e.g. {"node--course", ...}."""
    payload = http.get_json(f"https://{host}/jsonapi")
    return {key for key in (payload.get("links") or {}) if "--" in key}


def jsonapi_collection(
    http: Http,
    host: str,
    resource: str,
    *,
    params: dict[str, Any] | None = None,
    workers: int | None = None,
    page_size: int = 50,
) -> list[dict[str, Any]]:
    """Fetch every record of a Drupal JSON:API collection.

    Drupal caps `page[limit]` at 50, so a 47k-row collection is 900+ requests.
    When the server reports a total we fan the offsets out across threads;
    otherwise we walk `links.next` one page at a time.
    """
    url = f"https://{host}/jsonapi/{resource}"
    base = dict(params or {})
    base["page[limit]"] = page_size

    first = http.get_json(url, params={**base, "page[offset]": 0})
    records = [simplify_jsonapi(item) for item in first.get("data") or []]
    total = (first.get("meta") or {}).get("count")

    if isinstance(total, int) and total > len(records):
        def fetch(offset: int) -> list[dict[str, Any]]:
            payload = http.get_json(url, params={**base, "page[offset]": offset})
            return [simplify_jsonapi(item) for item in payload.get("data") or []]

        for chunk in http.map(fetch, range(page_size, total, page_size), workers=workers):
            records.extend(chunk)
        return records

    next_link = ((first.get("links") or {}).get("next") or {}).get("href")
    while next_link:
        payload = http.get_json(next_link)
        batch = payload.get("data") or []
        if not batch:
            break
        records.extend(simplify_jsonapi(item) for item in batch)
        next_link = ((payload.get("links") or {}).get("next") or {}).get("href")
    return records


# --------------------------------------------------------------------------
# WordPress REST
# --------------------------------------------------------------------------


def js_literal(html: str, name: str) -> Any:
    """Read one `var <name> = [...]/{...}` literal out of a page.

    Several UBC WordPress sites inline a client-side app's whole dataset this
    way -- the program finder, the cost estimator -- and it is cheaper and more
    complete than driving the UI that consumes it. WordPress emits these with
    json_encode, so the body is valid JSON once the matching bracket is found.
    Scanning respects string literals and escapes.
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


def wp_collection(
    http: Http,
    host: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    per_page: int = 100,
    workers: int | None = None,
) -> list[Any]:
    """Fetch every page of a WP REST collection using X-WP-TotalPages."""
    url = f"https://{host}/wp-json/{path}"
    base = {**(params or {}), "per_page": per_page}

    first = http.get(url, params={**base, "page": 1})
    records = list(first.json())
    pages = int(first.headers.get("X-WP-TotalPages") or 1)

    if pages > 1:
        def fetch(page: int) -> list[Any]:
            return http.get_json(url, params={**base, "page": page})

        for chunk in http.map(fetch, range(2, pages + 1), workers=workers):
            records.extend(chunk)
    return records
