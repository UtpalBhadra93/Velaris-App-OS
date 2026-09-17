/**
 * Intent-based execution (F-11) — where the plan is read and agreed to.
 *
 * "Plan presentation is consent." That sentence is the reason this screen has
 * to exist: without somewhere to read the step list, the hash-binding underneath
 * it is theatre. So the screen's job is to make the plan legible, and then to
 * hand back the exact hash it displayed.
 *
 * Four things it is careful about:
 *
 *   1. **The confirm sends back the hash that was on screen.** Not one fetched
 *      at click time — a plan that moved between reading and confirming must be
 *      refused, and it can only be refused if the client says what it saw.
 *   2. **Consequential capabilities are called out.** Anything that leaves the
 *      platform or moves money is not something to skim past in a list.
 *   3. **What the model said, and what was dropped**, are both shown. The
 *      containment is only reassuring if you can see it working.
 *   4. **A refusal explains which rung stopped it.** A gap in the fragment
 *      library looks like a refusal from outside, and the rung is the fix.
 *
 * Layout follows the platform shell (`/help`, `/work-center`): no local title —
 * `AppLayout` renders it — a flush `Tabs` bar, and one scrolling body.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useState } from "react";

import {
  confirmIntentPlan,
  createIntentPlan,
  executeIntentPlan,
  listFragments,
  listIntentPlans,
  verifyFragment,
  type IntentFragment,
  type IntentPlan,
} from "@shared/api/client";
import {
  Button, Card, EmptyState, ErrorNote, MONO, Pill, SectionTitle, Spinner,
  SubPanel, TABLE, Tabs, TextInput, type PillTone,
} from "@shared/components";

const CONSEQUENTIAL = new Set([
  "notify.customer", "payment.request", "payment.refund", "document.generate",
]);

const muted: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)" };

const PLAN_TONE: Record<string, PillTone> = {
  refused: "muted", executed: "ok", abandoned: "bad",
  planned: "warn", confirmed: "info",
};

function PlanCard({ plan, onChanged }: { plan: IntentPlan; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); onChanged(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  const consequential = plan.capabilities.filter((c) => CONSEQUENTIAL.has(c));
  const tone = PLAN_TONE[plan.status] ?? "warn";

  return (
    <Card style={{ borderLeft: `3px solid ${
      tone === "ok" ? "var(--status-completed)"
      : tone === "bad" ? "var(--status-failed)"
      : tone === "info" ? "var(--accent)"
      : tone === "muted" ? "var(--border-default)" : "var(--status-running)"}` }}>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <Pill tone={tone}>{plan.status}</Pill>
        <strong style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>
          {plan.intent}
        </strong>
      </div>

      {/* The containment is only reassuring if you can see it working. */}
      <p style={{ ...muted, margin: "8px 0 0", lineHeight: 1.6 }}>
        goal spec from <strong style={{ color: "var(--text-secondary)" }}>{plan.spec_source}</strong> · kept{" "}
        <span style={{ ...MONO, color: "var(--text-secondary)" }}>
          {plan.goal_spec.goals.join(", ") || "nothing"}
        </span>
        {plan.goal_spec.dropped?.length ? (
          <> · <span style={{ ...MONO, color: "var(--status-running)" }}>
            dropped {plan.goal_spec.dropped.join(", ")}
          </span></>
        ) : null}
      </p>

      {plan.status === "refused" && (
        <SubPanel tone="warn">
          <div style={{ fontSize: 13, color: "var(--text-primary)" }}>
            Stopped at <strong style={MONO}>{plan.stopped_at}</strong> — {plan.stopped_reason}
          </div>
        </SubPanel>
      )}

      {plan.steps?.length ? (
        <div style={{ marginTop: "var(--space-md)" }}>
          <div style={{ ...muted, fontFamily: "var(--font-mono)", textTransform: "uppercase",
                        letterSpacing: "0.06em", fontSize: 11, marginBottom: 8 }}>
            What will run
          </div>
          <ol style={{ margin: 0, paddingLeft: 20, fontSize: 13, color: "var(--text-primary)",
                       display: "grid", gap: 8 }}>
            {plan.steps.map((s) => (
              <li key={s.step}>
                <strong style={{ fontWeight: 600 }}>{s.name}</strong>
                <span style={{ color: "var(--text-secondary)" }}> — {s.does}</span>
                <div style={{ ...MONO, color: "var(--text-muted)", marginTop: 2 }}>
                  {s.capabilities.join(", ")}
                </div>
              </li>
            ))}
          </ol>
        </div>
      ) : null}

      {consequential.length > 0 && (
        <SubPanel tone="warn">
          {/* Not something to skim past. */}
          <Pill tone="warn">read this</Pill>
          <p style={{ fontSize: 13, color: "var(--text-primary)", margin: "6px 0 0", lineHeight: 1.55 }}>
            This plan can <strong style={MONO}>{consequential.join(", ")}</strong> — it leaves
            the platform or moves money.
          </p>
        </SubPanel>
      )}

      <p style={{ ...muted, margin: "var(--space-md) 0 0" }}>
        {plan.step_count} steps · data classes{" "}
        {plan.data_classes.join(", ") || "none"}
        {/* A refused plan has no hash. Printing a bare "…" reads as a value
            that was truncated rather than one that does not exist. */}
        {plan.plan_sha256 ? (
          <> · <span style={MONO}>{plan.plan_sha256.slice(0, 16)}…</span></>
        ) : null}
      </p>

      {plan.status === "planned" && plan.plan_sha256 && (
        <div style={{ marginTop: "var(--space-md)", paddingTop: "var(--space-md)",
                      borderTop: "1px solid var(--border-subtle)" }}>
          <Button disabled={busy}
                  onClick={() => act(() =>
                    // The hash THIS screen displayed, not one fetched at click
                    // time. A plan that moved is refused, and can only be
                    // refused if the client says what it saw.
                    confirmIntentPlan(plan.id, plan.plan_sha256 as string))}>
            Confirm this plan
          </Button>
          <p style={{ ...muted, margin: "8px 0 0", lineHeight: 1.55 }}>
            Confirming binds your approval to exactly this process. If it has
            changed since the page loaded, it will be refused.
          </p>
        </div>
      )}

      {plan.status === "confirmed" && (
        <div style={{ display: "flex", gap: "var(--space-sm)", alignItems: "center",
                      flexWrap: "wrap", marginTop: "var(--space-md)",
                      paddingTop: "var(--space-md)", borderTop: "1px solid var(--border-subtle)" }}>
          <Button disabled={busy} onClick={() => act(() => executeIntentPlan(plan.id))}>
            Run it
          </Button>
          <span style={muted}>confirmed by {plan.confirmed_by}</span>
        </div>
      )}

      {plan.status === "abandoned" && (
        <SubPanel tone="bad">
          <p style={{ fontSize: 13, color: "var(--text-primary)", margin: 0, lineHeight: 1.55 }}>
            The composed process no longer matches what was confirmed, so it was
            not run.
          </p>
        </SubPanel>
      )}

      {error && <ErrorNote style={{ marginTop: "var(--space-md)" }}>{error}</ErrorNote>}
    </Card>
  );
}

