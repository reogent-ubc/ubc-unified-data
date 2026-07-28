# Unified UBC Data

A dataset of public institutional data about the University of British Columbia,
gathered from official UBC sources and kept in plain JSON/CSV/GeoJSON files.

The goal is the stuff that helps students: where things are, what's taught, when
it's taught, where you can sit down and study, what it costs, who teaches it,
and what's happening on campus. **No student data** — no grades, no historical
class sizes, no individual records.

```
Unified-UBC-Data/
├── collectors/     # code that gathers the data
├── data/           # the data itself
└── update.py       # the one command that refreshes everything
```

## Update the data

```bash
pip install -r requirements.txt
python update.py
```

That's it. Everything under `data/` is rebuilt and `data/manifest.json` is
regenerated with per-dataset record counts, sizes, sources and timestamps.

```bash
python update.py --list              # what's available
python update.py courses events      # refresh just these groups
python update.py --skip people       # everything except one group
python update.py --min-interval 0.2  # go easier on the source servers
```

A group that fails is reported and recorded in the manifest, but the rest of the
run continues. Exit code is non-zero if anything failed.

## What's in here

| Group | Folder | What you get |
|---|---|---|
| `geospatial` | `data/geospatial/` | Buildings, entrances, sub-buildings, complexes, addresses, points of interest, campus trees, hard/soft landscape, water features, roads, routes, bollards, land use, neighbourhoods, legal boundaries — Vancouver, Okanagan and off-campus. GeoJSON + CSV, mirrored verbatim. |
| `courses` | `data/courses/` | Two tables. `courses.*` is the catalogue (code, number, title, credits, subject, description). `sections.*` is every **actual offering** — term, meeting days and times, start/end dates, instructor, delivery mode, status. Plus subject, campus, term, year-level and department code lists, and standard timetables. |
| `calendar` | `data/academic-calendar/` | The course *catalogue* for both campuses: descriptions and credit values, plus prerequisites, corequisites, equivalencies, credit exclusions and hours vectors **parsed out of the description prose** (see below) — and the faculty / school / department hierarchy. |
| `spaces` | `data/learning-spaces/` | Bookable classrooms and informal study spaces with capacity, building, room number, furniture and layout style, photos and links. `filters.json` holds the full vocabulary UBC records per space (A/V, accessibility, room features). |
| `events` | `data/events/` | Public events with times, cost, description and category, plus venues (street address, city, postal code) and organizers (email, phone, website). Includes the raw iCal feed. Venues have no coordinates — join them to `geospatial/ubcv/locations/` on address or building to place them on a map. |
| `admissions` | `data/admissions/` | Every undergraduate program in the program finder with degree, campus, faculty and areas of interest; the degree list; the cost-estimator data; and the you.ubc.ca pages on requirements, costs, deadlines and how to apply. |
| `people` | `data/people/` | Public faculty and staff profiles, tagged with the site each came from. **Partial coverage:** only 5 of 43 probed UBC sites expose profiles over an API — Applied Science, Nursing, Law, Pharmaceutical Sciences and Science (1,712 profiles). The rest (Arts, Sauder, Medicine, Forestry, Education, Grad Studies…) aren't on the shared Drupal platform and return no `/jsonapi`. `_sites.json` records exactly which host gave what. |
| `services` | `data/campus-services/` | Food outlets, parking locations, permits and parking maps, facilities and learning-space resource libraries, recreation and student-services pages, UBC news. |
| `reports` | `data/reports/` | An index of UBC's published institutional documents — annual financial reports, budgets, tuition and fee schedules, annual enrolment reports, Facts & Figures — each with a direct download URL, file type and source page. |

### Campus coverage

Vancouver and Okanagan are handled differently per group, because the upstream
sources handle them differently. There is no single `campus` column across the
dataset — this table is the map.

| Group | How campus is represented |
|---|---|
| `geospatial` | **Separate directories** — `ubcv/` (45 files), `ubco/` (10), `off-campus/` (3) |
| `calendar` | **Separate directories** — `vancouver/` (9,491 courses), `okanagan/` (2,513), plus a `campus` column on every row |
| `courses` | **One combined file.** No `campus` column. Derive it from the course-code suffix — `_V` (38,529 rows) or `_O` (8,700) — or join `terms.json`, whose names carry `(UBC-V)`/`(UBC-O)`, 5 terms each. `campuses.json` holds the code list (`UBCV`, `UBCO`). |
| `admissions` | `campuses` field holds term ids; resolve against `campuses.json` → `9` = Vancouver (211 programs), `10` = Okanagan (113) |
| `spaces` | `campus` column, but **Vancouver only** — all 411 rows. The Find a Space tool has no Okanagan data despite accepting a campus parameter. |
| `events` | No campus field. Venue `city` is the closest proxy (189 Vancouver, 1 Kelowna). |
| `people` / `services` / `reports` | Organised by source site, not campus. PAIR publishes separate `Factsheets_UBCV`/`Factsheets_UBCO` documents. |

### File conventions

- Tabular datasets are written **twice**: `name.json` (full fidelity, nested
  values intact) and `name.csv` (same rows, nested values JSON-encoded into the
  cell). Use whichever suits your tooling.
- Files beginning with `_` are about the data rather than data itself:
  `_index`, `_source.json`, `_sources.json`, `_configs.json`, `_unavailable.json`.
- Geospatial files keep the upstream repository's directory layout
  (`ubcv/`, `ubco/`, `off-campus/`) so paths match UBC's own documentation, and
  each theme ships a `metadata/` CSV defining its fields.
