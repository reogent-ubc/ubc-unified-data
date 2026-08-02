"""Campus services students actually use: food outlets, parking, facilities info.

Sources are a mix of Drupal JSON:API and WordPress REST endpoints across UBC
service sites. Each dataset is optional -- a site that moves or retires an
endpoint is recorded in `_sources.json` rather than failing the run.
"""

from __future__ import annotations

from typing import Any, Callable

from . import holidays
from .base import (
    Collector,
    Http,
    Output,
    jsonapi_collection,
    jsonapi_index,
    register,
    wp_collection,
)


def _drupal(host: str, resource: str) -> Callable[[Http], list[dict[str, Any]]]:
    def fetch(http: Http) -> list[dict[str, Any]]:
        if resource.replace("/", "--") not in jsonapi_index(http, host):
            raise LookupError(f"{host} does not expose {resource}")
        return jsonapi_collection(http, host, resource)

    fetch.source = f"https://{host}/jsonapi/{resource}"  # type: ignore[attr-defined]
    return fetch


def _wordpress(host: str, path: str) -> Callable[[Http], list[dict[str, Any]]]:
    def fetch(http: Http) -> list[dict[str, Any]]:
        return wp_collection(http, host, path)

    fetch.source = f"https://{host}/wp-json/{path}"  # type: ignore[attr-defined]
    return fetch


DATASETS: dict[str, Callable[[Http], list[dict[str, Any]]]] = {
    "food_outlets": _wordpress("food.ubc.ca", "wp/v2/places"),
    "food_events": _wordpress("food.ubc.ca", "wp/v2/event"),
    "parking_locations": _drupal("parking.ubc.ca", "node/parking_location"),
    "parking_permits": _drupal("parking.ubc.ca", "node/permits"),
    "parking_maps": _drupal("parking.ubc.ca", "node/map_page"),
    "facilities_resources": _wordpress("facilities.ubc.ca", "wp/v2/resources"),
    "learning_space_resources": _wordpress("learningspaces.ubc.ca", "wp/v2/resources"),
    "recreation_pages": _wordpress("recreation.ubc.ca", "wp/v2/pages"),
    "student_services_pages": _wordpress("students.ubc.ca", "wp/v2/pages"),
    "news": _wordpress("news.ubc.ca", "wp/v2/posts"),
    # The one entry here that isn't an API: HR publishes the holiday list as a
    # page, so `holidays` reads the tables off it.
    "statutory_holidays": holidays.fetch,
}


@register
class Services(Collector):
    name = "services"
    folder = "campus-services"
    title = "Campus services (food, parking, facilities, student services, news)"
    description = (
        "Food outlets and their hours, parking locations, permits and parking maps, "
        "facilities and learning-space resource libraries, recreation and student "
        "services pages, UBC news posts, and the statutory holidays UBC observes."
    )
    sources = (
        "https://food.ubc.ca",
        "https://parking.ubc.ca",
        "https://facilities.ubc.ca",
        holidays.URL,
    )

    def collect(self, http: Http, out: Output) -> None:
        status: list[dict[str, Any]] = []

        for name, fetch in DATASETS.items():
            source = getattr(fetch, "source", None)
            try:
                records = fetch(http)
            except Exception as error:
                status.append({"dataset": name, "source": source, "records": 0,
                               "skipped": f"{type(error).__name__}: {error}"})
                continue
            if records:
                out.table(name, records, source=source)
            status.append({"dataset": name, "source": source, "records": len(records),
                           "skipped": None})

        out.json("_sources.json", status)
