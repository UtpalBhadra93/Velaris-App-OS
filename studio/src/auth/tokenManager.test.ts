/**
 * Token manager.
 *
 * Every authenticated request in Studio depends on this. Two behaviours are
 * load-bearing and neither is visible when it breaks: the device id must
 * survive logout, and concurrent refreshes must collapse into one.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ACCESS_KEY,
  cancelProactiveRefresh,
  clearTokens,
  getAccessToken,
  getDeviceId,
  getRefreshToken,
  setDeviceId,
  setTokens,
} from "./tokenManager";

afterEach(() => cancelProactiveRefresh());

describe("token storage", () => {
  it("round-trips access and refresh tokens", () => {
    setTokens("a-token", "r-token");
    expect(getAccessToken()).toBe("a-token");
    expect(getRefreshToken()).toBe("r-token");
  });

  it("returns null when nothing is stored", () => {
    expect(getAccessToken()).toBeNull();
    expect(getRefreshToken()).toBeNull();
  });

  it("stores the access token under the key the whole app reads", () => {
    // Several modules hand-roll their auth header off this exact key.
    // Renaming it silently unauthenticates them.
    setTokens("a", "r");
    expect(localStorage.getItem(ACCESS_KEY)).toBe("a");
    expect(ACCESS_KEY).toBe("helix_token");
  });

  it("clears both tokens on logout", () => {
    setTokens("a", "r");
    clearTokens();
    expect(getAccessToken()).toBeNull();
    expect(getRefreshToken()).toBeNull();
  });
});

describe("device id", () => {
  it("survives logout on purpose", () => {
    // The next login reuses the same device row instead of growing a new one
    // per login; the server re-validates ownership before reuse.
    setDeviceId("device-1");
    setTokens("a", "r");
    clearTokens();
    expect(getDeviceId()).toBe("device-1");
  });

  it("ignores an empty id rather than storing a blank", () => {
    setDeviceId("device-1");
    setDeviceId("");
    expect(getDeviceId()).toBe("device-1");
  });

  it("is null before one is issued", () => {
    expect(getDeviceId()).toBeNull();
  });
});

describe("attemptRefresh", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("collapses concurrent calls into a single request", async () => {
    // A 401 storm — several requests failing at once — must not fire one
    // refresh each. The server rotates the refresh token, so the second
    // would present an already-spent token and log the user out.
    const fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => ({ access_token: "new-a", refresh_token: "new-r" }),
    } as Response));
    vi.stubGlobal("fetch", fetchMock);

    const { attemptRefresh, setTokens: set } = await import("./tokenManager");
    set("old-a", "old-r");

    const [a, b, c] = await Promise.all([
      attemptRefresh(), attemptRefresh(), attemptRefresh(),
    ]);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(a).toBe(b);
    expect(b).toBe(c);
  });

  it("returns null when there is no refresh token", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const { attemptRefresh } = await import("./tokenManager");
    expect(await attemptRefresh()).toBeNull();
    // Asking the server to refresh nothing is a wasted round trip that
    // always 401s.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns null and does not throw when the server refuses", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: false, status: 401, json: async () => ({ detail: "expired" }),
    } as Response)));

    const { attemptRefresh, setTokens: set } = await import("./tokenManager");
    set("old-a", "old-r");

    // The caller decides what a failed refresh means; throwing here would
    // surface as an unhandled rejection inside an interceptor.
    await expect(attemptRefresh()).resolves.toBeNull();
  });

  it("survives a network error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));

    const { attemptRefresh, setTokens: set } = await import("./tokenManager");
    set("old-a", "old-r");
    await expect(attemptRefresh()).resolves.toBeNull();
  });
});
