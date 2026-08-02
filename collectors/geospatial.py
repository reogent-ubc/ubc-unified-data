"""Campus geospatial data: buildings, addresses, trees, roads, land use, POIs.

Source: the UBCGeodata/ubc-geospatial-opendata GitHub repository, which UBC
Campus + Community Planning publishes as the authoritative wayfinding and
operational geometry for both campuses. Files are mirrored verbatim so the
GeoJSON stays valid and the accompanying field-definition CSVs come along.

One correction is applied on the way through. Five CSVs under
`ubcv/locations/csv/` label their coordinate columns the wrong way round: `LAT`
holds the longitude and `LONG` holds the latitude. Mirroring that verbatim ships
coordinates that put UBC in the Southern Ocean for anyone who trusts the header,
so `_fix_swapped_coordinates` swaps the two names -- see the note on that
function for why only the header line is touched, and why the test is a property
of the numbers rather than a list of filenames. Every file corrected is named in
`_source.json` under `corrections`, so the divergence from upstream is on the
record rather than silent. The GeoJSON alongside these CSVs is already correct
and is never touched.
"""

from __future__ import annotations

import csv
import io
import statistics
from pathlib import PurePosixPath
from typing import Any

from .base import Collector, Http, Output, register, utcnow, wants

ORG = "UBCGeodata"

# Repository -> where its files are mounted in `data/geospatial/`.
#
# `ubc-geospatial-opendata` is UBC's consolidated publication and already uses
# the `<campus>/<theme>/<format>/` layout this directory mirrors, so it mounts at
# the root. `ubcv-parking` is a separate, still-maintained repo that predates
# that consolidation and is flat (`csv/`, `geojson/`, `metadata/`), so it is
# mounted as a theme to keep one layout across the whole directory -- which is
# also what lets `_index` keep deriving campus and theme from the path.
#
# The org's other repos are deliberately absent. `ubcv-locations`,
# `ubcv-buildings`, `ubcv-routes`, `ubcv-landscape` and `ubcv-context` were
# folded into the consolidated repo and left behind: their POI file has 437
# features against the current 489, buildings 270 against 449, trees 2,892
# against 9,324. Mirroring them would import stale duplicates of data already
# here. Layers the consolidation genuinely dropped -- elevation contours, public
# spaces, stairs and barriers as standalone geometry -- are a deliberate gap, not
# an oversight; add a repo here to pick them up.
REPOS = {
    "ubc-geospatial-opendata": "",
    "ubcv-parking": "ubcv/parking",
}

DATA_SUFFIXES = {".geojson", ".json", ".csv", ".kml", ".gpkg", ".zip"}
SKIP_PREFIXES = (".github/",)

# Top-level directories in the mirror map to campuses. `off-campus/` holds UBC
# sites elsewhere in BC and is not campus-specific, so it is always included.
CAMPUS_DIRS = {"ubcv": "vancouver", "ubco": "okanagan"}

# The pair of columns UBC writes coordinates into, and how many data rows to
# sample before deciding they are the wrong way round.
COORDINATE_COLUMNS = ("LAT", "LONG")
COORDINATE_SAMPLE = 400


def _median_of(rows: list[list[str]], index: int) -> float | None:
    values = []
    for row in rows:
        if index < len(row):
            try:
                values.append(float(row[index]))
            except (TypeError, ValueError):
                continue
    return statistics.median(values) if values else None


def _fix_swapped_coordinates(path: str, content: bytes) -> tuple[bytes, bool]:
    """Put `LAT` and `LONG` back on the right columns, if upstream swapped them.

    The test is arithmetic, not a list of filenames: a latitude cannot fall
    outside ±90, so a `LAT` column whose median is -123 is definitively not
    latitude, and a `LONG` column whose median is 49 definitively could be. Both
    conditions must hold, which is what keeps this away from the two files that
    look similar but are fine -- `ubcv_campus_trees.csv`, which is already the
    right way round, and `ubc_regional_locations.csv`, which is in a projected
    CRS where *both* numbers are out of latitude range and neither is a degree.

    Only the header line is rewritten. Swapping the values instead would mean
    re-encoding all 489 data rows through a CSV writer, and these files carry
    free-text HTML and contact fields whose quoting would not necessarily
    survive the round trip -- a cosmetic diff on every row is a poor trade for a
    fix that one line expresses exactly. The column order therefore reads
    `LONG,LAT` after the swap, which is unusual but correctly labelled; anything
    reading by name, which is the normal case, is unaffected.
    """
    if PurePosixPath(path).suffix.lower() != ".csv":
        return content, False

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content, False

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return content, False
    if not all(column in header for column in COORDINATE_COLUMNS):
        return content, False

    lat_at, long_at = (header.index(column) for column in COORDINATE_COLUMNS)
    sample = [row for _, row in zip(range(COORDINATE_SAMPLE), reader)]
    lat, long = _median_of(sample, lat_at), _median_of(sample, long_at)
    if lat is None or long is None:
        return content, False
    if not (abs(lat) > 90 and abs(long) <= 90):
        return content, False

    # Rewrite the header line alone, leaving every data byte where it was.
    header[lat_at], header[long_at] = header[long_at], header[lat_at]
    fixed = io.StringIO()
    csv.writer(fixed, lineterminator="").writerow(header)
    _, newline, rest = text.partition("\n")
    return (fixed.getvalue() + newline + rest).encode("utf-8"), True


