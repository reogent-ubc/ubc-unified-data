import { describe, expect, it } from "vitest";
import { BUCKETS, gradeRow, parseCSV } from "./grades.ts";

const HEADER =
  "Campus,Year,Session,Subject,Course,Detail,Section,Title,Professor,Reported,Avg,Median,Percentile (25),Percentile (75),High,Low,<50,50-54,55-59,60-63,64-67,68-71,72-75,76-79,80-84,85-89,90-100";

describe("parseCSV", () => {
  it("parses rows, skipping blank lines", () => {
    const rows = parseCSV(
      `${HEADER}\r\nUBCV,2021,S,ACAM,390,A,001,Title,Henry Yu,20,93.9,94.5,91.5,97.3,100.0,85.0,,,,,,,,,,,16`,
    );
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ Subject: "ACAM", Course: "390", Section: "001", Avg: "93.9" });
  });

  it("handles quoted fields", () => {
    const rows = parseCSV(`${HEADER}\r\nUBCV,2021,S,APBI,100,,001,"Trees, Soils and People",Alice,20,80,,,,,,,,,,,,,`);
    expect(rows[0]).toMatchObject({ Title: "Trees, Soils and People", Reported: "20" });
  });
});

describe("gradeRow", () => {
  const base = {
    Campus: "UBCV",
    Year: "2021",
    Session: "S",
    Subject: "ACAM",
    Course: "390",
    Section: "001",
    Title: "T",
    Professor: "P",
    Reported: "20",
    Avg: "93.9",
    Median: "",
    "Percentile (25)": "",
    "Percentile (75)": "",
    High: "100.0",
    Low: "85.0",
  };

  it("derives a distribution table row", () => {
    const row = gradeRow({
      ...base,
      "<50": "",
      "50-54": "",
      "55-59": "1",
      "60-63": "",
      "64-67": "",
      "68-71": "",
      "72-75": "",
      "76-79": "",
      "80-84": "",
      "85-89": "",
      "90-100": "16",
    })!;
    expect(row).toMatchObject({
      subject: "ACAM",
      course: "390",
      section: "001",
      year: 2021,
      session: "S",
      enrolled: 20,
      avg: 93.9,
      median: null,
      high: 100,
      low: 85,
    });
    expect(row["distribution"]).toEqual({
      ...Object.fromEntries(BUCKETS.map((b) => [b, 0])),
      "55-59": 1,
      "90-100": 16,
    });
  });

  it("skips OVERALL and cross-listed rows", () => {
    expect(gradeRow({ ...base, Section: "OVERALL" })).toBeNull();
    expect(gradeRow({ ...base, Campus: "UBCO" })).toBeNull();
    expect(gradeRow({ ...base, Detail: "D" })).toBeNull();
  });
});