- `data/manifest.json` is the index of everything: every file, its record count,
  byte size, source URL and when it was last refreshed.

## Sources

Everything comes from a public UBC endpoint. No credentials, no scraping behind
a login, no student data.

| Source | Used for |
|---|---|
| [UBCGeodata/ubc-geospatial-opendata](https://github.com/UBCGeodata/ubc-geospatial-opendata) | `geospatial` — pinned to the current commit SHA, recorded in `_source.json` |
| [courses.students.ubc.ca](https://courses.students.ubc.ca/) Drupal JSON:API | `courses` |
| [vancouver.calendar.ubc.ca](https://vancouver.calendar.ubc.ca/) / [okanagan.calendar.ubc.ca](https://okanagan.calendar.ubc.ca/) JSON:API | `calendar` |
| [learningspaces.ubc.ca Find a Space](https://learningspaces.ubc.ca/find-a-space/) | `spaces` |
| [events.ubc.ca](https://events.ubc.ca/resources/webdev/) Events Calendar REST API | `events` |
| [you.ubc.ca](https://you.ubc.ca/programs/) program finder + WP REST | `admissions` |
| UBC faculty/unit sites on the shared Drupal platform | `people` |
| food.ubc.ca, parking.ubc.ca, facilities.ubc.ca, students.ubc.ca, recreation.ubc.ca, news.ubc.ca | `services` |
| finance.ubc.ca, [pair.ubc.ca](https://pair.ubc.ca/) (Planning, Analytics & Institutional Research) | `reports` |

### Notes on a few of them

- **Course Schedule vs Academic Calendar.** The Course Schedule (`courses`) is
  what is *actually offered and when*. The Academic Calendar (`calendar`) is
  what a course *is* — description, prerequisites, credits. Join them on
  subject + course number.
- **Prerequisites are prose, not a field.** The Academic Calendar schema *has*
  `field_course_prerequisite`, `field_course_co_requistite`,
  `field_course_vector` and `field_course_equivalency` — and all four are empty
  on every record sampled (0/350). UBC writes the whole lot into
  `field_course_description`, exactly as its own pages render it:

  > Physical and mathematical structures of computation. … sequential
  > instruction execution. **[3-2-1] Prerequisite:** Principles of Mathematics 12
  > or Pre-calculus 12. **Corequisite:** One of CPSC 107, CPSC 110.

  So `collectors/coursetext.py` parses it back out into `prerequisite`,
  `corequisite`, `equivalency`, `credit_exclusion`, `hours_vector` and a cleaned
  `description_text`. These columns are **derived, not fetched** — the original
  description is always kept next to them so you can re-parse or spot-check.
  The same parser runs over the Course Schedule's `body` field, which carries
  prerequisite prose on about 40% of courses.
- **Instructors live on sections, not courses.** `courses/sections.json` has
  `field_instructors` populated on every row sampled, along with `field_days`,
  `field_start_time`/`field_end_time` and start/end dates. `courses/courses.json`
  has no instructor field at all — join sections to courses via
  `related.course`. Times are **seconds after midnight** (`55800` = 15:30).
- **One course node per offering-year.** `node/course` has ~47k rows because
  UBC creates an instance per course per academic year (`field_course_instance_id`),
  not ~12k unique courses. Deduplicate on `field_course_code` if you want the
  distinct course list.
- **Drupal JSON:API caps `page[limit]` at 50**, so the ~47k courses and ~44k
  sections take roughly 1,800 requests. The collector fans offsets out across
  threads when the server reports a total, and falls back to walking
  `links.next` when it doesn't.
- **Find a Space** is driven by a `data-config` blob on the page that mounts the
  app (`{"campus": ..., "formal": true}`). The collector discovers those configs
  by scanning the site's pages instead of hard-coding them, so an added campus
  or tab is picked up automatically. Its nonce is scraped fresh each run.
- **Finance and institutional research are not an API.** UBC publishes them as
  PDF/CSV/XLSX attached to pages, so `reports` builds an *index* of those
  documents with resolved download URLs rather than pretending there is
  structured financial data to fetch.

## Adding a source

Drop a module in `collectors/` with a registered `Collector` subclass, and add
it to the imports in `collectors/__init__.py`:

```python
from .base import Collector, Http, Output, register, wp_collection

@register
class Housing(Collector):
    name = "housing"           # CLI name: python update.py housing
    folder = "housing"         # output dir: data/housing/
    title = "Student housing"
    description = "Residences, room types and rates."
    sources = ("https://vancouver.housing.ubc.ca",)

    def collect(self, http: Http, out: Output) -> None:
        residences = wp_collection(http, "vancouver.housing.ubc.ca", "wp/v2/pages")
        out.table("residences", residences)
```

`collectors/base.py` already provides the pieces you'll want: `Http` (retries,
backoff, throttling, threaded `map`), `jsonapi_collection` and `jsonapi_index`
for Drupal sites, `wp_collection` for WordPress sites, and `Output.table` /
`.json` / `.csv` / `.raw` for writing. Anything written through `Output` shows
up in the manifest automatically.

## Licence and reuse

These are just scrapers — do whatever you want with the code. The data is a
different matter: it belongs to UBC and its terms apply, so check the source
before redistributing, particularly the
[geospatial repository's licence](https://github.com/UBCGeodata/ubc-geospatial-opendata)
and UBC's site terms. Be a good citizen with the endpoints: `--min-interval`
exists for a reason.