function FragmentRow({ fragment, onChanged }: {
  fragment: IntentFragment; onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <tr style={TABLE.tr}>
      <td style={{ ...TABLE.td, ...MONO }}>{fragment.slug}</td>
      <td style={TABLE.td}>{fragment.display_name}</td>
      <td style={{ ...TABLE.td, ...MONO }}>{fragment.provides.join(", ")}</td>
      <td style={{ ...TABLE.td, ...MONO, color: "var(--text-muted)" }}>
        {fragment.requires.join(", ") || "—"}
      </td>
      <td style={TABLE.td}>
        <Pill tone={fragment.status === "verified" ? "ok" : "muted"}>
          {fragment.status}
        </Pill>
      </td>
      <td style={TABLE.td}>
        {fragment.status === "verified"
          ? <span style={{ ...MONO, color: "var(--text-muted)" }}>
              {fragment.signed ? `key ${fragment.signing_key_id?.slice(0, 10)}…` : "unsigned"}
            </span>
          : <>
              <Button size="sm" variant="secondary" disabled={busy} onClick={async () => {
                setBusy(true); setError(null);
                try { await verifyFragment(fragment.slug); onChanged(); }
                catch (e) { setError(String(e)); }
                finally { setBusy(false); }
              }}>Verify</Button>
              {error && (
                <div style={{ color: "var(--status-failed)", fontSize: 11, marginTop: 4 }}>
                  {error}
                </div>
              )}
            </>}
      </td>
    </tr>
  );
}

