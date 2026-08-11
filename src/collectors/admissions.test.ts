import { describe, expect, it } from "vitest";
import { filterByCampus, programFinderDatasets } from "./admissions.ts";

const FINDER_HTML = `<script>
var programsListData = {
  "programs": [
    { "id": 149486, "title": "Ancient Mediterranean and Near Eastern Studies",
      "link": "https://you.ubc.ca/programs/ancient-mediterranean-near-eastern-studies/",
      "overview": "Study the ancient world.", "is_new": false,
      "degrees": ["bachelor-of-arts"], "campuses": [{ "slug": "vancouver", "name": "Vancouver" }],
      "topics": ["history-law-and-politics"], "interests": ["history"] },
    { "id": 149487, "title": "Okanagan Science", "link": "https://you.ubc.ca/programs/okanagan-science/",
      "overview": "Science in the Okanagan.", "is_new": false,
      "degrees": ["bachelor-of-science"], "campuses": [{ "slug": "okanagan", "name": "Okanagan" }],
      "topics": ["health-and-life-sciences"], "interests": ["science"] }
  ],
  "taxonomies": {
    "campuses": [ { "slug": "vancouver", "name": "Vancouver" }, { "slug": "okanagan", "name": "Okanagan" } ],
    "degree": [ { "slug": "bachelor-of-arts", "name": "Bachelor of Arts" },
                { "slug": "bachelor-of-science", "name": "Bachelor of Science" } ],
    "topic_areas": [ { "slug": "history-law-and-politics", "name": "History, law and politics" },
                     { "slug": "health-and-life-sciences", "name": "Health and life sciences" } ],
    "interests": [ { "slug": "history", "name": "History" }, { "slug": "science", "name": "Science" } ]
  }
};
var f = d.getElementsByTagName(s)[0],
</script>`;

describe("programFinderDatasets", () => {
  it("reshapes the slug-keyed literal into the old per-dataset shape", () => {
    const datasets = programFinderDatasets(FINDER_HTML);
    expect(datasets).not.toBeNull();
    expect(datasets!["programs"]).toHaveLength(2);
    expect(datasets!["programs"][0]).toMatchObject({
      id: 149486,
      post_title: "Ancient Mediterranean and Near Eastern Studies",
      link: "https://you.ubc.ca/programs/ancient-mediterranean-near-eastern-studies/",
      summary: "Study the ancient world.",
    });

    const [degree, campus, topic, interest] = [
      datasets!["degrees"][0],
      datasets!["campuses"][0],
      datasets!["topics"][0],
      datasets!["interests"][0],
    ];
    expect(degree).toMatchObject({ name: "Bachelor of Arts", slug: "bachelor-of-arts" });
    expect(campus).toMatchObject({ name: "Vancouver", slug: "vancouver" });
    expect(topic).toMatchObject({ name: "History, law and politics" });
    expect(interest).toMatchObject({ name: "History", slug: "history" });

    // The ids the program row references come from the same slug mapping.
    const program = datasets!["programs"][0];
    expect(program["degrees"]).toEqual([degree["term_id"]]);
    expect(program["campuses"]).toEqual([campus["term_id"]]);
    expect(program["topics"]).toEqual([topic["term_id"]]);
    expect(program["interests"]).toEqual([interest["term_id"]]);
  });

  it("returns null when the page has no programsListData", () => {
    expect(programFinderDatasets("<html></html>")).toBeNull();
  });
});

describe("filterByCampus", () => {
  it("drops programs from campuses outside the run's scope", () => {
    const datasets = programFinderDatasets(FINDER_HTML);
    filterByCampus(datasets!);
    expect(datasets!["programs"]).toHaveLength(1);
    expect(datasets!["programs"][0]["id"]).toBe(149486);
    expect(datasets!["campuses"]).toHaveLength(1);
  });
});
