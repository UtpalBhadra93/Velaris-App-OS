/**
 * Tests for the Compliance module's shared helpers.
 *
 * `cents()` gets the most attention deliberately. HxLedger stores money as
 * integer cents precisely so float arithmetic never touches it — and this
 * function is the one place the frontend divides by 100. A rounding bug here
 * would misreport a trial balance while every backend test still passed.
 */
import { describe, expect, it, vi } from "vitest";

import { apiJSON, authHdr, cents, postJSON, when } from "./shared";

describe("authHdr", () => {
  it("returns no header when there is no token", () => {
    expect(authHdr()).toEqual({});
  });

  it("returns a bearer header when a token is present", () => {
    localStorage.setItem("velaris_token", "abc123");
    expect(authHdr()).toEqual({ Authorization: "Bearer abc123" });
  });
});

describe("cents", () => {
  it("renders whole and fractional amounts to exactly 2 decimals", () => {
    expect(cents(10_000)).toBe("USD 100.00");
    expect(cents(1)).toBe("USD 0.01");
    expect(cents(0)).toBe("USD 0.00");
  });

  it("keeps the minus sign outside the currency", () => {
    // "USD -5.00", not "-USD 5.00" — the latter reads as a negative currency.
    expect(cents(-500)).toBe("-USD 5.00");
  });

  it("distinguishes null from zero", () => {
    // A ledger that renders "no data" as 0.00 claims books that balance.
    expect(cents(null)).toBe("—");
    expect(cents(undefined)).toBe("—");
    expect(cents(0)).toBe("USD 0.00");
  });

  it("does not lose precision on amounts that break float division", () => {
    expect(cents(70)).toBe("USD 0.70");
    expect(cents(1_000_000_01)).toBe("USD 1,000,000.01");
  });

  it("uppercases the currency it is given", () => {
    expect(cents(2_500, "eur")).toBe("EUR 25.00");
  });
});

describe("when", () => {
  it("renders an em dash for missing timestamps rather than 'Invalid Date'", () => {
    expect(when(null)).toBe("—");
    expect(when(undefined)).toBe("—");
    expect(when("")).toBe("—");
  });

  it("formats a real ISO timestamp", () => {
    expect(when("2026-08-04T10:00:00Z")).not.toBe("—");
  });
});

describe("apiJSON", () => {
  it("attaches the auth header", async () => {
    localStorage.setItem("velaris_token", "tok");
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      Promise.resolve({ ok: true, json: () => Promise.resolve({ fine: true }) } as Response),
    );
    vi.stubGlobal("fetch", fetchMock);

    await apiJSON("/api/v1/compliance/audit/status");

    const [, init] = fetchMock.mock.calls[0]!;
    expect((init!.headers as Record<string, string>).Authorization).toBe("Bearer tok");
  });

  it("surfaces the server's detail rather than a bare status", async () => {
    vi.stubGlobal("fetch", vi.fn(() =>
      Promise.resolve({
        ok: false, status: 400,
        json: () => Promise.resolve({ detail: "that basis cannot be withdrawn" }),
      } as Response),
    ));

    await expect(apiJSON("/x")).rejects.toThrow("that basis cannot be withdrawn");
  });

  it("stringifies a non-string detail instead of rendering [object Object]", async () => {
    vi.stubGlobal("fetch", vi.fn(() =>
      Promise.resolve({
        ok: false, status: 422,
        json: () => Promise.resolve({ detail: [{ loc: ["body"], msg: "bad" }] }),
      } as Response),
    ));

    await expect(apiJSON("/x")).rejects.toThrow(/loc/);
  });

  it("still throws a useful error when the body is not JSON at all", async () => {
    vi.stubGlobal("fetch", vi.fn(() =>
      Promise.resolve({
        ok: false, status: 502,
        json: () => Promise.reject(new SyntaxError("Unexpected token <")),
      } as Response),
    ));

    await expect(apiJSON("/gateway")).rejects.toThrow("502");
  });
});

describe("postJSON", () => {
  it("sends the body as JSON with a content-type", async () => {
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      Promise.resolve({ ok: true, json: () => Promise.resolve({}) } as Response),
    );
    vi.stubGlobal("fetch", fetchMock);

    await postJSON("/api/v1/compliance/consent", { purpose: "marketing" });

    const [, init] = fetchMock.mock.calls[0]!;
    expect(init!.method).toBe("POST");
    expect(JSON.parse(init!.body as string)).toEqual({ purpose: "marketing" });
  });
});
