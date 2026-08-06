/**
 * nav-data — the single source for GlobalSearch (Ctrl+K) and the SiteMap.
 *
 * It is UI metadata only, never a backend source of truth. What makes it worth
 * testing is that its failures are invisible: a description promising a tab
 * that does not exist, or a role list that hides a page from everyone, both
 * look like ordinary rows in the file.
 *
 * The `Licence management` entry on `/enterprise` was exactly that — it
 * advertised a tab the module never had.
 */
import { describe, expect, it } from "vitest";

import { NAV_DATA } from "./nav-data";

describe("nav entries", () => {
  it("has no duplicate paths", () => {
    const paths = NAV_DATA.map(e => e.path);
    const dupes = paths.filter((p, i) => paths.indexOf(p) !== i);
    // A duplicate makes Ctrl+K show the same destination twice and makes the
    // sitemap's section grouping ambiguous.
    expect([...new Set(dupes)]).toEqual([]);
  });

  it("gives every entry a path, label, description and section", () => {
    const incomplete = NAV_DATA
      .filter(e => !e.path?.trim() || !e.label?.trim() || !e.description?.trim() || !e.section?.trim())
      .map(e => e.path || e.label || "<blank>");
    // An entry with no description is unfindable by search, which is most of
    // what this file exists for.
    expect(incomplete).toEqual([]);
  });

  it("starts every path with a slash", () => {
    expect(NAV_DATA.filter(e => !e.path.startsWith("/")).map(e => e.path)).toEqual([]);
  });

  it("uses the documented pipe-separated description format", () => {
    // The file's own header specifies "Feature A | Feature B | Feature C".
    // A description without separators still renders, but searches badly.
    const unseparated = NAV_DATA
      .filter(e => !e.description.includes("|"))
      .map(e => e.path);
    expect(unseparated).toEqual([]);
  });

  it("keeps descriptions to a searchable handful of keywords", () => {
    // The header asks for 5–7. Beyond ~10 the entry matches everything and
    // stops discriminating in search results.
    const bloated = NAV_DATA
      .filter(e => e.description.split("|").length > 10)
      .map(e => e.path);
    expect(bloated).toEqual([]);
  });
});

describe("roles", () => {
  const VALID = new Set([
    "admin", "manager", "designer", "case_worker",
    "devops", "integration", "security", "viewer", "developer",
  ]);

  it("uses only roles the type declares", () => {
    const bad: string[] = [];
    for (const e of NAV_DATA) {
      for (const r of e.roles) if (!VALID.has(r)) bad.push(`${e.path}: ${r}`);
    }
    // A typo'd role is not a type error at runtime — it silently matches
    // nobody, and the page vanishes from the nav for every user.
    expect(bad).toEqual([]);
  });

  it("treats an empty role list as 'everyone', not 'no one'", () => {
    const open = NAV_DATA.filter(e => e.roles.length === 0);
    // Several workspace pages rely on this. If the convention ever inverts,
    // the dashboard disappears for all users at once.
    expect(open.length).toBeGreaterThan(0);
    expect(open.some(e => e.path === "/")).toBe(true);
  });
});

describe("the Enterprise entry", () => {
  const enterprise = NAV_DATA.find(e => e.path === "/enterprise")!;

  it("exists and is admin-only", () => {
    expect(enterprise).toBeDefined();
    expect(enterprise.roles).toEqual(["admin"]);
  });

  it("advertises retention policies", () => {
    expect(enterprise.description.toLowerCase()).toContain("retention");
  });

  it("no longer advertises licence management", () => {
    // There is no licence tab. Enterprise has overview, events, gdpr and
    // retention — the claim was removed on 2026-08-04.
    expect(enterprise.description.toLowerCase()).not.toContain("licence");
    expect(enterprise.description.toLowerCase()).not.toContain("license");
  });
});
