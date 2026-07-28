"""Campus geospatial data: buildings, addresses, trees, roads, land use, POIs.

Source: the UBCGeodata/ubc-geospatial-opendata GitHub repository, which UBC
Campus + Community Planning publishes as the authoritative wayfinding and
operational geometry for both campuses. Files are mirrored verbatim so the
GeoJSON stays valid and the accompanying field-definition CSVs come along.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from .base import Collector, Http, Output, register, utcnow, wants

REPO = "UBCGeodata/ubc-geospatial-opendata"
BRANCH = "master"
DATA_SUFFIXES = {".geojson", ".json", ".csv", ".kml", ".gpkg", ".zip"}
SKIP_PREFIXES = (".github/",)

# Top-level directories in the repo map to campuses. `off-campus/` holds UBC
# sites elsewhere in BC and is not campus-specific, so it is always included.
CAMPUS_DIRS = {"ubcv": "vancouver", "ubco": "okanagan"}


@register
class Geospatial(Collector):
    name = "geospatial"
    title = "Campus geospatial open data"
    description = (
        "Buildings, building entrances, sub-buildings, complexes, addresses, points of "
        "interest, trees, hard/soft landscape, water features, roads, routes, bollards, "
        "land use, neighbourhoods and legal boundaries for UBC Vancouver, UBC Okanagan "
        "and off-campus UBC locations."
    )
    sources = (f"https://github.com/{REPO}",)

    def collect(self, http: Http, out: Output) -> None:
        head = http.get_json(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}")
        sha = head["sha"]

        tree = http.get_json(
            f"https://api.github.com/repos/{REPO}/git/trees/{sha}",
            params={"recursive": "1"},
        )
        if tree.get("truncated"):
            raise RuntimeError("GitHub truncated the file tree; repo grew past a single listing")

        paths = [
            node["path"]
            for node in tree.get("tree", [])
            if node.get("type") == "blob"
            and PurePosixPath(node["path"]).suffix.lower() in DATA_SUFFIXES
            and not node["path"].startswith(SKIP_PREFIXES)
            and wants(CAMPUS_DIRS.get(PurePosixPath(node["path"]).parts[0]))
        ]

        def fetch(path: str) -> tuple[str, bytes]:
            url = f"https://raw.githubusercontent.com/{REPO}/{sha}/{path}"
            return path, http.get(url).content

        for path, content in http.map(fetch, paths, workers=8):
            out.raw(path, content, source=f"https://github.com/{REPO}/blob/{sha}/{path}")

        index: list[dict[str, Any]] = []
        for path in paths:
            parts = PurePosixPath(path).parts
            index.append(
                {
                    "path": path,
                    "campus": parts[0] if parts else "",
                    "theme": parts[1] if len(parts) > 1 else "",
                    "format": PurePosixPath(path).suffix.lstrip(".").lower(),
                    "name": PurePosixPath(path).stem,
                }
            )
        out.table("_index", index)
        out.json(
            "_source.json",
            {
                "repository": f"https://github.com/{REPO}",
                "branch": BRANCH,
                "commit": sha,
                "commit_date": (head.get("commit") or {}).get("committer", {}).get("date"),
                "files": len(paths),
                "retrieved_at": utcnow(),
            },
        )
