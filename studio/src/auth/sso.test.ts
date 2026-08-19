/**
 * SSO round trip — the browser half of the CSRF defence.
 *
 * The backend signature proves a `state` is ours and unexpired; it cannot
 * prove the person finishing the sign-in is the person who started it. That is
 * this file's job: the state is kept in sessionStorage and the value coming
 * back from the provider must match it. Without the comparison an attacker can
 * hand a victim a callback URL carrying the attacker's own valid state and
 * code, and the victim silently ends up inside the attacker's account.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { beginSso, completeSso, consumeSso } from "./sso";

const PENDING = { provider: "google", state: "signed-state", redirectUri: "http://app/cb" };

beforeEach(() => {
  sessionStorage.clear();
  vi.restoreAllMocks();
});

afterEach(() => sessionStorage.clear());

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

describe("beginSso", () => {
  it("stores the state before navigating", async () => {
    mockFetch(200, { auth_url: "https://accounts.google.com/o/oauth2/v2/auth?x=1", state: "s-1" });
    // jsdom refuses a real navigation; only the storage side matters here.
    vi.stubGlobal("location", { ...window.location, href: "", origin: "http://app" });

    await beginSso("google", "http://app/cb");

    const stored = JSON.parse(sessionStorage.getItem("velaris_sso_pending")!);
    expect(stored).toEqual({ provider: "google", state: "s-1", redirectUri: "http://app/cb" });
  });

  it("refuses to navigate when the backend returns no state", async () => {
    // A stateless auth_url is the old, forgeable behaviour — never follow it.
    mockFetch(200, { auth_url: "https://accounts.google.com/o/oauth2/v2/auth" });
    await expect(beginSso("google", "http://app/cb")).rejects.toThrow();
    expect(sessionStorage.getItem("velaris_sso_pending")).toBeNull();
  });
});

describe("consumeSso", () => {
  it("is single-use", () => {
    sessionStorage.setItem("velaris_sso_pending", JSON.stringify(PENDING));
    expect(consumeSso()).toEqual(PENDING);
    expect(consumeSso()).toBeNull();
  });

  it("returns null for a half-written entry", () => {
    sessionStorage.setItem("velaris_sso_pending", JSON.stringify({ provider: "google" }));
    expect(consumeSso()).toBeNull();
  });

  it("returns null for unparseable storage", () => {
    sessionStorage.setItem("velaris_sso_pending", "not-json");
    expect(consumeSso()).toBeNull();
  });
});

describe("completeSso", () => {
  it("refuses a state this tab did not issue", async () => {
    const fetchFn = mockFetch(200, { access_token: "t" });
    await expect(
      completeSso(PENDING, "code", "attacker-state"),
    ).rejects.toThrow(/did not start in this browser/);
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it("refuses a callback carrying no state at all", async () => {
    const fetchFn = mockFetch(200, { access_token: "t" });
    await expect(completeSso(PENDING, "code", "")).rejects.toThrow();
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it("exchanges the code when the state matches", async () => {
    const fetchFn = mockFetch(200, { access_token: "a", refresh_token: "r", device_id: "d" });
    const tokens = await completeSso(PENDING, "the-code", PENDING.state, "dev-1");

    expect(tokens.access_token).toBe("a");
    const body = JSON.parse((fetchFn.mock.calls[0][1] as RequestInit).body as string);
    expect(body).toMatchObject({
      provider: "google",
      code: "the-code",
      redirect_uri: "http://app/cb",
      state: "signed-state",
      device_id: "dev-1",
    });
  });

  it("carries the catalog code so the failure can be explained", async () => {
    mockFetch(403, {
      detail: "This account is not provisioned in Velaris.",
      error: { code: "VEL-AUTH-SSO-PROVISION-403" },
    });
    await expect(completeSso(PENDING, "code", PENDING.state)).rejects.toMatchObject({
      code: "VEL-AUTH-SSO-PROVISION-403",
    });
  });
});