@register
class Geospatial(Collector):
    name = "geospatial"
    title = "Campus geospatial open data"
    description = (
        "Buildings, building entrances, sub-buildings, complexes, addresses, points of "
        "interest, parking facilities with rates and permits, trees, hard/soft landscape, "
        "water features, roads, routes, bollards, land use, neighbourhoods and legal "
        "boundaries for UBC Vancouver, UBC Okanagan and off-campus UBC locations."
    )
    sources = tuple(f"https://github.com/{ORG}/{repo}" for repo in REPOS)

    def collect(self, http: Http, out: Output) -> None:
        index: list[dict[str, Any]] = []
        corrections: list[str] = []
        repositories: list[dict[str, Any]] = []

        for repo, mount in REPOS.items():
            repositories.append(self._mirror(http, out, repo, mount, index, corrections))

        out.table("_index", index)
        out.json(
            "_source.json",
            {
                "repositories": repositories,
                "retrieved_at": utcnow(),
                # Files this mirror deliberately differs from upstream on, and
                # how. Empty once UBC fixes it at source -- the check is on the
                # values, so nothing here needs editing when that happens.
                "corrections": {
                    "swapped_lat_long_headers": sorted(corrections),
                    "note": (
                        "Upstream labels these files' coordinate columns the wrong way "
                        "round: LAT held longitude and LONG held latitude. The two column "
                        "names are swapped here so each holds what it says; the data rows "
                        "are byte-identical to upstream, so the columns now read LONG,LAT "
                        "in that order. The GeoJSON versions were already correct and are "
                        "untouched."
                    ),
                } if corrections else {},
            },
        )

    def _mirror(
        self,
        http: Http,
        out: Output,
        repo: str,
        mount: str,
        index: list[dict[str, Any]],
        corrections: list[str],
    ) -> dict[str, Any]:
        """Copy one repository into the mirror, and report what it contributed.

        The campus filter is applied to the *destination* path rather than the
        upstream one, because a flat repo like `ubcv-parking` only says which
        campus it belongs to once it has been mounted -- its own paths start
        `csv/`, which names a format, not a campus.
        """
        slug = f"{ORG}/{repo}"
        meta = http.get_json(f"https://api.github.com/repos/{slug}")
        branch = meta.get("default_branch") or "master"
        head = http.get_json(f"https://api.github.com/repos/{slug}/commits/{branch}")
        sha = head["sha"]

        tree = http.get_json(
            f"https://api.github.com/repos/{slug}/git/trees/{sha}",
            params={"recursive": "1"},
        )
        if tree.get("truncated"):
            raise RuntimeError(f"GitHub truncated {slug}'s file tree; it outgrew one listing")

        mount_path = PurePosixPath(mount) if mount else None
        pairs: list[tuple[str, str]] = []
        for node in tree.get("tree", []):
            path = node.get("path", "")
            if node.get("type") != "blob" or path.startswith(SKIP_PREFIXES):
                continue
            if PurePosixPath(path).suffix.lower() not in DATA_SUFFIXES:
                continue
            dest = str(mount_path / path) if mount_path else path
            if not wants(CAMPUS_DIRS.get(PurePosixPath(dest).parts[0])):
                continue
            pairs.append((path, dest))

        def fetch(pair: tuple[str, str]) -> tuple[str, str, bytes]:
            path, dest = pair
            url = f"https://raw.githubusercontent.com/{slug}/{sha}/{path}"
            return path, dest, http.get(url).content

        for path, dest, content in http.map(fetch, pairs, workers=8):
            content, corrected = _fix_swapped_coordinates(dest, content)
            if corrected:
                corrections.append(dest)
            out.raw(dest, content, source=f"https://github.com/{slug}/blob/{sha}/{path}")

        for _, dest in pairs:
            parts = PurePosixPath(dest).parts
            index.append(
                {
                    "path": dest,
                    "campus": parts[0] if parts else "",
                    "theme": parts[1] if len(parts) > 1 else "",
                    "format": PurePosixPath(dest).suffix.lstrip(".").lower(),
                    "name": PurePosixPath(dest).stem,
                    "repository": repo,
                }
            )

        return {
            "repository": f"https://github.com/{slug}",
            "branch": branch,
            "commit": sha,
            "commit_date": (head.get("commit") or {}).get("committer", {}).get("date"),
            "mounted_at": mount or "/",
            "files": len(pairs),
        }
