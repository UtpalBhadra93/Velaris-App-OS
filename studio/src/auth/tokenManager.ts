export const ACCESS_KEY  = "velaris_token";
const REFRESH_KEY = "velaris_refresh_token";
// Group J: id of this browser's auth_devices row. Survives logout on purpose —
// the next login reuses the same device record instead of growing a new row
// per login. The server re-validates ownership + user-agent before reuse.
const DEVICE_KEY  = "velaris_device_id";

/* ── One-time carry-over from the pre-rebrand key names ───────────
   These are `localStorage` keys in this app only — the server never sees them,
   it reads the `Authorization` header — so renaming them is cosmetic *except*
   that a browser holding a live session under the old names would silently be
   logged out by the new build. So the old names are read once and rewritten,
   then removed.

   Removal is the point, not tidiness: `clearTokens()` only clears the keys it
   knows about, so a leftover `helix_refresh_token` would survive a sign-out and
   leave a usable refresh token sitting in the browser. Runs at module load,
   before any accessor can be called, because every consumer reaches storage
   through this module. */
const LEGACY_KEYS: ReadonlyArray<readonly [string, string]> = [
  ["helix_token",         ACCESS_KEY],
  ["helix_refresh_token", REFRESH_KEY],
  ["helix_device_id",     DEVICE_KEY],
];

export function migrateLegacyKeys(): void {
  try {
    for (const [legacy, current] of LEGACY_KEYS) {
      const value = localStorage.getItem(legacy);
      if (value === null) continue;
      // Never overwrite a value this build already wrote: a second tab on the
      // old build can rewrite the legacy key after this one has moved on, and
      // adopting that stale pair would revoke the live session.
      if (localStorage.getItem(current) === null) localStorage.setItem(current, value);
      localStorage.removeItem(legacy);
    }
  } catch {
    // Storage disabled or full. Nothing here is worth breaking startup for —
    // the worst case is one extra sign-in.
  }
}

migrateLegacyKeys();

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_KEY);
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_KEY);
}

/** Set the access token alone.
 *
 * The portal's own sign-in issues an access token and no refresh token, so it
 * cannot go through `setTokens`. Deliberately does not arm the proactive
 * refresh: there is no refresh token to rotate.
 */
export function setAccessToken(access: string): void {
  localStorage.setItem(ACCESS_KEY, access);
}

export function setTokens(access: string, refresh: string): void {
  localStorage.setItem(ACCESS_KEY, access);
  localStorage.setItem(REFRESH_KEY, refresh);
  scheduleProactiveRefresh();
}

export function clearTokens(): void {
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(REFRESH_KEY);
  // A tab still running the pre-rename build can rewrite the legacy keys after
  // startup migrated them. Signing out must not leave that copy behind.
  localStorage.removeItem("helix_token");
  localStorage.removeItem("helix_refresh_token");
  cancelProactiveRefresh();
}

export function getDeviceId(): string | null {
  return localStorage.getItem(DEVICE_KEY);
}

export function setDeviceId(id: string): void {
  if (id) localStorage.setItem(DEVICE_KEY, id);
}

// Deduplicate concurrent refresh attempts — only one in-flight at a time.
let _refreshing: Promise<string | null> | null = null;
let _lastRefreshAt = 0;

export async function attemptRefresh(): Promise<string | null> {
  if (_refreshing) return _refreshing;
  // A rotation just happened (e.g. the global 401 interceptor refreshed and a
  // request-level retry asks again) — reuse it instead of rotating the chain twice.
  if (Date.now() - _lastRefreshAt < 10_000) return getAccessToken();

  // Capture both tokens before any async gap — logout may call clearTokens()
  // while the fetch is in-flight. We need the current access token for the
  // orphan-revoke Authorization header even after localStorage has been cleared.
  const refreshToken  = getRefreshToken();
  const capturedAccess = getAccessToken();
  if (!refreshToken) return null;

  _refreshing = (async () => {
    try {
      const resp = await fetch("/api/v1/auth/real/refresh", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ refresh_token: refreshToken }),
      });
      if (!resp.ok) {
        // Refresh tokens are single-use: a 401 here usually means another tab
        // won the rotation race and our copy was revoked by its rotation.
        // localStorage is shared, so the winner's new pair lands there — poll
        // briefly for it and adopt it instead of nuking the whole session.
        for (const delay of [0, 400, 1200]) {
          if (delay) await new Promise((r) => setTimeout(r, delay));
          const current = getRefreshToken();
          if (current && current !== refreshToken) return getAccessToken();
          if (!current) return null; // a real logout happened meanwhile
        }
        if (resp.status === 429) return null; // rate-limited — retry later, session may still be fine
        clearTokens();
        return null;
      }
      const data = await resp.json();
      // Guard: if the user logged out while this fetch was in-flight, the refresh
      // token has been cleared. Revoke the newly issued pair server-side so it
      // cannot be used — the old access token is used as the auth credential
      // because clearTokens() has already wiped localStorage.
      if (!getRefreshToken()) {
        fetch("/api/v1/auth/real/logout", {
          method:  "POST",
          headers: {
            "Content-Type": "application/json",
            ...(capturedAccess ? { "Authorization": `Bearer ${capturedAccess}` } : {}),
          },
          body: JSON.stringify({ refresh_token: data.refresh_token }),
        }).catch(() => {});
        return null;
      }
      setTokens(data.access_token, data.refresh_token);
      _lastRefreshAt = Date.now();
      return data.access_token as string;
    } catch {
      return null;
    } finally {
      _refreshing = null;
    }
  })();

  return _refreshing;
}

/* ── Proactive refresh ────────────────────────────────────────────
   Refresh shortly BEFORE the access token expires so active users
   (e.g. a representative mid-call) never hit a 401 at all. Armed by
   setTokens() on login/refresh and by AuthContext after a restored
   session; disarmed by clearTokens(). */

let _proactiveTimer: number | null = null;

function accessTokenExpMs(token: string): number | null {
  try {
    const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return typeof payload.exp === "number" ? payload.exp * 1000 : null;
  } catch {
    return null;
  }
}

export function cancelProactiveRefresh(): void {
  if (_proactiveTimer !== null) { clearTimeout(_proactiveTimer); _proactiveTimer = null; }
}

export function scheduleProactiveRefresh(): void {
  cancelProactiveRefresh();
  const access = getAccessToken();
  if (!access || !getRefreshToken()) return;
  const exp = accessTokenExpMs(access);
  if (!exp) return;
  // 2 min head start + per-tab jitter so multiple tabs don't rotate the
  // shared single-use refresh token at the same instant.
  const headStart = 120_000 + Math.floor(Math.random() * 30_000);
  const delay = Math.max(exp - Date.now() - headStart, 30_000);
  _proactiveTimer = window.setTimeout(async () => {
    _proactiveTimer = null;
    const tok = await attemptRefresh(); // success re-arms via setTokens()
    // Failure without logout (network blip, 429, lost race with a slow
    // winner): keep the timer alive so we try again before expiry.
    if (!tok && getRefreshToken()) scheduleProactiveRefresh();
  }, delay);
}
