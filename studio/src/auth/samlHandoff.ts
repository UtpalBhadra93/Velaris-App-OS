// The browser half of a SAML sign-in.
//
// The ACS never hands tokens back in the redirect: a refresh token in a URL is
// written into history, into the `Referer` of whatever the landing page loads
// next, and into every proxy log on the way. It mints a **single-use, 60-second
// handoff code** instead and redirects with `?saml_code=…`; this module is what
// trades that code for a session.
//
// Without it the flow dead-ends — the assertion verifies, the account is
// provisioned, and the browser sits on the landing page with an unused code in
// the address bar. That is exactly how the OAuth half was broken before
// `SsoCallback` existed.
//
// **There is no dedicated route to hang this on.** RelayState decides where the
// ACS lands, and any path inside the provider's allowlist is legitimate, so the
// code has to be redeemed wherever the browser arrives — see `ProtectedRoute`.
//
// Copyright (c) 2024-2026 Velaris Contributors
// SPDX-License-Identifier: BSL-1.1

import { getDeviceId, setDeviceId, setTokens } from "./tokenManager";

export const SAML_CODE_PARAM = "saml_code";

/** The handoff code in this URL, if the browser arrived from an ACS redirect. */
export function pendingSamlCode(): string | null {
  const value = new URLSearchParams(window.location.search).get(SAML_CODE_PARAM);
  return value && value.trim() ? value.trim() : null;
}

/** Drop the code from the address bar without reloading or adding history.
 *
 *  The code is single-use and already spent by the time this runs; leaving it
 *  in the URL means a refresh, a bookmark or a shared link carries a credential
 *  that looks live, and a copied URL is the most common way one leaks. */
export function stripSamlCode(): void {
  const url = new URL(window.location.href);
  if (!url.searchParams.has(SAML_CODE_PARAM)) return;
  url.searchParams.delete(SAML_CODE_PARAM);
  window.history.replaceState({}, "", url.pathname + url.search + url.hash);
}

/** Trade the code for a session. Resolves true when a session now exists. */
export async function redeemSamlCode(code: string): Promise<boolean> {
  const resp = await fetch("/api/v1/auth/saml/exchange", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // The device is bound from *this* request: the ACS POST came from the IdP's
    // redirect, so this is the first request the user's own browser makes.
    body: JSON.stringify({ code, device_id: getDeviceId() }),
  });
  if (!resp.ok) {
    let detail = "Sign-in could not be completed.";
    try {
      const body = await resp.json();
      detail = body?.detail?.message ?? body?.detail ?? detail;
    } catch { /* a non-JSON body is still a failed sign-in */ }
    throw new Error(typeof detail === "string" ? detail : "Sign-in could not be completed.");
  }
  const data = await resp.json();
  if (!data?.access_token) return false;
  setTokens(data.access_token, data.refresh_token ?? "");
  if (data.device_id) setDeviceId(data.device_id);
  return true;
}

/** Start an SP-initiated sign-in. A full navigation, not fetch: the response is
 *  a 302 to the IdP, and the browser has to follow it. */
export function beginSamlLogin(tenant: string, target: string = window.location.pathname): void {
  const query = new URLSearchParams({ target: target.startsWith("/") ? target : "/" });
  window.location.href =
    `/api/v1/auth/saml/${encodeURIComponent(tenant)}/login?${query}`;
}
