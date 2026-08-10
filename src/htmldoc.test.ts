import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { blocks, items, sections, text, unescapeHtml } from "./htmldoc.ts";

const fixture = JSON.parse(
  readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "test/fixtures/htmldoc.json"), "utf8"),
) as Array<{
  input: string;
  blocks: Array<["Heading" | "Table", ...unknown[]]>;
  sections: Array<[number, string, string, string[]]>;
  text: string;
}>;

function mineBlocks(html: string): Array<["Heading" | "Table", ...unknown[]]> {
  return blocks(html).map((block) =>
    "level" in block ? ["Heading", block.text, [], []] : ["Table", "", block.headers, block.rows],
  );
}

function mineSections(html: string): Array<[number, string, string, string[]]> {
  return sections(html).map((section) => [section.level, section.heading, section.text, section.items]);
}

describe("htmldoc block, section and text extraction", () => {
  for (const [index, input] of fixture.entries()) {
    it(`case ${index}: ${input.input.slice(0, 70)}`, () => {
      expect(mineBlocks(input.input)).toEqual(input.blocks);
      expect(mineSections(input.input)).toEqual(input.sections);
      expect(text(input.input)).toBe(input.text);
    });
  }
});

describe("items() falls back to prose", () => {
  it("returns list items when present", () => {
    expect(items("<ul><li>one</li><li>two</li></ul>")).toEqual(["one", "two"]);
  });
  it("falls back to the fragment text", () => {
    expect(items("<p>just a sentence</p>")).toEqual(["just a sentence"]);
  });
});

describe("unescapeHtml matches html.unescape", () => {
  it("decodes named and numeric references", () => {
    expect(unescapeHtml("&lt;tag&gt; &copy; &#38; &#x27;")).toBe("<tag> © & '");
  });
  it("keeps unknown names, with the longest-prefix fallback", () => {
    expect(unescapeHtml("&bogus; &ampx &CounterClockwiseContourIntegral; &fjlig;")).toBe("&bogus; &x ∳ fj");
  });
  it("maps invalid and out-of-range numeric references", () => {
    expect(unescapeHtml("&#0; &#13; &#x80; &#xD800; &#x110000; &#x10FFFE;")).toBe("\uFFFD \r \u20ac \uFFFD \uFFFD ");
  });
});
