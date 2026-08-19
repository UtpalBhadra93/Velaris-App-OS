// Where the identity provider drops the user back.
//
// This route did not exist. `LoginPage` sent the provider a `redirect_uri` of
// /auth/sso/callback and nothing was mounted there, so every SSO sign-in
// dead-ended on the app shell with an unused `?code=` in the address bar —
// the backend's callback endpoint was never called by any shipped client.
//
// Copyright (c) 2024-2026 Velaris Contributors
// SPDX-License-Identifier: BSL-1.1
import React, { useEffect, useRef, useState } from "react";

import ErrorCodeLink from "@shared/errors/ErrorCodeLink";
import { completeSso, consumeSso } from "./sso";
import { getDeviceId, setDeviceId, setTokens } from "./tokenManager";

export default function SsoCallback() {
  const [error, setError] = useState<unknown>(null);
  // React 18 StrictMode double-invokes effects in development, and the code is
  // single-use at the provider — a second exchange would fail a working login.
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;

    (async () => {
      const params = new URLSearchParams(window.location.search);
      const pending = consumeSso();

      // The user declined at the provider, or it refused us.
      const providerError = params.get("error_description") || params.get("error");
      if (providerError) return setError(new Error(providerError));

      const code = params.get("code");
      if (!code || !pending) {
        return setError(new Error(
          "This sign-in did not start in this browser. Return to the sign-in page and try again.",
        ));
      }

      try {
        const d = await completeSso(pending, code, params.get("state") ?? "", getDeviceId());
        setTokens(d.access_token, d.refresh_token ?? "");
        if (d.device_id) setDeviceId(d.device_id);
        window.location.replace("/");
      } catch (e) {
        setError(e);
      }
    })();
  }, []);

  const box: React.CSSProperties = {
    display: "flex", flexDirection: "column", alignItems: "center",
    justifyContent: "center", gap: 12, height: "100vh",
    background: "var(--bg-root)", color: "var(--text-primary, #111)",
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    padding: 24, textAlign: "center",
  };

  if (!error) {
    return (
      <div style={box}>
        <div style={{ fontSize: 14, fontWeight: 600 }}>Completing sign-in…</div>
      </div>
    );
  }

  return (
    <div style={box}>
      <div style={{ fontSize: 15, fontWeight: 700 }}>Sign-in failed</div>
      <div style={{ fontSize: 13, maxWidth: 460, opacity: 0.8 }}>
        {(error as Error)?.message ?? "SSO sign-in failed."}
      </div>
      <ErrorCodeLink error={error} />
      <a href="/" style={{ fontSize: 13, marginTop: 8 }}>Back to sign in</a>
    </div>
  );
}