export default function Intent() {
  const [tab, setTab] = useState<"plans" | "library">("plans");
  const [plans, setPlans] = useState<IntentPlan[]>([]);
  const [fragments, setFragments] = useState<IntentFragment[]>([]);
  const [vocab, setVocab] = useState<{ goals: string[]; facts: string[] } | null>(null);
  const [intent, setIntent] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [p, f] = await Promise.all([listIntentPlans(), listFragments()]);
      setPlans(p.plans); setFragments(f.fragments); setVocab(f.vocabulary);
    } catch (e) { setError(String(e)); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  return (
    <div style={{ display: "flex", flexDirection: "column", width: "100%", height: "100%",
                  overflow: "hidden", boxSizing: "border-box" }}>
      {/* No page title here: `AppLayout` already renders one from `nav-data`. */}
      <Tabs
        ariaLabel="Intent execution"
        tabs={[
          { key: "plans" as const, label: "Plans", badge: plans.length || undefined },
          { key: "library" as const, label: "Fragments", badge: fragments.length || undefined },
        ]}
        value={tab}
        onChange={(k) => setTab(k as "plans" | "library")}
      />

      <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-lg) var(--space-2xl)",
                    boxSizing: "border-box" }}>
        {error && <ErrorNote style={{ marginBottom: "var(--space-md)" }}>{error}</ErrorNote>}

        {loading ? (
          <div style={{ display: "flex", justifyContent: "center", padding: "var(--space-2xl)" }}>
            <Spinner size={24} />
          </div>
        ) : tab === "plans" ? (
          <div style={{ display: "grid", gap: "var(--space-md)" }}>
            <Card>
              <SectionTitle>State an outcome</SectionTitle>
              <div style={{ display: "flex", gap: "var(--space-sm)" }}>
                <TextInput value={intent} onChange={(e) => setIntent(e.target.value)}
                           placeholder="refund this customer and tell them once it is sent"
                           style={{ flex: 1 }} />
                <Button disabled={busy || intent.trim().length < 3} onClick={async () => {
                  setBusy(true); setError(null);
                  try { await createIntentPlan({ intent }); setIntent(""); await load(); }
                  catch (e) { setError(String(e)); }
                  finally { setBusy(false); }
                }}>Plan it</Button>
              </div>
              {vocab && (
                <p style={{ ...muted, margin: "10px 0 0", lineHeight: 1.55 }}>
                  {/* Why an intent could not be planned is usually here. */}
                  Outcomes the library can reach:{" "}
                  <span style={{ ...MONO, color: "var(--text-secondary)" }}>
                    {vocab.goals.join(", ") || "none — verify a fragment first"}
                  </span>
                </p>
              )}
            </Card>

            {plans.length === 0
              ? <EmptyState title="No plans yet"
                            description="State an outcome above and Velaris will compose one." />
              : plans.map((p) => <PlanCard key={p.id} plan={p} onChanged={load} />)}
          </div>
        ) : (
          <Card style={{ padding: 0, overflow: "hidden" }}>
            <div style={{ padding: "var(--space-lg) var(--space-lg) 0" }}>
              <SectionTitle right={<span style={muted}>
                {fragments.filter(f => f.status === "verified").length} of {fragments.length} verified
              </span>}>
                Fragment library
              </SectionTitle>
            </div>
            {fragments.length === 0
              ? <EmptyState title="No fragments"
                            description="Only verified fragments can be composed into a plan." />
              : <div style={{ overflowX: "auto", padding: "0 var(--space-lg) var(--space-lg)" }}>
                  <table style={TABLE.table}>
                    <thead>
                      <tr>
                        <th style={TABLE.th}>Slug</th>
                        <th style={TABLE.th}>Name</th>
                        <th style={TABLE.th}>Provides</th>
                        <th style={TABLE.th}>Requires</th>
                        <th style={TABLE.th}>Status</th>
                        <th style={TABLE.th} />
                      </tr>
                    </thead>
                    <tbody>
                      {fragments.map((f) => (
                        <FragmentRow key={f.slug} fragment={f} onChanged={load} />
                      ))}
                    </tbody>
                  </table>
                </div>}
          </Card>
        )}
      </div>
    </div>
  );
}
