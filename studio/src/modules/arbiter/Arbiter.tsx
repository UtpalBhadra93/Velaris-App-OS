/**
 * HxArbiter — sovereignty & security policy engine (the PAP).
 *
 * This screen exists to make three things visible that are otherwise invisible,
 * and each tab is one of them:
 *
 *   1. **Nothing here is enforcing yet.** Every namespace ships clamped to
 *      `shadow`, so the engine computes and logs and changes no answer anybody
 *      receives. The Namespaces tab says so on every row rather than burying it
 *      in a settings page — an operator who does not know their policies are
 *      inert will trust them.
 *   2. **A promotion has to be earned.** Parity is the evidence, and the server
 *      refuses `enforce` without it. The UI's job is to show WHY it was refused,
 *      including the case people find most surprising: a namespace nobody
 *      exercised agrees perfectly, and that is not evidence of anything.
 *   3. **Where a rule came from.** A draft carries its model, its provider and
 *      how many attempts it took to compile. "Your compliance control was
 *      written by an LLM" is the first question in the room, and this answers it
 *      on the screen rather than in a log.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useState } from "react";

import {
  activateArbiterDraft,
  adviseArbiterUpkeep,
  anchorArbiterDecisions,
  exportArbiterPolicies,
  importArbiterPolicies,
  purgeArbiterDecisions,
  setArbiterRetention,
  approveArbiterDraft,
  createArbiterDraft,
  getArbiterCatalog,
  getArbiterParity,
  getArbiterUpkeep,
  listArbiterDecisions,
  listArbiterPolicies,
  promoteArbiterNamespace,
  retireArbiterPolicy,
  simulateArbiter,
  syncArbiterBridge,
  verifyArbiterChain,
  type ArbiterCatalog,
  type ArbiterDecision,
  type ArbiterDraft,
  type ArbiterParity,
  type ArbiterPolicy,
  type ArbiterUpkeep,
  type ArbiterUpkeepAdvice,
} from "@shared/api/client";
import {
  Banner, Button, Card, EmptyState, ErrorNote, FIELD_STYLE, MONO, Pill, Select,
  Spinner, TABLE, Tabs, TextInput,
} from "@shared/components";

type TabKey = "policies" | "namespaces" | "decisions" | "parity" | "upkeep"
  | "evidence" | "author" | "simulate";

const TABS = [
  { key: "policies"   as const, label: "Policies" },
  { key: "namespaces" as const, label: "Namespaces" },
  { key: "decisions"  as const, label: "Decision log" },
  { key: "parity"     as const, label: "Parity" },
  { key: "upkeep"     as const, label: "Upkeep" },
  { key: "evidence"   as const, label: "Evidence" },
  { key: "author"     as const, label: "Author" },
  { key: "simulate"   as const, label: "Simulate" },
];

//: The UI's own wording for each suggested action. The model chooses a key and
//: never supplies prose that an operator could hand to another model.
const ACTION_LABEL: Record<string, string> = {
  none: "nothing to do",
  review_rules: "read the rules involved — this is a judgement, not a fix",
  amend_policy: "the rules need changing",
  check_pep_wiring: "check the enforcement point is wired at the call site",
  run_bridge_sync: "regenerate the legacy mirror on the Namespaces tab",
  review_promotion: "the parity evidence supports enforcing — your decision",
  check_sealer: "the decision log is not being sealed",
  investigate_now: "stop and investigate — the chain does not verify",
};

export default function Arbiter() {
  const [tab, setTab] = useState<TabKey>("policies");
  const [catalog, setCatalog] = useState<ArbiterCatalog | null>(null);
  const [policies, setPolicies] = useState<ArbiterPolicy[]>([]);
  const [decisions, setDecisions] = useState<ArbiterDecision[]>([]);
  const [parity, setParity] = useState<ArbiterParity | null>(null);
  const [parityNs, setParityNs] = useState("ai");
  const [chain, setChain] = useState<{ ok: boolean; problem: string | null } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [intent, setIntent] = useState("");
  const [draftNs, setDraftNs] = useState("ai");
  const [draft, setDraft] = useState<ArbiterDraft | null>(null);
  const [readback, setReadback] = useState("");

  const [exported, setExported] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const [upkeep, setUpkeep] = useState<ArbiterUpkeep | null>(null);
  const [upkeepDays, setUpkeepDays] = useState(30);
  const [advice, setAdvice] = useState<ArbiterUpkeepAdvice | null>(null);
  // Separate from `error`: no provider is the feature being ABSENT, not the
  // page failing. It is shown next to the button it disables and nowhere else.
  const [adviceUnavailable, setAdviceUnavailable] = useState<string | null>(null);

  const [simAction, setSimAction] = useState("ai.external_call");
  const [simAttrs, setSimAttrs] = useState('{\n  "tenant.region": "eu-west",\n  "context.provider_class": "external"\n}');
  const [simResult, setSimResult] = useState<Record<string, unknown> | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    // `allSettled`, not `all`. The four calls need DIFFERENT privileges — the
    // decision log and the chain are `arbiter.decisions.read` (admin,
    // security), while the catalogue and policies are `arbiter.policies.read`
    // (which a developer also holds). With `Promise.all` a single 403 on a call
    // the viewer was never entitled to make would blank the whole page,
    // including the tabs they ARE allowed to use. A tab the caller cannot read
    // stays empty; nothing else is affected.
    const [c, p, d, v] = await Promise.allSettled([
      getArbiterCatalog(),
      listArbiterPolicies(),
      listArbiterDecisions({ limit: 100 }),
      verifyArbiterChain(),
    ]);
    if (c.status === "fulfilled") setCatalog(c.value);
    if (p.status === "fulfilled") setPolicies(p.value);
    if (d.status === "fulfilled") setDecisions(d.value);
    if (v.status === "fulfilled") setChain(v.value);

    // Only report a failure the viewer could not have expected: if the
    // catalogue itself is unreachable, the page genuinely cannot work.
    if (c.status === "rejected") {
      setError(c.reason instanceof Error ? c.reason.message : String(c.reason));
    }
    setLoading(false);
  }, []);

  useEffect(() => { void load(); }, [load]);

  const enforcing = (catalog?.namespaces ?? []).filter(n => n.max_mode === "enforce").length;

  async function act<T>(fn: () => Promise<T>) {
    setBusy(true); setError(null);
    try { await fn(); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* No PageHeader here: `AppLayout` already renders one from `nav-data`,
          so a module adding its own gets two stacked title bars. */}

      {/* The single most important fact about this module, stated once, at the
          top, on every tab: an operator who thinks their policies are live when
          they are in shadow will trust an answer nothing produced. */}
      {catalog && enforcing === 0 && (
        <Banner tone="warn" title="Every namespace is in shadow.">
          Policies are evaluated and logged, and change no answer anybody receives.
          Promote a namespace on the Namespaces tab once its parity is proven.
        </Banner>
      )}

      <Tabs tabs={TABS} value={tab} onChange={k => setTab(k as TabKey)} />

      <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-lg) var(--space-2xl)",
                    boxSizing: "border-box" }}>
        {notice && (
          <Card style={{ marginBottom: "var(--space-md)",
                         borderLeft: "3px solid var(--accent)" }}>
            <span style={{ fontSize: 13, color: "var(--text-primary)" }}>{notice}</span>
          </Card>
        )}
        {error && <ErrorNote style={{ marginBottom: "var(--space-md)" }}>{error}</ErrorNote>}
        {loading ? <Spinner /> : (
          <>
            {tab === "policies" && (
              <Card>
                <div style={{ display: "flex", justifyContent: "space-between",
                              alignItems: "center", marginBottom: 12 }}>
                  <h3 style={{ margin: 0, fontSize: 14 }}>Policies</h3>
                  <Button disabled={busy} onClick={() => act(syncArbiterBridge)}>
                    Regenerate legacy mirrors
                  </Button>
                </div>
                {policies.length === 0 ? (
                  <EmptyState title="No policies yet"
                    description="Regenerate the legacy mirrors to see what the controls you already run would look like as policy, or write one on the Author tab." />
                ) : (
                  <table style={TABLE.table}>
                    <thead>
                      <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
                        <th style={TABLE.th}>Name</th>
                        <th style={TABLE.th}>Namespace</th>
                        <th style={TABLE.th}>Scope</th>
                        <th style={TABLE.th}>Status</th>
                        <th style={TABLE.th}>Version</th>
                        <th style={TABLE.th} />
                      </tr>
                    </thead>
                    <tbody>
                      {policies.map(p => (
                        <tr key={p.id} style={{ borderTop: "1px solid var(--border-subtle)" }}>
                          <td style={TABLE.td}>
                            {p.name}
                            {p.origin === "legacy_bridge" && (
                              <> <Pill tone="muted">generated mirror</Pill></>
                            )}
                          </td>
                          <td style={{ padding: 8, ...MONO }}>{p.namespace}</td>
                          <td style={TABLE.td}>
                            {p.scope === "platform"
                              ? <Pill tone="warn">platform — binds every tenant</Pill>
                              : "tenant"}
                          </td>
                          <td style={TABLE.td}>{p.status}</td>
                          <td style={{ padding: 8, ...MONO }}>{p.active_version ?? "—"}</td>
                          <td style={{ padding: 8, textAlign: "right" }}>
                            {p.status === "active" && (
                              <Button variant="ghost" disabled={busy}
                                onClick={() => act(() => retireArbiterPolicy(p.id))}>
                                Retire
                              </Button>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </Card>
            )}

            {tab === "namespaces" && catalog && (
              <Card>
                <h3 style={{ margin: "0 0 4px", fontSize: 14 }}>Namespaces</h3>
                <p style={{ margin: "0 0 14px", fontSize: 12,
                            color: "var(--text-muted)" }}>
                  The ceiling holds every policy in a namespace down, whatever the
                  policy itself says. Raising it to <code>enforce</code> is what
                  makes decisions binding — and the server refuses that without
                  parity evidence.
                </p>
                <table style={TABLE.table}>
                  <thead>
                    <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
                      <th style={TABLE.th}>Namespace</th>
                      <th style={TABLE.th}>Default</th>
                      <th style={TABLE.th}>Ceiling</th>
                      <th style={TABLE.th}>Description</th>
                      <th style={TABLE.th} />
                    </tr>
                  </thead>
                  <tbody>
                    {catalog.namespaces.map(n => (
                      <tr key={n.namespace} style={{ borderTop: "1px solid var(--border-subtle)" }}>
                        <td style={{ padding: 8, ...MONO }}>{n.namespace}</td>
                        <td style={TABLE.td}>
                          <Pill tone={n.default_effect === "deny" ? "bad" : "ok"}>
                            {n.default_effect}
                          </Pill>
                        </td>
                        <td style={TABLE.td}>
                          {n.max_mode === "enforce"
                            ? <Pill tone="ok">enforcing</Pill>
                            : <Pill tone="muted">{n.max_mode} — changes nothing</Pill>}
                        </td>
                        <td style={{ padding: 8, color: "var(--text-muted)" }}>
                          {n.description}
                        </td>
                        <td style={{ padding: 8, textAlign: "right" }}>
                          {n.max_mode === "enforce" ? (
                            <Button variant="ghost" disabled={busy}
                              onClick={() => act(() => promoteArbiterNamespace(
                                n.namespace, { max_mode: "shadow" }))}>
                              Back to shadow
                            </Button>
                          ) : (
                            <Button disabled={busy}
                              onClick={() => act(() => promoteArbiterNamespace(
                                n.namespace, { max_mode: "enforce" }))}>
                              Promote to enforce
                            </Button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Card>
            )}

            {tab === "evidence" && (
              <Card>
                <h3 style={{ margin: "0 0 4px", fontSize: 14 }}>Evidence</h3>
                <p style={{ margin: "0 0 16px", fontSize: 12,
                            color: "var(--text-muted)" }}>
                  The decision log is hash-chained, which proves it has not been
                  edited by anything that writes to it. An external timestamp
                  proves more: that nothing older than the receipt can be forged
                  even by someone who can rewrite the whole chain. Retention and
                  portability live here too — both are platform-operator actions.
                </p>

                <div style={{ display: "flex", gap: 8, flexWrap: "wrap",
                              marginBottom: 20 }}>
                  <Button disabled={busy} onClick={() => act(async () => {
                    const r = await anchorArbiterDecisions() as
                      Record<string, unknown>;
                    setNotice(r.anchored
                      ? `Anchored at sequence ${r.tip_sequence}.`
                      : `Not anchored: ${String(r.reason)}.`);
                  })}>
                    Anchor the chain now
                  </Button>
                  <Button variant="ghost" disabled={busy}
                    onClick={() => act(async () => {
                      const doc = await exportArbiterPolicies();
                      setExported(JSON.stringify(doc, null, 2));
                      setNotice("Exported. Copy the document below.");
                    })}>
                    Export policies
                  </Button>
                </div>

                <h4 style={{ margin: "0 0 6px", fontSize: 13 }}>Retention</h4>
                <p style={{ margin: "0 0 10px", fontSize: 12,
                            color: "var(--text-muted)" }}>
                  Blank means <strong>kept forever</strong>, which is what every
                  namespace does until you say otherwise. Setting a horizon
                  deletes nothing on its own — purging is a separate act, and it
                  never touches a decision the chain has not yet sealed.
                </p>
                <table style={{ width: "100%", fontSize: 13,
                                borderCollapse: "collapse", marginBottom: 12 }}>
                  <tbody>
                    {(catalog?.namespaces ?? []).map(n => (
                      <tr key={n.namespace}>
                        <td style={{ padding: "6px 8px", ...MONO }}>{n.namespace}</td>
                        <td style={{ padding: "6px 8px" }}>
                          <TextInput
                            type="number" min={1} max={3650}
                            placeholder="forever"
                            defaultValue={n.decision_retention_days ?? ""}
                            style={{ width: 110, padding: "5px 10px" }}
                            onBlur={e => {
                              const raw = e.target.value.trim();
                              const days = raw === "" ? null : Number(raw);
                              if (days === (n.decision_retention_days ?? null)) return;
                              void act(async () => {
                                await setArbiterRetention(n.namespace, days);
                                setNotice(`${n.namespace}: retention set to `
                                  + (days === null ? "forever" : `${days} days`));
                              });
                            }} />
                          <span style={{ marginLeft: 8, fontSize: 12,
                                         color: "var(--text-muted)" }}>
                            days
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <Button variant="ghost" disabled={busy}
                  onClick={() => act(async () => {
                    const r = await purgeArbiterDecisions() as
                      Record<string, unknown>;
                    setNotice(`Purged ${String(r.total)} decision(s).`);
                  })}>
                  Purge past the horizon
                </Button>

                <h4 style={{ margin: "20px 0 6px", fontSize: 13 }}>Import</h4>
                <p style={{ margin: "0 0 8px", fontSize: 12,
                            color: "var(--text-muted)" }}>
                  Everything lands in <strong>shadow</strong> and is not
                  activated, whatever the document says. A document is somebody
                  else's judgement about somebody else's platform.
                </p>
                <textarea value={exported} onChange={e => setExported(e.target.value)}
                  rows={8} placeholder="Paste an exported document here"
                  style={{ ...FIELD_STYLE, ...MONO, width: "100%", resize: "vertical" }} />
                <div style={{ marginTop: 8 }}>
                  <Button disabled={busy || !exported.trim()}
                    onClick={() => act(async () => {
                      let doc: unknown;
                      try { doc = JSON.parse(exported); }
                      catch { throw new Error("That is not valid JSON."); }
                      const r = await importArbiterPolicies({ document: doc }) as
                        Record<string, unknown>;
                      setNotice(`Imported ${String(r.imported)} policy(ies) in shadow.`);
                    })}>
                    Import
                  </Button>
                </div>
              </Card>
            )}

            {tab === "decisions" && (
              <Card>
                <div style={{ display: "flex", justifyContent: "space-between",
                              alignItems: "center", marginBottom: 12 }}>
                  <h3 style={{ margin: 0, fontSize: 14 }}>Decision log</h3>
                  {chain && (chain.ok
                    ? <Pill tone="ok">chain verified</Pill>
                    : <Pill tone="bad">TAMPERED — {chain.problem}</Pill>)}
                </div>
                {decisions.length === 0 ? (
                  <EmptyState title="No decisions recorded"
                    description="Nothing has asked the engine yet. An empty log is also why a parity report cannot justify a promotion." />
                ) : (
                  <table style={{ ...TABLE.table, ...MONO }}>
                    <thead>
                      <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
                        <th style={{ padding: 6 }}>When</th>
                        <th style={{ padding: 6 }}>Action</th>
                        <th style={{ padding: 6 }}>Subject</th>
                        <th style={{ padding: 6 }}>Engine</th>
                        <th style={{ padding: 6 }}>Legacy</th>
                        <th style={{ padding: 6 }}>Binding</th>
                        <th style={{ padding: 6 }}>Sealed</th>
                      </tr>
                    </thead>
                    <tbody>
                      {decisions.map(d => (
                        <tr key={d.id} style={{ borderTop: "1px solid var(--border-subtle)" }}>
                          <td style={{ padding: 6 }}>{new Date(d.evaluated_at).toLocaleString()}</td>
                          <td style={{ padding: 6 }}>{d.action}</td>
                          <td style={{ padding: 6 }}>
                            {d.subject_id}
                            {d.claimed_by && d.claimed_by !== d.subject_id && (
                              <span style={{ color: "var(--text-muted)" }}>
                                {" "}(asserted by {d.claimed_by})
                              </span>
                            )}
                          </td>
                          <td style={{ padding: 6 }}>
                            <Pill tone={d.effect === "deny" ? "bad" : "ok"}>{d.effect}</Pill>
                            {d.error && <> <Pill tone="warn">{d.error}</Pill></>}
                          </td>
                          <td style={{ padding: 6 }}>
                            {d.legacy_effect
                              ? (d.legacy_effect === d.effect
                                  ? d.legacy_effect
                                  : <Pill tone="warn">{d.legacy_effect} — disagreed</Pill>)
                              : "—"}
                          </td>
                          <td style={{ padding: 6 }}>{d.enforced ? "yes" : "no"}</td>
                          <td style={{ padding: 6 }}>{d.sealed ? "yes" : "pending"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </Card>
            )}

            {tab === "parity" && (
              <Card>
                <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 14 }}>
                  <Select value={parityNs} onChange={e => setParityNs(e.target.value)}
                          style={{ padding: 6, fontSize: 13 }}>
                    {(catalog?.namespaces ?? []).map(n =>
                      <option key={n.namespace} value={n.namespace}>{n.namespace}</option>)}
                  </Select>
                  <Button disabled={busy} onClick={() => act(async () =>
                    setParity(await getArbiterParity(parityNs)))}>
                    Run parity report
                  </Button>
                </div>
                {!parity ? (
                  <EmptyState title="No report yet"
                    description="Parity compares what the engine decided against what the control it shadows decided. It is the evidence a promotion rests on." />
                ) : (
                  <div style={{ fontSize: 13 }}>
                    <p>
                      <strong>{parity.compared}</strong> compared decisions over{" "}
                      {parity.window_days} days —{" "}
                      <strong>{(parity.agreement_rate * 100).toFixed(2)}%</strong> agreement.{" "}
                      {parity.eligible_for_promotion
                        ? <Pill tone="ok">eligible for promotion</Pill>
                        : <Pill tone="bad">not eligible</Pill>}
                    </p>
                    <p style={{ color: "var(--text-muted)" }}>
                      Engine stricter than the legacy control: {parity.engine_stricter} ·
                      engine weaker: {parity.engine_weaker} · unevaluable: {parity.errors}
                    </p>
                    {parity.reasons.length > 0 && (
                      <ul style={{ paddingLeft: 18 }}>
                        {parity.reasons.map((r, i) => <li key={i}>{r}</li>)}
                      </ul>
                    )}
                  </div>
                )}
              </Card>
            )}

            {tab === "upkeep" && (
              <Card>
                <div style={{ display: "flex", justifyContent: "space-between",
                              alignItems: "center", marginBottom: 4 }}>
                  <h3 style={{ margin: 0, fontSize: 14 }}>Upkeep</h3>
                  <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <Select value={upkeepDays} disabled={busy}
                      onChange={e => { setUpkeepDays(Number(e.target.value));
                                       setUpkeep(null); setAdvice(null); }}>
                      {[7, 30, 90, 365].map(d =>
                        <option key={d} value={d}>last {d} days</option>)}
                    </Select>
                    <Button disabled={busy} onClick={() => act(async () => {
                      setAdvice(null); setAdviceUnavailable(null);
                      setUpkeep(await getArbiterUpkeep(upkeepDays));
                    })}>
                      {upkeep ? "Re-run" : "Run check"}
                    </Button>
                  </div>
                </div>
                <p style={{ margin: "0 0 12px", fontSize: 12,
                            color: "var(--text-muted)" }}>
                  Every finding below is computed from the database — no model is
                  involved and none is needed. A policy set drifts after it is
                  written: an attribute disappears from the catalogue and the
                  policy naming it is skipped silently, two policies authored
                  months apart start contradicting each other, a mirror stops
                  being regenerated and the parity report quietly measures
                  against a stale oracle.
                </p>

                {!upkeep ? (
                  <EmptyState title="Nothing checked yet"
                    description="Run the check to see what the live policy set is doing." />
                ) : (
                  <>
                    <div style={{ display: "flex", gap: 8, alignItems: "center",
                                  flexWrap: "wrap", marginBottom: 12 }}>
                      <Pill tone={upkeep.counts.error ? "bad" : "ok"}>
                        {upkeep.counts.error} error
                      </Pill>
                      <Pill tone={upkeep.counts.warning ? "warn" : "muted"}>
                        {upkeep.counts.warning} warning
                      </Pill>
                      <Pill tone="muted">{upkeep.counts.info} info</Pill>
                      <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
                        {upkeep.policies_examined} policies ·{" "}
                        {upkeep.decisions_examined.toLocaleString()} decisions
                        scanned · {upkeep.scope} scope
                      </span>
                      <div style={{ marginLeft: "auto", display: "flex", gap: 8,
                                    alignItems: "center" }}>
                        {/* Absent, not broken: a 503 here means no AI provider
                            is configured, and the report above is complete
                            without one. */}
                        {adviceUnavailable && (
                          <span style={{ fontSize: 12,
                                         color: "var(--text-muted)" }}>
                            {adviceUnavailable}
                          </span>
                        )}
                        <Button variant="ghost" disabled={busy || !upkeep.findings.length}
                          onClick={async () => {
                            setBusy(true); setAdviceUnavailable(null);
                            try { setAdvice(await adviseArbiterUpkeep({
                              window_days: upkeep.window_days })); }
                            catch (e) {
                              const msg = e instanceof Error ? e.message : String(e);
                              if (msg.includes("NO-PROVIDER")) {
                                setAdviceUnavailable(
                                  "No AI provider is configured for this tenant, "
                                  + "so there is nothing to explain the findings "
                                  + "with. They stand on their own.");
                              } else { setError(msg); }
                            } finally { setBusy(false); }
                          }}>
                          Explain with HxNexus
                        </Button>
                      </div>
                    </div>

                    {advice?.summary && (
                      <div style={{ padding: 12, borderRadius: 6, marginBottom: 12,
                                    background: "var(--bg-elevated)",
                                    fontSize: 13 }}>
                        {advice.summary}
                        <div style={{ marginTop: 6, fontSize: 11,
                                      color: "var(--text-muted)" }}>
                          Written by {advice.provenance.model ?? "unknown"} (
                          {advice.provenance.provider ?? "unknown"} provider). It
                          was shown the findings only — no case, no person, no
                          decision. It decides nothing.
                          {advice.dropped.length > 0 && (
                            <> {advice.dropped.length} reply entrie(s) referred to
                            findings that were never sent and were discarded.</>
                          )}
                        </div>
                      </div>
                    )}

                    {upkeep.findings.length === 0 ? (
                      <EmptyState title="Nothing to report"
                        description="No drift, no dormancy and no broken seal over this window." />
                    ) : (
                      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                        {upkeep.findings.map(f => {
                          const a = advice?.entries?.[f.id];
                          return (
                            <div key={f.id} style={{
                              border: "1px solid var(--border-subtle)",
                              borderRadius: 6, padding: 12, fontSize: 13 }}>
                              <div style={{ display: "flex", gap: 8,
                                            alignItems: "center", flexWrap: "wrap" }}>
                                <Pill tone={f.severity === "error" ? "bad"
                                          : f.severity === "warning" ? "warn" : "muted"}>
                                  {f.kind}
                                </Pill>
                                {f.namespace && <Pill tone="muted">{f.namespace}</Pill>}
                                {f.scope === "platform" && <Pill tone="muted">platform</Pill>}
                                {/* The most important pill on this screen. A
                                    finding whose evidence cannot support a
                                    conclusion must never look like one that
                                    can. */}
                                {!f.assessable && (
                                  <Pill tone="muted">not conclusive</Pill>
                                )}
                                {a && <Pill tone={a.priority === "now" ? "bad"
                                                : a.priority === "soon" ? "warn" : "muted"}>
                                  {a.priority}
                                </Pill>}
                              </div>
                              <p style={{ margin: "8px 0 0" }}>{f.message}</p>
                              {f.caveat && (
                                <p style={{ margin: "6px 0 0", fontSize: 12,
                                            color: "var(--status-running)" }}>
                                  Why this may mean nothing: {f.caveat}.
                                </p>
                              )}
                              {a?.meaning && (
                                <p style={{ margin: "8px 0 0", fontSize: 12,
                                            color: "var(--text-muted)" }}>
                                  <strong>HxNexus:</strong> {a.meaning}
                                </p>
                              )}
                              {f.suggestion && (
                                <p style={{ margin: "8px 0 0", fontSize: 12,
                                            color: "var(--text-muted)" }}>
                                  {f.suggestion}
                                </p>
                              )}
                              {/* The model picks an action from a closed set;
                                  this file owns the wording, and the Author tab
                                  opens EMPTY. Model text never becomes the next
                                  model's prompt. */}
                              {a && a.action !== "none" && (
                                <div style={{ marginTop: 8, display: "flex",
                                              alignItems: "center", gap: 8 }}>
                                  {a.action === "amend_policy" ? (
                                    <>
                                      <Button variant="ghost" onClick={() => {
                                        setDraftNs(f.namespace ?? draftNs);
                                        setIntent("");
                                        setTab("author");
                                      }}>
                                        Author an amendment
                                      </Button>
                                      <span style={{ fontSize: 12,
                                                     color: "var(--text-muted)" }}>
                                        Opens the Author tab empty — describe the
                                        change in your own words.
                                      </span>
                                    </>
                                  ) : (
                                    <span style={{ fontSize: 12,
                                                   color: "var(--text-muted)" }}>
                                      Suggested next step: {ACTION_LABEL[a.action]}
                                    </span>
                                  )}
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}

                    {upkeep.limitations.length > 0 && (
                      <div style={{ marginTop: 16, fontSize: 12,
                                    color: "var(--text-muted)" }}>
                        <strong>What this run could not check</strong>
                        <ul style={{ paddingLeft: 18, margin: "6px 0 0" }}>
                          {upkeep.limitations.map((l, i) => <li key={i}>{l}</li>)}
                        </ul>
                      </div>
                    )}
                  </>
                )}
              </Card>
            )}

            {tab === "author" && (
              <Card>
                <h3 style={{ margin: "0 0 4px", fontSize: 14 }}>Describe a policy</h3>
                <p style={{ margin: "0 0 12px", fontSize: 12,
                            color: "var(--text-muted)" }}>
                  The model proposes rules and a plain-English readback. The
                  readback is written <em>without</em> your words in front of it,
                  so it describes what the rules do rather than agreeing with what
                  you meant — compare the two before approving.
                </p>
                <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
                  <Select value={draftNs} onChange={e => setDraftNs(e.target.value)}
                          style={{ padding: 6, fontSize: 13 }}>
                    {(catalog?.namespaces ?? []).map(n =>
                      <option key={n.namespace} value={n.namespace}>{n.namespace}</option>)}
                  </Select>
                </div>
                <textarea
                  value={intent} onChange={e => setIntent(e.target.value)}
                  rows={4} placeholder="EU tenants' personal data must never be processed by an external AI provider, regardless of consent."
                  style={{ ...FIELD_STYLE, width: "100%", marginBottom: 8,
                           resize: "vertical", lineHeight: 1.55 }} />
                <Button disabled={busy || intent.trim().length < 8}
                  onClick={() => act(async () => {
                    const d = await createArbiterDraft({ intent, namespace: draftNs });
                    setDraft(d); setReadback(d.readback ?? "");
                  })}>
                  Draft it
                </Button>

                {draft && (
                  <div style={{ marginTop: 16, fontSize: 13 }}>
                    <p style={{ color: "var(--text-muted)" }}>
                      Proposed by <strong>{draft.provenance.model ?? "unknown"}</strong>{" "}
                      ({draft.provenance.provider ?? "unknown"} provider), prompt{" "}
                      {draft.provenance.prompt_version}, {draft.provenance.attempts} attempt(s).
                      {draft.provenance.attempts > 1 && (
                        <> <Pill tone="warn">needed a repair round — read closely</Pill></>
                      )}
                    </p>
                    {(draft.errors ?? []).length > 0 && (
                      <ErrorNote style={{ display: "block", margin: "8px 0" }}>
                        The draft did not compile:
                        <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                          {draft.errors!.map((e, i) => <li key={i}>{e}</li>)}
                        </ul>
                      </ErrorNote>
                    )}
                    {draft.analysis.length > 0 && (
                      <ul style={{ paddingLeft: 18 }}>
                        {draft.analysis.map((f, i) => (
                          <li key={i}>
                            <Pill tone={f.severity === "error" ? "bad"
                                      : f.severity === "warning" ? "warn" : "muted"}>
                              {f.kind}
                            </Pill>{" "}{f.message}
                          </li>
                        ))}
                      </ul>
                    )}
                    <pre style={{ ...MONO, background: "var(--bg-elevated)",
                                  padding: 10, borderRadius: 6, overflowX: "auto" }}>
                      {JSON.stringify(draft.rules, null, 2)}
                    </pre>
                    <label style={{ display: "block", margin: "10px 0 4px" }}>
                      Readback ({draft.readback_source}) — edit it until it is true,
                      then approve. What you approve is what is stored.
                    </label>
                    <textarea value={readback} onChange={e => setReadback(e.target.value)}
                      rows={5} style={{ ...FIELD_STYLE, ...MONO, width: "100%",
                                        resize: "vertical", lineHeight: 1.6 }} />
                    <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                      <Button disabled={busy || !readback.trim()}
                        onClick={() => act(async () => {
                          await approveArbiterDraft(draft.id, readback);
                          setDraft(await import("@shared/api/client")
                            .then(m => m.getArbiterDraft(draft.id)));
                        })}>
                        Approve readback
                      </Button>
                      <Button variant="ghost" disabled={busy || draft.state !== "approved"}
                        onClick={() => act(async () => {
                          await activateArbiterDraft(draft.id);
                          setDraft(null); setIntent(""); setReadback("");
                        })}>
                        Activate in shadow
                      </Button>
                    </div>
                  </div>
                )}
              </Card>
            )}

            {tab === "simulate" && (
              <Card>
                <h3 style={{ margin: "0 0 4px", fontSize: 14 }}>What-if</h3>
                <p style={{ margin: "0 0 12px", fontSize: 12,
                            color: "var(--text-muted)" }}>
                  Runs through the same evaluator the real path uses. Supply the
                  attributes a resolver would otherwise look up — "suppose this
                  tenant were in eu-west". Nothing is enforced.
                </p>
                <TextInput value={simAction} onChange={e => setSimAction(e.target.value)}
                  style={{ width: "100%", marginBottom: 8, ...MONO }} />
                <textarea value={simAttrs} onChange={e => setSimAttrs(e.target.value)}
                  rows={6} style={{ ...FIELD_STYLE, ...MONO, width: "100%",
                                    marginBottom: 8, resize: "vertical" }} />
                <Button disabled={busy} onClick={() => act(async () => {
                  let attributes: Record<string, unknown> = {};
                  try { attributes = JSON.parse(simAttrs); }
                  catch { throw new Error("The attributes box is not valid JSON."); }
                  setSimResult(await simulateArbiter({
                    subject: { kind: "user", id: "simulated" },
                    action: simAction,
                    resource: { kind: "case" },
                    context: {},
                    attributes,
                  }) as Record<string, unknown>);
                })}>
                  Run
                </Button>
                {simResult && (
                  <pre style={{ ...MONO, marginTop: 12,
                                background: "var(--bg-elevated)",
                                padding: 10, borderRadius: 6, overflowX: "auto" }}>
                    {JSON.stringify(simResult, null, 2)}
                  </pre>
                )}
              </Card>
            )}
          </>
        )}
      </div>
    </div>
  );
}
