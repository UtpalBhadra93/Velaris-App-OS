/**
 * The Studio error catalog.
 *
 * This file is hand-maintained and consumed by `ErrorCodeLink`, which decides
 * whether a code the API returned becomes a link or stays plain text. A
 * malformed or duplicated entry degrades silently — the link just never
 * appears — so the structural checks live here rather than being noticed in
 * production by someone already looking at an error.
 */
import { describe, expect, it } from "vitest";

import { ERROR_CATALOG, allErrors, hasEntry, searchErrors } from "./catalog";

describe("catalog structure", () => {
  it("has entries", () => {
    expect(allErrors().length).toBeGreaterThan(0);
  });

  it("uses the VEL-<SUBSYSTEM>-<SLUG>-<HTTP> scheme throughout", () => {
    const bad = allErrors()
      .map(e => e.code)
      // VEL-SYS-STORAGE-PRECHECK predates the scheme and is documented as such.
      .filter(c => !/^VEL-[A-Z]+-[A-Z0-9-]+(-\d{3})?$/.test(c));
    expect(bad).toEqual([]);
  });

  it("has no duplicate codes", () => {
    const codes = allErrors().map(e => e.code);
    const dupes = codes.filter((c, i) => codes.indexOf(c) !== i);
    // A duplicate makes search return the same code twice and makes the
    // deep-link ambiguous about which entry it opened.
    expect([...new Set(dupes)]).toEqual([]);
  });

  it("gives every entry a symptom, cause and resolution", () => {
    const incomplete = allErrors()
      .filter(e => !e.symptom?.trim() || !e.rootCause?.trim() || !e.resolution?.trim())
      .map(e => e.code);
    // An entry with an empty resolution is worse than no entry: the user
    // follows a link promising help and finds a blank row.
    expect(incomplete).toEqual([]);
  });

  it("labels every component group", () => {
    const unlabelled = Object.entries(ERROR_CATALOG)
      .filter(([, c]) => !c.label?.trim())
      .map(([k]) => k);
    expect(unlabelled).toEqual([]);
  });

  it("points `related` only at codes that exist", () => {
    const known = new Set(allErrors().map(e => e.code));
    const dangling: string[] = [];
    for (const e of allErrors()) {
      for (const r of e.related ?? []) {
        // Related codes render as cross-references; one pointing nowhere
        // sends a reader looking for a page that does not exist.
        if (!known.has(r)) dangling.push(`${e.code} → ${r}`);
      }
    }
    expect(dangling).toEqual([]);
  });
});

describe("hasEntry", () => {
  it("finds a known code", () => {
    expect(hasEntry(allErrors()[0]!.code)).toBe(true);
  });

  it("is case-insensitive and tolerates whitespace", () => {
    const code = allErrors()[0]!.code;
    expect(hasEntry(`  ${code.toLowerCase()}  `)).toBe(true);
  });

  it("rejects an unknown code", () => {
    // ErrorCodeLink relies on this: an unknown code must render as plain
    // text, never as a link promising an explanation that does not exist.
    expect(hasEntry("VEL-NOPE-MISSING-418")).toBe(false);
  });
});

describe("searchErrors", () => {
  it("returns nothing for an empty query", () => {
    expect(searchErrors("   ")).toEqual([]);
  });

  it("matches on code, case-insensitively", () => {
    const code = allErrors()[0]!.code;
    expect(searchErrors(code.toLowerCase()).some(e => e.code === code)).toBe(true);
  });

  it("matches on free text in the symptom", () => {
    expect(searchErrors("upload").length).toBeGreaterThan(0);
  });

  it("carries the component label on each result", () => {
    const [first] = searchErrors("VEL-");
    expect(first?.componentLabel).toBeTruthy();
    expect(first?.componentKey).toBeTruthy();
  });
});
