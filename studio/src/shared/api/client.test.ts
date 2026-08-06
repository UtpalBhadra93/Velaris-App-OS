/**
 * API client — the single path every case-service call takes.
 *
 * The header-merge order is the load-bearing detail. A caller passing its own
 * `headers` must merge with the auth header, not replace it: replacing drops
 * Authorization, the resulting 401 trips the global interceptor, and the user
 * is logged out by what looks like an unrelated feature.
 */
import { describe, expect, it, vi } from "vitest";

import { ApiError, codedFrom } from "./client";

describe("ApiError", () => {
  it("carries status and message", () => {
    const e = new ApiError(404, "not found");
    expect(e.status).toBe(404);
    expect(e.message).toBe("not found");
    expect(e.name).toBe("ApiError");
  });

  it("is instanceof Error, so existing catch blocks still work", () => {
    expect(new ApiError(500, "x")).toBeInstanceOf(Error);
  });

  it("carries the coded block when one is supplied", () => {
    const e = new ApiError(503, "storage down", {
      code: "VEL-DOC-STORAGE-503",
      docs: "/docs/errors#VEL-DOC-STORAGE-503",
    });
    expect(e.code).toBe("VEL-DOC-STORAGE-503");
    expect(e.docs).toContain("#VEL-DOC-STORAGE-503");
  });

  it("leaves code undefined for the many uncoded subsystems", () => {
    expect(new ApiError(400, "bad").code).toBeUndefined();
  });
});

describe("codedFrom", () => {
  it("extracts a well-formed error block", () => {
    expect(codedFrom({ detail: "x", error: { code: "VEL-A-B-400", docs: "/d" } }))
      .toEqual({ code: "VEL-A-B-400", docs: "/d" });
  });

  it("returns undefined when the body has no error block", () => {
    expect(codedFrom({ detail: "plain" })).toBeUndefined();
  });

  it("survives every malformed shape rather than throwing", () => {
    // This runs on a response that has ALREADY failed. Throwing here would
    // replace a useful HTTP error with a TypeError from the error handler.
    expect(codedFrom(null)).toBeUndefined();
    expect(codedFrom(undefined)).toBeUndefined();
    expect(codedFrom("a string")).toBeUndefined();
    expect(codedFrom({ error: "not an object" })).toBeUndefined();
    expect(codedFrom({ error: {} })).toBeUndefined();
    expect(codedFrom({ error: { code: 42 } })).toBeUndefined();
  });

  it("keeps the code when docs is missing or wrong-typed", () => {
    expect(codedFrom({ error: { code: "VEL-A-B-400" } }))
      .toEqual({ code: "VEL-A-B-400", docs: undefined });
    expect(codedFrom({ error: { code: "VEL-A-B-400", docs: 7 } })?.code)
      .toBe("VEL-A-B-400");
  });
});

describe("request header merging", () => {
  it("keeps Authorization when the caller passes its own headers", async () => {
    localStorage.setItem("helix_token", "tok");
    const fetchMock = vi.fn(async (_u: string, _i?: RequestInit) => ({
      ok: true, status: 200, json: async () => ({}),
    } as Response));
    vi.stubGlobal("fetch", fetchMock);

    const { getHealth } = await import("./client");
    await getHealth();

    const [, init] = fetchMock.mock.calls[0]!;
    const headers = init!.headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer tok");
    expect(headers["Content-Type"]).toBe("application/json");
  });

  it("sends no Authorization header when logged out", async () => {
    const fetchMock = vi.fn(async (_u: string, _i?: RequestInit) => ({
      ok: true, status: 200, json: async () => ({}),
    } as Response));
    vi.stubGlobal("fetch", fetchMock);

    const { getHealth } = await import("./client");
    await getHealth();

    const [, init] = fetchMock.mock.calls[0]!;
    expect((init!.headers as Record<string, string>).Authorization).toBeUndefined();
  });
});
