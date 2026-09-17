/**
 * Customer Engagement (item 12) — the rep desk.
 *
 * This screen IS the feature. A rep on a call cannot use an API, and the
 * customer's original question — *"will the rep see the customer's details, and
 * to what extent?"* — is answered by what is drawn here and what is not.
 *
 * Five things it is careful about, each because the alternative misleads:
 *
 *   1. **A withheld panel is drawn as WITHHELD, never as empty.** An empty
 *      panel says "this customer has none of these" and a rep will act on it.
 *      The screen shows the panel name and which of the two reasons applies —
 *      "not yours to see" and "verify the caller first" need different actions.
 *   2. **The tier is displayed, never chosen.** It comes from the interaction.
 *      A control that let a rep pick their own tier would answer the customer's
 *      question for them.
 *   3. **Masked values are shown masked, with the reveal beside them**, one
 *      field at a time and never in bulk — there is deliberately no "show all".
 *   4. **A failed verification prints the server's single message**, unchanged.
 *      Helpfully explaining which factor failed would build the oracle the
 *      server refuses to be.
 *   5. **Staleness is surfaced.** A CRM-mastered attribute that has not been
 *      refreshed says so rather than passing as current.
 *
 * Layout follows the platform shell (`/help`, `/work-center`): `AppLayout` owns
 * the title, the call bar is a fixed strip that does not scroll away mid-call,
 * and the contact is a two-column body on a wide screen.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useState } from "react";

import {
  engage360,
  engageOnBehalf,
  engageReveal,
  engageScreenPop,
  engageTransfer,
  engageVerify,
  engageWrapUp,
  getEngageStatus,
  type EngageStatus,
  type ScreenPop,
  type ThreeSixty,
} from "@shared/api/client";
import {
  Banner, Button, Card, EmptyState, ErrorNote, MONO, Pill, SectionTitle, Select,
  Spinner, SubPanel, Tabs, TextInput, type PillTone,
} from "@shared/components";
import { NextActions, Recap, WrapUpPicker } from "./CallInsights";
import FloorAnalytics from "./FloorAnalytics";

const TIER_LABEL: Record<string, string> = {
  none: "Anonymous", partial: "Partially verified", full: "Verified",
};
const TIER_TONE: Record<string, PillTone> = {
  none: "muted", partial: "warn", full: "ok",
};

const muted: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)" };

/** The panels the rep is NOT being shown, and which of the two reasons applies. */
function Withheld({ view }: { view: ThreeSixty }) {
  if (!view.withheld.length) return null;
  const permission = view.withheld.filter((w) => w.why.includes("not permitted"));
  const tier = view.withheld.filter((w) => !w.why.includes("not permitted"));
  return (
    <Card style={{ borderLeft: "3px solid var(--status-running)" }}>
      <SectionTitle>Not shown</SectionTitle>
      {tier.length > 0 && (
        <div style={{ display: "flex", gap: 10, alignItems: "flex-start",
                      marginBottom: permission.length ? 10 : 0 }}>
          <Pill tone="warn">verify the caller</Pill>
          <span style={{ fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.55 }}>
            {tier.map((w) => w.panel).join(", ")} — available at a higher
            verification tier.
          </span>
        </div>
      )}
      {permission.length > 0 && (
        <div style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
          <Pill tone="muted">not yours to see</Pill>
          <span style={{ fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.55 }}>
            {permission.map((w) => w.panel).join(", ")} — your own permissions do
            not cover these. Verifying the caller will not change that.
          </span>
        </div>
      )}
    </Card>
  );
}

