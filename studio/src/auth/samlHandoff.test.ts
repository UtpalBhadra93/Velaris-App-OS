/**
 * The browser half of a SAML sign-in.
 *
 * The ACS deliberately redirects with a **code**, not tokens — a refresh token
 * in a URL lands in history, in `Referer` and in proxy logs. Nothing shipped
 * redeemed that code, so a verified assertion ended with the browser sitting on
 * the landing page and an unused credential in the address bar: the same shape
 * as the OAuth callback route that was missing before `SsoCallback` existed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { pendingSamlCode, redeemSamlCode, stripSamlCode } from "./samlHandoff";

function setUrl(url: string) {
  window.history.replaceState({}, "", url);
}

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
  setUrl("/");
});

afterEach(() => localStorage.clear());

describe("pendingSamlCode", () => {
  it("finds the code the ACS redirect carries", () => {
    setUrl("/?saml_code=abc123");
    expect(pendingSamlCode()).toBe("abc123");
  });

  it("is null on an ordinary page load", () => {
    setUrl("/cases");
    expect(pendingSamlCode()).toBeNull();
  });

  it("ignores an empty parameter rather than exchanging nothing", () => {
    setUrl("/?saml_code=");
    expect(pendingSamlCode()).toBeNull();
  });
});

describe("stripSamlCode", () => {
  it("removes the spent code without dropping the rest of the URL", () => {
    setUrl("/cases?saml_code=abc123&view=open");
    stripSamlCode();
    expect(window.location.search).toBe("?view=open");
    expect(window.location.pathname).toBe("/cases");
  });

  it("leaves a URL without a code alone", () => {
    setUrl("/cases?view=open");
    stripSamlCode();
    expect(window.location.search).toBe("?view=open");
  });
});

describe("redeemSamlCode", () => {
  it("stores the pair and the device binding", async () => {
    const fetchMock = mockFetch(200, {
      access_token: "at", refresh_token: "rt", device_id: "dev-1",
      user: { username: "ada" },
    });

    await expect(redeemSamlCode("abc123")).resolves.toBe(true);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/auth/saml/exchange");
    expect(JSON.parse(init.body).code).toBe("abc123");
    expect(localStorage.getItem("velaris_token")).toBe("at");
    // Without the refresh token the session dies at the first access-token
    // expiry — the exact defect the MFA path had.
    expect(localStorage.getItem("velaris_refresh_token")).toBe("rt");
    expect(localStorage.getItem("velaris_device_id")).toBe("dev-1");
  });

  it("surfaces the server's refusal instead of a blank failure", async () => {
    mockFetch(400, { detail: { message: "Sign-in could not be completed." } });
    await expect(redeemSamlCode("spent")).rejects.toThrow(
      "Sign-in could not be completed.");
    expect(localStorage.getItem("velaris_token")).toBeNull();
  });

  it("does not store a session when the body carries no token", async () => {
    mockFetch(200, { user: { username: "ada" } });
    await expect(redeemSamlCode("odd")).resolves.toBe(false);
    expect(localStorage.getItem("velaris_token")).toBeNull();
  });
});
