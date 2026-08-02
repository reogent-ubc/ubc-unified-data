"""Faculty and staff profiles published by UBC faculties, schools and units.

Source: the `node--ubc_profile` resource on the shared UBC Drupal platform.
Most faculty and unit sites run on it, so we probe a list of known hosts and
take profiles from whichever expose them -- names, titles, units and profile
URLs, as each unit publishes them.

This is public directory-style information only. No student data is collected.
"""

from __future__ import annotations

from typing import Any

from .base import Collector, Http, Output, jsonapi_collection, jsonapi_index, register

# UBC units known to run the shared Drupal platform. Hosts that are absent,
# offline or not on the platform are skipped without failing the run.
HOSTS = (
    "science.ubc.ca",
    "apsc.ubc.ca",
    "arts.ubc.ca",
    "grad.ubc.ca",
    "forestry.ubc.ca",
    "landfood.ubc.ca",
    "sauder.ubc.ca",
    "educ.ubc.ca",
    "law.ubc.ca",
    "nursing.ubc.ca",
    "pharmsci.ubc.ca",
    "dentistry.ubc.ca",
    "med.ubc.ca",
    "spph.ubc.ca",
    "sppga.ubc.ca",
    "music.ubc.ca",
    "sala.ubc.ca",
    "ires.ubc.ca",
    "chbe.ubc.ca",
    "civil.ubc.ca",
    "ece.ubc.ca",
    "mech.ubc.ca",
    "cs.ubc.ca",
    "math.ubc.ca",
    "stat.ubc.ca",
    "phas.ubc.ca",
    "chem.ubc.ca",
    "zoology.ubc.ca",
    "botany.ubc.ca",
    "eoas.ubc.ca",
    "economics.ubc.ca",
    "psych.ubc.ca",
    "geog.ubc.ca",
    "history.ubc.ca",
    "english.ubc.ca",
    "philosophy.ubc.ca",
    "linguistics.ubc.ca",
    "anth.ubc.ca",
    "soci.ubc.ca",
    "politics.ubc.ca",
    "library.ubc.ca",
    "parking.ubc.ca",
    "courses.students.ubc.ca",
)

RESOURCE = "node/ubc_profile"


@register
class People(Collector):
    name = "people"
    title = "Faculty and staff profiles by unit"
    description = (
        "Public faculty and staff profiles from UBC units on the shared Drupal "
        "platform, tagged with the site they came from. Coverage is partial -- most "
        "faculties run other platforms and expose no API, so `_sites.json` records "
        "which hosts answered. Public directory information only -- no student data."
    )
    sources = tuple(f"https://{host}/jsonapi/{RESOURCE}" for host in HOSTS[:3]) + ("...",)

    def collect(self, http: Http, out: Output) -> None:
        def gather(host: str) -> tuple[str, list[dict[str, Any]], str | None]:
            try:
                if RESOURCE.replace("/", "--") not in jsonapi_index(http, host):
                    return host, [], "no ubc_profile resource"
                records = jsonapi_collection(http, host, RESOURCE, workers=4)
            except Exception as error:
                return host, [], f"{type(error).__name__}: {error}"
            for record in records:
                record["site"] = host
            return host, records, None

        results = http.map(gather, HOSTS, workers=8)

        profiles: list[dict[str, Any]] = []
        sites: list[dict[str, Any]] = []
        for host, records, error in results:
            profiles.extend(records)
            sites.append({"site": host, "profiles": len(records), "skipped": error})

        out.describe(
            "profiles",
            grain="one published faculty or staff profile on one UBC site",
            columns={
                "title": "the person's name as the site publishes it",
                "field_profile_job_title": "their title in the unit",
                "field_profile_email": "public contact email",
                "field_profile_phone": "public contact phone",
                "field_profile_office": "office location, where published",
                "body": "biography HTML",
                "site": "which UBC host published it -- the only unit marker there is",
                "alias": "path on that host; prefix the host for the URL",
            },
            joins=[
                "title ~ courses/sections.field_instructors (by name; there is no shared id)",
                "site -> people/_sites.site",
            ],
        )
        out.describe(
            "_sites",
            grain="one host that was probed for profiles, whether or not it had any",
            columns={
                "site": "the host",
                "profiles": "how many it returned; 0 means it is not on the shared platform",
                "skipped": "why nothing came back, when nothing did",
            },
            joins=["site -> people/profiles.site"],
        )
        out.table("profiles", profiles)
        out.table("_sites", sites)