function Identifiers({ view, customerId, interactionId, onChanged }: {
  view: ThreeSixty; customerId: string; interactionId: string | null;
  onChanged: () => void;
}) {
  const [revealing, setRevealing] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const rows = view.panels.identifiers ?? [];

  return (
    <Card>
      <SectionTitle>How we know them</SectionTitle>
      {rows.length === 0 && (
        <p style={{ ...muted, margin: 0 }}>No identifiers on this profile.</p>
      )}
      <div style={{ display: "grid", gap: 10 }}>
        {rows.map((r) => (
          <div key={r.kind} style={{ display: "flex", gap: 10, alignItems: "center",
                                     flexWrap: "wrap" }}>
            <span style={{ ...muted, width: 90, flexShrink: 0,
                           fontFamily: "var(--font-mono)" }}>
              {r.kind}
            </span>
            <span style={{ ...MONO, fontSize: 13, color: "var(--text-primary)" }}>
              {r.value}
            </span>
            {/* An unverified identifier is how they were FOUND, not proof of who
                they are, and the screen says so. */}
            <Pill tone={r.verified ? "ok" : "muted"}>
              {r.verified ? `confirmed by ${r.verified_by}` : "unconfirmed"}
            </Pill>
            {r.masked && r.revealable && (
              revealing === r.kind ? (
                <>
                  <TextInput value={reason} onChange={(e) => setReason(e.target.value)}
                             placeholder="Why you need to see it"
                             style={{ width: 240 }} autoFocus />
                  <Button size="sm" disabled={reason.trim().length < 5} onClick={async () => {
                    setError(null);
                    try {
                      await engageReveal(customerId, {
                        field_path: `identifiers.${r.kind}`, reason,
                        interaction_id: interactionId });
                      setRevealing(null); setReason(""); onChanged();
                    } catch (e) { setError(String(e)); }
                  }}>Reveal</Button>
                  <Button size="sm" variant="ghost"
                          onClick={() => { setRevealing(null); setReason(""); }}>
                    Cancel
                  </Button>
                </>
              ) : (
                <Button size="sm" variant="secondary"
                        onClick={() => setRevealing(r.kind)}>Reveal</Button>
              )
            )}
          </div>
        ))}
      </div>
      {/* No "show all". There is deliberately no endpoint that could become one. */}
      <p style={{ ...muted, margin: "var(--space-md) 0 0" }}>
        One field at a time, each with a reason, each recorded.
      </p>
      {error && <ErrorNote style={{ marginTop: "var(--space-sm)" }}>{error}</ErrorNote>}
    </Card>
  );
}

function Verify({ interactionId, tier, onChanged }: {
  interactionId: string; tier: string; onChanged: () => void;
}) {
  const [factor, setFactor] = useState("otp");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const attempt = async (ok: boolean) => {
    setBusy(true); setMessage(null);
    try {
      const out = await engageVerify(interactionId, { factor, ok, to_tier: "full" });
      // The server's single message, printed unchanged. Explaining which factor
      // failed would build the oracle the server refuses to be.
      setMessage(out.verified ? `Verified — ${TIER_LABEL[out.verification_level]}`
                              : (out.message ?? "Could not verify."));
      onChanged();
    } catch (e) { setMessage(String(e)); }
    finally { setBusy(false); }
  };

  if (tier === "full") {
    return (
      <Card style={{ borderLeft: "3px solid var(--status-completed)", display: "flex",
                     alignItems: "center", gap: "var(--space-sm)" }}>
        <Pill tone="ok">verified</Pill>
        <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>
          Nothing further is needed on this call.
        </span>
      </Card>
    );
  }
  return (
    <Card style={{ borderLeft: "3px solid var(--status-running)" }}>
      <SectionTitle>Verify the caller</SectionTitle>
      <div style={{ display: "flex", gap: "var(--space-sm)", alignItems: "center",
                    flexWrap: "wrap" }}>
        <Select value={factor} onChange={(e) => setFactor(e.target.value)}>
          <option value="otp">One-time code</option>
          <option value="idp">Signed-in session</option>
          <option value="document">Document check</option>
          <option value="call">Confirmed on the call</option>
        </Select>
        <Button disabled={busy} onClick={() => attempt(true)}>They passed</Button>
        <Button variant="secondary" disabled={busy} onClick={() => attempt(false)}>
          They failed
        </Button>
      </div>
      {message && (
        <SubPanel>
          <span style={{ fontSize: 13, color: "var(--text-primary)" }}>{message}</span>
        </SubPanel>
      )}
    </Card>
  );
}

