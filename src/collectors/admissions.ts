/** Admissions: the undergraduate degree/program finder and its supporting pages.
 *
 * Source: you.ubc.ca. The program finder is a client-side app whose full dataset
 * is inlined into /programs/ as `var programs = [...]` and `var degrees = {...}`,
 * so we parse those literals rather than driving the search UI. Admission
 * requirement, cost and deadline content lives in the WP REST pages collection.
 *
 * The requirements themselves are a separate exercise -- see `admissionreqs`.
 * What a given program demands of you depends on where you went to school, so
 * they are not on the program page at all: the page ships a dropdown of provinces
 * and countries and fetches the matching rules over AJAX. That module drives the
 * same endpoint and lands the answers as tables.
 */

import type { Http, Output } from "../base.ts";
import { jsLiteral, register, wants, wpCollection } from "../base.ts";
import { collect as collectAdmissionreqs, ENDPOINT as REQS_ENDPOINT } from "./admissionreqs.ts";

export const HOST = "you.ubc.ca";
export const PROGRAMS_URL = `https://${HOST}/programs/`;

export const VAR_RE = /\bvar\s+([A-Za-z_$][\w$]*)\s*=\s*[\[{]/g;

// Front-end plumbing that happens to be declared the same way as the data.
export const IGNORE_VARS = /(Loc|Settings|Bundle|Vars|Strings)$|recaptcha|ajax|dataLayer|analytics|shim|nonce/i;

export const TAXONOMIES = ["program-areas", "student-groups", "ubc-life-topics", "cycle-stages", "content-types"];

// Written as a nested object rather than a row per program, so flattening it to
// rows the way every other literal here is flattened produces four unlabelled
// blobs. `finances` reshapes it properly; keep the original next to that.
export const NESTED_VARS = new Set(["costEstimatorData"]);

export function campusIds(campuses: Array<AnyJson>): Set<string> {
  /** The campus term ids in scope (9 = Vancouver, 10 = Okanagan). */
  const ids = new Set<string>();
  for (const term of campuses) {
    if (
      wants(
        String(term["name"] ?? "")
          .trim()
          .toLowerCase() || null,
      )
    ) {
      ids.add(String(term["term_id"]));
    }
  }
  return ids;
}

export function onCampus(programs: Array<AnyJson>, wantedIds: Set<string>): Array<AnyJson> {
  /** Programs offered on a campus in scope; one offered on both is kept. */
  if (wantedIds.size === 0) return programs;
  const kept: Array<AnyJson> = [];
  for (const program of programs) {
    const raw = program["campuses"];
    const ids = Array.isArray(raw) ? raw : raw != null ? [raw] : [];
    if (ids.length === 0 || ids.some((id: unknown) => wantedIds.has(String(id)))) kept.push(program);
  }
  return kept;
}

export function filterByCampus(datasets: Record<string, Array<AnyJson>>): void {
  /** Drop programs not offered on the selected campus. */
  const campuses = datasets["campuses"] ?? [];
  const wantedIds = campusIds(campuses);
  if (wantedIds.size === 0) return;

  datasets["campuses"] = campuses.filter((c) => wantedIds.has(String(c["term_id"])));
  datasets["programs"] = onCampus(datasets["programs"] ?? [], wantedIds);
}

export function asRows(value: unknown): Array<AnyJson> {
  /** Normalise either a list or an index-keyed object into a list of rows. */
  if (Array.isArray(value)) {
    return value.filter((row): row is AnyJson => row !== null && typeof row === "object" && !Array.isArray(row));
  }
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    return Object.values(value).filter(
      (row): row is AnyJson => row !== null && typeof row === "object" && !Array.isArray(row),
    );
  }
  return [];
}

type AnyJson = Record<string, unknown>;

export const Admissions = register(
  class {
    name = "admissions";
    folder = "admissions";
    title = "Admissions: degree and program finder";
    description =
      "Every undergraduate program in UBC's program finder with its degree, campus, " +
      "faculty, areas of interest and links, the degree list, and the you.ubc.ca " +
      "content pages covering admission requirements, costs, deadlines and how to apply.";
    sources = [PROGRAMS_URL, `https://${HOST}/wp-json/wp/v2/pages`, REQS_ENDPOINT];

    async collect(http: Http, out: Output): Promise<void> {
      const html = await http.getText(PROGRAMS_URL);

      const datasets: Record<string, Array<AnyJson>> = {};
      const names = [...new Set([...html.matchAll(VAR_RE)].map((m) => m[1]!))];
      for (const name of names) {
        if (IGNORE_VARS.test(name) || NESTED_VARS.has(name)) continue;
        try {
          const rows = asRows(jsLiteral(html, name));
          if (rows.length > 0) datasets[name] = rows;
        } catch {
          continue;
        }
      }

      if (datasets["programs"] === undefined) {
        throw new Error("no `programs` dataset found on the program finder page");
      }

      filterByCampus(datasets);

      out.describe("programs", {
        grain: "one undergraduate program in the program finder",
        columns: {
          id: "you.ubc.ca post id -- the key everything else here joins on",
          post_title: "program name",
          link: "the program page",
          summary: "one-paragraph description",
          degrees: "degree term ids; resolve against degrees.json",
          campuses: "campus term ids (9 = Vancouver, 10 = Okanagan)",
          interests: "interest term ids; resolve against interests.json",
          topics: "topic term ids; resolve against topics.json",
          duration: "nominal length, e.g. {amount: 4, unit: years}",
        },
        joins: [
          "id -> admissions/requirements/program_requirements.program_id",
          "id -> finances/program_cost_estimates.program_id",
          "degrees -> admissions/degrees.term_id",
          "campuses -> admissions/campuses.term_id",
        ],
      });
      for (const [name, rows] of Object.entries(datasets)) {
        await out.table(name, rows, { source: PROGRAMS_URL });
      }

      const pages = (await wpCollection(http, HOST, "wp/v2/pages", {
        params: { _embed: "0" },
      })) as Array<AnyJson>;
      await out.table("pages", pages, { source: `https://${HOST}/wp-json/wp/v2/pages` });

      for (const taxonomy of TAXONOMIES) {
        let terms: Array<AnyJson>;
        try {
          terms = (await wpCollection(http, HOST, `wp/v2/${taxonomy}`)) as Array<AnyJson>;
        } catch {
          continue;
        }
        if (terms.length > 0) {
          await out.table(`taxonomies/${taxonomy}`, terms, {
            source: `https://${HOST}/wp-json/wp/v2/${taxonomy}`,
          });
        }
      }

      const degrees: Record<string, string> = {};
      for (const term of datasets["degrees"] ?? []) {
        degrees[String(term["term_id"])] = String(term["name"] ?? "");
      }
      await collectAdmissionreqs(http, out, {
        programs: datasets["programs"],
        degrees,
      });
    }
  },
);
