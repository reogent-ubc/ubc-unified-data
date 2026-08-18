import { describe, expect, it } from "vitest";
import { humanBytes } from "./update.ts";

describe("humanBytes", () => {
  it("scales through the units", () => {
    expect(humanBytes(900)).toBe("900B");
    expect(humanBytes(31741506)).toBe("30.3MB");
    expect(humanBytes(197 * 1024 * 1024)).toBe("197.0MB");
    expect(humanBytes(1.5 * 1024 * 1024 * 1024)).toBe("1.5GB");
  });
});
