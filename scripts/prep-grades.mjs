import { mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const ROOT = new URL("..", import.meta.url).pathname;
const V2_DIR = join(ROOT, "data/grades/raw");
const OUT = join(ROOT, "data/grades/distributions.json");

const BUCKETS = ["<50", "50-54", "55-59", "60-63", "64-67", "68-71", "72-75", "76-79", "80-84", "85-89", "90-100"];

function parseCSV(text) {
  // Simple CSV parse: split on newlines, handle quoted fields
  const lines = text.split(/\r?\n/).filter((l) => l.trim());
  if (lines.length < 2) return [];
  const header = splitCSVLine(lines[0]);
  return lines.slice(1).map((line) => {
    const vals = splitCSVLine(line);
    const row = {};
    header.forEach((h, i) => {
      row[h] = vals[i] ?? "";
    });
    return row;
  });
}

function splitCSVLine(line) {
  const fields = [];
  let i = 0;
  while (i <= line.length) {
    if (line[i] === '"') {
      let val = "";
      i++; // skip opening quote
      while (i < line.length) {
        if (line[i] === '"' && line[i + 1] === '"') {
          val += '"';
          i += 2;
        } else if (line[i] === '"') {
          i++;
          break;
        } else {
          val += line[i];
          i++;
        }
      }
      fields.push(val);
      i++; // skip comma
    } else {
      const next = line.indexOf(",", i);
      if (next === -1) {
        fields.push(line.slice(i));
        break;
      }
      fields.push(line.slice(i, next));
      i = next + 1;
    }
  }
  return fields;
}

function toInt(v) {
  const n = parseInt(v, 10);
  return Number.isNaN(n) ? null : n;
}
function toFloat(v) {
  const n = parseFloat(v);
  return Number.isNaN(n) ? null : n;
}

function processV2(csvDir) {
  const rows = [];
  for (const session of readdirSync(csvDir)) {
    const dir = join(csvDir, session);
    for (const file of readdirSync(dir).filter((f) => f.endsWith(".csv"))) {
      const text = readFileSync(join(dir, file), "utf-8");
      for (const r of parseCSV(text)) {
        if (r.Campus !== "UBCV") continue;
        if (r.Section === "OVERALL") continue;
        if (r.Detail?.trim()) continue;
        const dist = {};
        for (const b of BUCKETS) dist[b] = toInt(r[b]) ?? 0;
        rows.push({
          subject: r.Subject,
          course: r.Course,
          section: r.Section,
          year: toInt(r.Year),
          session: r.Session,
          title: r.Title,
          professor: r.Professor,
          enrolled: toInt(r.Reported),
          avg: toFloat(r.Avg),
          std_dev: null,
          median: toFloat(r.Median) ?? null,
          percentile_25: toFloat(r["Percentile (25)"]) ?? null,
          percentile_75: toFloat(r["Percentile (75)"]) ?? null,
          high: toInt(r.High) ?? toFloat(r.High),
          low: toInt(r.Low) ?? toFloat(r.Low),
          distribution: dist,
        });
      }
    }
  }
  return rows;
}

const rows = processV2(V2_DIR);

rows.sort(
  (a, b) =>
    a.year - b.year ||
    a.session.localeCompare(b.session) ||
    a.subject.localeCompare(b.subject) ||
    a.course.localeCompare(b.course) ||
    a.section.localeCompare(b.section),
);

mkdirSync(join(ROOT, "data/grades"), { recursive: true });
writeFileSync(OUT, JSON.stringify(rows, null, 2));
console.log(`Wrote ${rows.length} rows to ${OUT}`);