export default function RepDesk() {
  const [status, setStatus] = useState<EngageStatus | null>(null);
  const [ani, setAni] = useState("");
  const [pop, setPop] = useState<ScreenPop | null>(null);
  const [view, setView] = useState<ThreeSixty | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [view_, setViewTab] = useState<"desk" | "floor">("desk");

  useEffect(() => {
    getEngageStatus().then(setStatus).catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  const refresh = useCallback(async () => {
    if (!pop?.customer_id) return;
    try { setView(await engage360(pop.customer_id, pop.interaction_id)); }
    catch (e) { setError(String(e)); }
  }, [pop]);
  useEffect(() => { void refresh(); }, [refresh]);

  const takeCall = async () => {
    setBusy(true); setError(null); setNote(null); setView(null);
    try {
      const out = await engageScreenPop({ channel: "voice", ani });
      setPop(out);
      if (out.customer_id) setView(await engage360(out.customer_id, out.interaction_id));
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  if (loading) {
    return (
      <div style={{ display: "flex", justifyContent: "center", padding: "var(--space-2xl)" }}>
        <Spinner size={24} />
      </div>
    );
  }

  // Two gates, and the rep is told which one. A dark tenant is not a fault.
  if (status && !status.enabled) {
    return (
      <div style={{ display: "flex", flexDirection: "column", width: "100%", height: "100%" }}>
        <Banner tone="muted" title="Rep Desk is not available here.">
          {status.released
            ? "This tenant has not installed Customer Engagement from the marketplace."
            : "Customer Engagement has not been released on this platform."}
        </Banner>
        <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-lg) var(--space-2xl)" }}>
          <EmptyState
            title="Nothing to show"
            description={status.released
              ? "An administrator installs Customer Engagement from the marketplace before a rep can take a call here."
              : "Customer Engagement has not been released on this platform."} />
        </div>
      </div>
    );
  }

  const tier = pop?.verification_level ?? "none";
  const summary = view?.panels.summary;

  return (
    <div style={{ display: "flex", flexDirection: "column", width: "100%", height: "100%",
                  overflow: "hidden", boxSizing: "border-box" }}>
      {/* No page title here: `AppLayout` already renders one from `nav-data`. */}
      <Tabs ariaLabel="Rep desk" value={view_} onChange={setViewTab}
            tabs={[{ key: "desk" as const, label: "Desk" },
                   { key: "floor" as const, label: "Floor" }]} />

      {view_ === "floor" ? (
        <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-lg) var(--space-2xl)",
                      boxSizing: "border-box" }}>
          <FloorAnalytics />
        </div>
      ) : (<>

      {/* The call bar. Fixed above the scroll so it cannot leave the screen
          mid-call. */}
      <div style={{ display: "flex", gap: "var(--space-sm)", alignItems: "center",
                    padding: "var(--space-md) var(--space-2xl)", flexShrink: 0,
                    background: "var(--bg-panel)",
                    borderBottom: "1px solid var(--border-subtle)" }}>
        <TextInput value={ani} onChange={(e) => setAni(e.target.value)}
                   placeholder="Caller's number, e.g. +44 7700 900000"
                   style={{ flex: 1, maxWidth: 420 }}
                   onKeyDown={(e) => { if (e.key === "Enter" && !busy) void takeCall(); }} />
        <Button disabled={busy} onClick={takeCall}>Take the call</Button>
        {pop?.customer_id && (
          <div style={{ marginLeft: "auto", display: "flex", gap: "var(--space-sm)",
                        alignItems: "center" }}>
            {/* Displayed, never chosen. */}
            <Pill tone={TIER_TONE[tier]}>{TIER_LABEL[tier]}</Pill>
            {pop.inherited_verification && (
              <Pill tone="ok">transferred — already verified</Pill>
            )}
          </div>
        )}
      </div>

      <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-lg) var(--space-2xl)",
                    boxSizing: "border-box" }}>
        {error && <ErrorNote style={{ marginBottom: "var(--space-md)" }}>{error}</ErrorNote>}
        {note && (
          <Card style={{ marginBottom: "var(--space-md)", borderLeft: "3px solid var(--accent)" }}>
            <span style={{ fontSize: 13, color: "var(--text-primary)" }}>{note}</span>
          </Card>
        )}

        {pop && !pop.customer_id && (
          <Card style={{ borderLeft: "3px solid var(--status-running)" }}>
            <Pill tone="warn">nobody matched</Pill>
            <p style={{ fontSize: 13, color: "var(--text-primary)", margin: "8px 0 0",
                        lineHeight: 1.55 }}>
              {pop.stub?.note}
            </p>
            <p style={{ ...muted, margin: "6px 0 0", lineHeight: 1.55 }}>
              The call is still recorded. Link it to an existing customer, or create
              a profile — Velaris will not invent one from the number.
            </p>
          </Card>
        )}

        {pop?.customer_id && (
          <div style={{ display: "grid",
                        gridTemplateColumns: "repeat(auto-fit, minmax(420px, 1fr))",
                        gap: "var(--space-md)", alignItems: "start" }}>

            {/* Left column: who is on the call, and proving it. */}
            <div style={{ display: "grid", gap: "var(--space-md)" }}>
              <Card>
                <div style={{ display: "flex", gap: 12, alignItems: "baseline",
                              flexWrap: "wrap" }}>
                  <strong style={{ fontSize: 18, fontWeight: 700,
                                   color: "var(--text-primary)",
                                   fontFamily: "var(--font-display)" }}>
                    {summary?.display_name ?? `${summary?.name_initial ?? "?"}…`}
                  </strong>
                  <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>
                    {summary?.open_cases ?? 0} open case
                    {summary?.open_cases === 1 ? "" : "s"}
                  </span>
                </div>
                {view?.freshness?.stale && (
                  <SubPanel tone="warn">
                    {/* Shown, never hidden. */}
                    <span style={{ fontSize: 13, color: "var(--text-primary)" }}>
                      {view.freshness.reason}
                    </span>
                  </SubPanel>
                )}
                <p style={{ ...muted, margin: "10px 0 0", lineHeight: 1.55 }}>
                  found by {pop.resolution.confidence ?? "—"} (rung{" "}
                  {pop.resolution.rung})
                  {/* Gated on the CURRENT tier, not on the resolution flag. That
                      flag is from the original screen-pop and never moves, so
                      reading it here left "a suggestion until the caller is
                      verified" on screen after the caller had been verified —
                      telling a rep the identity was still unproven when it was.
                      Found by rendering it, not by reading it. */}
                  {pop.resolution.verified === false && tier !== "full"
                    ? " — a suggestion until the caller is verified" : ""}
                </p>
              </Card>

              <Verify interactionId={pop.interaction_id} tier={tier}
                      onChanged={async () => {
                        const out = await engage360(pop.customer_id as string,
                                                    pop.interaction_id);
                        setView(out);
                        setPop({ ...pop, verification_level: out.verification });
                      }} />

              {view && <Withheld view={view} />}

              <Card>
                <SectionTitle>Actions</SectionTitle>
                <div style={{ display: "flex", gap: "var(--space-sm)", flexWrap: "wrap" }}>
                  <Button disabled={busy} onClick={async () => {
                    setBusy(true);
                    try {
                      const out = await engageOnBehalf(pop.customer_id as string, {
                        interaction_id: pop.interaction_id, purpose: "case_management" });
                      setNote(out.consent.granted
                        ? `Ready to raise — attributed to you, stamped ${out.attribution.verification_level}.`
                        : `Consent for this purpose is not on record. ${out.consent.note ?? ""}`);
                    } catch (e) { setError(String(e)); }
                    finally { setBusy(false); }
                  }}>Raise a case for them</Button>

                  <Button variant="secondary" disabled={busy} onClick={async () => {
                    setBusy(true);
                    try {
                      const out = await engageTransfer(pop.interaction_id,
                                                       { to_agent: "next-available" });
                      setNote(out.note);
                    } catch (e) { setError(String(e)); }
                    finally { setBusy(false); }
                  }}>Warm transfer</Button>

                </div>
                <div style={{ marginTop: "var(--space-sm)" }}>
                  {/* The tenant's codes when it named some; free text when not. */}
                  <WrapUpPicker busy={busy} onWrap={async (code) => {
                    setBusy(true);
                    try {
                      await engageWrapUp(pop.interaction_id, { disposition_code: code });
                      setNote("Call wrapped up.");
                    } catch (e) { setError(String(e)); }
                    finally { setBusy(false); }
                  }} />
                </div>
              </Card>
            </div>

            {/* Right column: what we hold on them. */}
            <div style={{ display: "grid", gap: "var(--space-md)" }}>
              <NextActions customerId={pop.customer_id} interactionId={pop.interaction_id}
                           tier={tier} />
              <Recap customerId={pop.customer_id} interactionId={pop.interaction_id}
                     tier={tier} />
              {view?.panels.identifiers && (
                <Identifiers view={view} customerId={pop.customer_id}
                             interactionId={pop.interaction_id} onChanged={refresh} />
              )}

              {view?.panels.interactions && (
                <Card>
                  <SectionTitle>Previous contacts</SectionTitle>
                  {view.panels.interactions.length === 0
                    ? <p style={{ ...muted, margin: 0 }}>No earlier contacts.</p>
                    : <div style={{ display: "grid", gap: 8 }}>
                        {view.panels.interactions.map((i) => (
                          <div key={i.id} style={{ display: "flex", gap: 10,
                                                   alignItems: "baseline", flexWrap: "wrap",
                                                   paddingBottom: 8,
                                                   borderBottom: "1px solid var(--border-subtle)" }}>
                            <Pill tone="muted">{i.channel}</Pill>
                            <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>
                              {i.started_at ? new Date(i.started_at).toLocaleString() : "—"}
                            </span>
                            {i.disposition_code && (
                              <span style={{ ...MONO, color: "var(--text-muted)" }}>
                                {i.disposition_code}
                              </span>
                            )}
                          </div>
                        ))}
                      </div>}
                </Card>
              )}
            </div>
          </div>
        )}

        {!pop && (
          <EmptyState title="No call in progress"
                      description="Enter the caller's number above to open the contact." />
        )}
      </div>
      </>)}
    </div>
  );
}
