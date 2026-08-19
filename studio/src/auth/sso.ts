// OAuth2 SSO round trip — the browser half of the CSRF defence.
//
// The backend signs the `state` it issues, which proves a callback's state was
// minted by us and has not expired. That alone does not tie the flow to the
// person finishing it: an attacker can start their own sign-in, obtain a
// perfectly valid state, and then trick a victim's browser into completing it
// with the attacker's authorization code — the victim ends up silently logged
// into the attacker's account.
//
// This file closes that half. The state is held in `sessionStorage`, which is
// origin-scoped, per-tab, and unwritable by any other site, and the value the
// provider hands back must match it. An attacker can forge the URL; they
// cannot forge the tab's storage.
//
// sessionStorage rather than localStorage on purpose: the value must not
// outlive the tab that started the sign-in, and two tabs signing in at once
// must not overwrite each other's state.
//
// Copyright (c) 2024-2026 Velaris Contributors
// SPDX-License-Identifier: BSL-1.1

const SSO_STATE_KEY = "velaris_sso_pending";

export interface PendingSso {
  provider: string;
  state: string;
  redirectUri: string;
}

/** Fetch the provider's authorize URL, remember the state, and navigate. */
export async function beginSso(provider: string, redirectUri: string): Promise<void> {
  const r = await fetch(
    `/api/v1/auth/real/sso/${encodeURIComponent(provider)}/auth-url` +
      `?redirect_uri=${encodeURIComponent(redirectUri)}`,
  );
  const d = await r.json();
  if (!r.ok || !d.auth_url || !d.state) {
    throw new Error(d?.detail || "Could not start SSO sign-in.");
  }
  const pending: PendingSso = { provider, state: d.state, redirectUri };
  sessionStorage.setItem(SSO_STATE_KEY, JSON.stringify(pending));
  window.location.href = d.auth_url;
}

/** Read and clear the pending sign-in. Single-use: a state that has been
 *  consumed must never validate a second callback. */
export function consumeSso(): PendingSso | null {
  const raw = sessionStorage.getItem(SSO_STATE_KEY);
  sessionStorage.removeItem(SSO_STATE_KEY);
  if (!raw) return null;
  try {
    const p = JSON.parse(raw) as PendingSso;
    return p?.provider && p?.state && p?.redirectUri ? p : null;
  } catch {
    return null;
  }
}

export interface SsoTokens {
  access_token: string;
  refresh_token?: string;
  device_id?: string;
}

/** Exchange the provider's code for Velaris tokens.
 *
 * `returnedState` is what came back in the URL; it is compared against the
 * stored value here rather than trusted, and only then sent on. */
export async function completeSso(
  pending: PendingSso,
  code: string,
  returnedState: string,
  deviceId?: string | null,
): Promise<SsoTokens> {
  if (!returnedState || returnedState !== pending.state) {
    throw new Error(
      "This sign-in did not start in this browser. Return to the sign-in page and try again.",
    );
  }
  const r = await fetch("/api/v1/auth/real/sso/callback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      provider: pending.provider,
      code,
      redirect_uri: pending.redirectUri,
      state: pending.state,
      device_id: deviceId ?? undefined,
    }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) {
    const err = new Error(d?.detail || "SSO sign-in failed.");
    // Carry the catalog code so ErrorCodeLink can offer the explanation.
    (err as { code?: string }).code = d?.error?.code;
    throw err;
  }
  return d as SsoTokens;
}
