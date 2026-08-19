// Redeems a SAML handoff code wherever the ACS lands.
//
// **Why this is not inside a route.** RelayState decides where the browser
// arrives, and it does not have to be a path this SPA renders — a provider
// configured with the old default sent people to `/studio`, which matches no
// route at all. Anything mounted inside `<Routes>` never runs there, so the
// sign-in completed on the server and then silently stopped: an unredeemed
// code sat in the address bar and the screen stayed blank.
//
// So this sits *above* route matching. It renders its children untouched on an
// ordinary page load, and takes over only when `?saml_code=` is present.
//
// Copyright (c) 2024-2026 Velaris Contributors
// SPDX-License-Identifier: BSL-1.1
import React, { useEffect, useRef, useState } from "react";

import { pendingSamlCode, redeemSamlCode, stripSamlCode } from "./samlHandoff";

const centred: React.CSSProperties = {
  display: "flex", flexDirection: "column", alignItems: "center",
  justifyContent: "center", gap: 12, height: "100vh",
  background: "var(--bg-root)", color: "var(--text-primary, #111)",
  fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
  padding: 24, textAlign: "center",
};

export default function SamlHandoffGate({ children }: { children: React.ReactNode }) {
  const [code] = useState<string | null>(() => pendingSamlCode());
  const [error, setError] = useState<string | null>(null);
  // StrictMode double-invokes effects in development and the code is
  // single-use: a second exchange would fail a sign-in that just succeeded.
  const ran = useRef(false);

  useEffect(() => {
    if (!code || ran.current) return;
    ran.current = true;
    (async () => {
      try {
        await redeemSamlCode(code);
        stripSamlCode();
        // Land on the app root rather than reloading in place: the path the ACS
        // chose may not be a route, and arriving signed-in on a blank screen is
        // the failure this component exists to prevent.
        window.location.replace("/");
      } catch (e: any) {
        stripSamlCode();
        setError(e?.message || "Sign-in could not be completed.");
      }
    })();
  }, [code]);

  if (code && !error) {
    return (
      <div style={centred}>
        <div style={{ fontSize: 14, fontWeight: 600 }}>Completing sign-in…</div>
      </div>
    );
  }

  if (error) {
    return (
      <div style={centred}>
        <div style={{ fontSize: 15, fontWeight: 700 }}>Sign-in failed</div>
        <div style={{ fontSize: 13, maxWidth: 460, opacity: 0.8 }}>{error}</div>
        <a href="/" style={{ fontSize: 13, marginTop: 8 }}>Back to sign in</a>
      </div>
    );
  }

  return <>{children}</>;
}
