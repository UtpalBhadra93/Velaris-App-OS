/**
 * Agent Workforce (F-05) — the screen for staffing decisions about non-humans.
 *
 * Four things are invisible without it, and each is a tab or a banner:
 *
 *   1. **Being in the registry is not being on the roster.** An agent exists as
 *      a principal long before anybody decides it may hold case work. The Enrol
 *      panel lists the registry agents nobody has staffed, so that decision is
 *      made on purpose rather than by an agent quietly appearing in a queue.
 *   2. **Why an agent stopped receiving work.** The server returns the reasons,
 *      not a flag, and the roster prints all of them: "at capacity" and "over
 *      budget" are different problems with different fixes, and the third —
 *      "benched because its escalation queue is empty" — is a staffing failure
 *      that looks like nothing at all from a case's point of view.
 *   3. **A proposal changes nothing until a person approves it.** A routing
 *      weight the system proposed for itself sits in Approvals until somebody
 *      answers, because a system tuning its own routing from its own numbers is
 *      a feedback loop nobody is steering.
 *   4. **Whether reviews are actually being computed.** The nightly sweep is off
 *      until an operator switches it on, and a scorecard page that silently
 *      stopped updating reads exactly like an agent nobody has reviewed.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useState } from "react";

import {
  benchWorkforceAgent,
  buildWorkforceScorecard,
  decideWorkforceWeight,
  enrolWorkforceAgent,
  getWorkforce,
  listAgents,
  listWorkforceScorecards,
  proposeWorkforceWeight,
  type AgentScorecard,
  type WorkforceRoster,
  type WorkforceRow,
} from "@shared/api/client";
import {
  Banner, Button, Card, EmptyState, ErrorNote, MONO, Pill, Select, Spinner,
  TABLE, Tabs, TextInput,
} from "@shared/components";

type TabKey = "roster" | "scorecards" | "approvals";

type RegistryAgent = {
  id: string; slug: string; kind: string; display_name: string;
  model_binding: string | null; status: string;
};

/** A rate that is genuinely unknown, printed as unknown. */
function Rate({ value }: { value: number | null }) {
  if (value === null) {
    // NOT "0%". Zero over zero reviews reads as "never wrong" and means
    // "nobody looked" — the server sends NULL for exactly this reason and the
    // screen must not undo it.
    return <span style={{ color: "var(--text-muted)" }}>not reviewed</span>;
  }
  return <span>{(value * 100).toFixed(1)}%</span>;
}

/** A money figure that does not invent a claim it cannot support.
 *
 *  The server sends a NULL currency in two different situations — a window that
 *  mixed currencies, and a window in which nothing was ever priced — and they
 *  read completely differently to an operator. Printing "(mixed)" over a zero
 *  total, as the first version of this screen did, told somebody their costs
 *  were in conflicting currencies when in truth no decision had been priced at
 *  all. A total above zero with no single currency is the genuinely mixed one.
 */
function Money({ total, currency }: { total: number; currency: string | null }) {
  if (currency) return <>{total} {currency}</>;
  if (total === 0) return <>0</>;
  return (
    <>
      {total}{" "}
      <span title="This window mixes currencies, so one total would be an arithmetic error."
            style={{ color: "var(--text-muted)" }}>
        (mixed)
      </span>
    </>
  );
}

function agentLabel(row: WorkforceRow): string {
  return row.agent?.display_name?.trim() || row.agent?.slug || row.agent_id;
}

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

export default function Workforce() {
  const [tab, setTab] = useState<TabKey>("roster");
  const [data, setData] = useState<WorkforceRoster | null>(null);
  const [registry, setRegistry] = useState<RegistryAgent[]>([]);
  const [cards, setCards] = useState<AgentScorecard[]>([]);
  const [pending, setPending] = useState<AgentScorecard[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Enrolment form
  const [enrolId, setEnrolId] = useState("");
  const [queue, setQueue] = useState("");
  const [skills, setSkills] = useState("");
  const [maxConcurrent, setMaxConcurrent] = useState("");
  const [budget, setBudget] = useState("");

  const [windowDays, setWindowDays] = useState(7);
  const [weights, setWeights] = useState<Record<string, string>>({});
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [benchWhy, setBenchWhy] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const roster = await getWorkforce();
      setData(roster);
      const [all, open] = await Promise.all([
        listWorkforceScorecards({ limit: 100 }),
        listWorkforceScorecards({ status: "pending", limit: 100 }),
      ]);
      setCards(all.scorecards);
      setPending(open.scorecards);
      try {
        // The registry read is a SEPARATE gate (`agents.registry.read`), so a
        // supervisor who may staff agents but not read the registry still gets
        // a working roster — they simply cannot enrol from this screen.
        const reg = await listAgents();
        setRegistry(reg.agents);
      } catch {
        setRegistry([]);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function act<T>(fn: () => Promise<T>, message?: string) {
    setBusy(true); setError(null); setNotice(null);
    try {
      await fn();
      if (message) setNotice(message);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const roster = data?.roster ?? [];
  const rostered = new Set(roster.map(r => r.agent_id));
  const unstaffed = registry.filter(a => !rostered.has(a.id) && a.status === "active");
  const nameFor = (agentId: string) =>
    roster.find(r => r.agent_id === agentId)?.agent?.slug
    ?? registry.find(a => a.id === agentId)?.slug
    ?? agentId;

  const TABS = [
    { key: "roster"     as const, label: "Roster", badge: roster.length || undefined },
    { key: "scorecards" as const, label: "Scorecards" },
    { key: "approvals"  as const, label: "Approvals", badge: pending.length || undefined },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* No PageHeader: AppLayout renders one from nav-data. */}

      {data && !data.scorecard_cron.enabled && (
        <Banner tone="muted" title="Nightly reviews are off.">
          Scorecards exist only when somebody asks for one on this screen. Set{" "}
          <span style={MONO}>VELARIS_CASE_AGENT_SCORECARD_CRON_ENABLED=true</span>
          {" "}to compute them every {data.scorecard_cron.interval_hours}h over a{" "}
          {data.scorecard_cron.window_days}-day window.
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
            {tab === "roster" && (
              <>
                <Card style={{ marginBottom: 16 }}>
                  <h3 style={{ margin: "0 0 12px", fontSize: 14 }}>Roster</h3>
                  {roster.length === 0 ? (
                    <EmptyState
                      title="No agent holds case work"
                      description="An agent in the registry is not a worker. Enrol one below to let it be assigned case steps — HxNexus's case-Q&A agent is deliberately not one of them." />
                  ) : (
                    <table style={TABLE.table}>
                      <thead>
                        <tr>
                          <th style={TABLE.th}>Agent</th>
                          <th style={TABLE.th}>Skills</th>
                          <th style={TABLE.th}>Load</th>
                          <th style={TABLE.th}>Tokens / h</th>
                          <th style={TABLE.th}>Weight</th>
                          <th style={TABLE.th}>Escalates to</th>
                          <th style={TABLE.th}>State</th>
                          <th style={TABLE.th} />
                        </tr>
                      </thead>
                      <tbody>
                        {roster.map(r => {
                          const av = r.availability;
                          return (
                            <tr key={r.agent_id}
                                style={TABLE.tr}>
                              <td style={TABLE.td}>
                                <div>{agentLabel(r)}</div>
                                <div style={{ ...MONO, color: "var(--text-muted)" }}>
                                  {r.agent?.model_binding ?? "no declared model"}
                                </div>
                              </td>
                              <td style={{ ...TABLE.td, ...MONO }}>
                                {r.skills.length ? r.skills.join(", ") : "any"}
                              </td>
                              <td style={TABLE.td}>
                                {av ? `${av.active_load}${r.max_concurrent ? ` / ${r.max_concurrent}` : ""}` : "—"}
                                {!r.max_concurrent && (
                                  <span style={{ color: "var(--text-muted)" }}> (no cap)</span>
                                )}
                              </td>
                              <td style={TABLE.td}>
                                {r.token_budget_per_hour === null ? (
                                  <span style={{ color: "var(--text-muted)" }}>no budget</span>
                                ) : `${av?.tokens_last_hour ?? 0} / ${r.token_budget_per_hour}`}
                              </td>
                              <td style={TABLE.td}>{r.routing_weight}</td>
                              <td style={{ ...TABLE.td, ...MONO }}>{r.escalation_queue}</td>
                              <td style={TABLE.td}>
                                {av?.available
                                  ? <Pill tone="ok">taking work</Pill>
                                  : <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                                      <Pill tone={r.benched ? "bad" : "warn"}>
                                        {r.benched ? "benched" : "unavailable"}
                                      </Pill>
                                      {/* Every reason, not the first one. Two
                                          problems fixed one at a time is two
                                          trips back to this screen. */}
                                      {(av?.reasons ?? []).map(x => (
                                        <span key={x} style={{ fontSize: 11,
                                                               color: "var(--text-muted)" }}>
                                          {x}
                                        </span>
                                      ))}
                                    </div>}
                              </td>
                              <td style={{ ...TABLE.td, whiteSpace: "nowrap" }}>
                                {/* The reason is an inline field, not a
                                    `window.prompt`: it is required by the
                                    server, and a native dialog cannot say why
                                    while you are typing into it — "it keeps the
                                    work it already holds" is the fact somebody
                                    benching an agent most needs to know. */}
                                {!r.benched && (
                                  <TextInput placeholder="why bench it?"
                                         title="Benching stops NEW assignments only. The agent keeps the work it already holds."
                                         style={{ width: 150, marginRight: 6, padding: "5px 10px" }}
                                         value={benchWhy[r.agent_id] ?? ""}
                                         onChange={e => setBenchWhy(
                                           { ...benchWhy, [r.agent_id]: e.target.value })} />
                                )}
                                <Button size="sm" variant={r.benched ? "secondary" : "danger"}
                                        disabled={busy || (!r.benched && !benchWhy[r.agent_id]?.trim())}
                                        onClick={() => act(
                                          () => benchWorkforceAgent(
                                            r.agent_id, !r.benched,
                                            benchWhy[r.agent_id] ?? ""),
                                          r.benched
                                            ? `${agentLabel(r)} is back on the roster.`
                                            : `${agentLabel(r)} will take no new work. It keeps what it already holds.`)}>
                                  {r.benched ? "Unbench" : "Bench"}
                                </Button>{" "}
                                <Button size="sm" variant="ghost" disabled={busy}
                                        onClick={() => act(
                                          () => buildWorkforceScorecard(r.agent_id, windowDays),
                                          `Scorecard computed for ${agentLabel(r)}.`)}>
                                  Review
                                </Button>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  )}
                </Card>

                <Card>
                  <h3 style={{ margin: "0 0 4px", fontSize: 14 }}>Enrol an agent</h3>
                  <p style={{ margin: "0 0 12px", fontSize: 12,
                              color: "var(--text-muted)" }}>
                    Staffing decision, not a registry edit. The escalation queue is
                    required: an agent that cannot hand work to a person is an agent
                    whose failures have nowhere to go.
                  </p>
                  {registry.length === 0 ? (
                    <EmptyState title="No registry agents to show"
                      description="Either none exist yet, or this account cannot read the agent registry. Create agents under Agent identity first." />
                  ) : (
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 8,
                                  alignItems: "flex-end" }}>
                      <label style={{ fontSize: 12 }}>
                        <div style={{ color: "var(--text-muted)" }}>Agent</div>
                        <Select value={enrolId} onChange={e => setEnrolId(e.target.value)}
                                style={{ minWidth: 200, marginTop: 4 }}>
                          <option value="">Choose…</option>
                          {unstaffed.map(a => (
                            <option key={a.id} value={a.id}>
                              {a.display_name || a.slug} ({a.kind})
                            </option>
                          ))}
                          {roster.map(r => (
                            <option key={r.agent_id} value={r.agent_id}>
                              {agentLabel(r)} — already on the roster
                            </option>
                          ))}
                        </Select>
                      </label>
                      <label style={{ fontSize: 12 }}>
                        <div style={{ color: "var(--text-muted)" }}>Escalation queue</div>
                        <TextInput value={queue} onChange={e => setQueue(e.target.value)}
                                   placeholder="ops-queue" style={{ marginTop: 4 }} />
                      </label>
                      <label style={{ fontSize: 12 }}>
                        <div style={{ color: "var(--text-muted)" }}>Skills (comma separated)</div>
                        <TextInput value={skills} onChange={e => setSkills(e.target.value)}
                                   placeholder="claims, triage" style={{ marginTop: 4 }} />
                      </label>
                      <label style={{ fontSize: 12 }}>
                        <div style={{ color: "var(--text-muted)" }}>Max concurrent</div>
                        <TextInput value={maxConcurrent} type="number" min={1}
                                   onChange={e => setMaxConcurrent(e.target.value)}
                                   placeholder="no cap" style={{ width: 110, marginTop: 4 }} />
                      </label>
                      <label style={{ fontSize: 12 }}>
                        <div style={{ color: "var(--text-muted)" }}>Tokens per hour</div>
                        <TextInput value={budget} type="number" min={1}
                                   onChange={e => setBudget(e.target.value)}
                                   placeholder="no budget" style={{ width: 130, marginTop: 4 }} />
                      </label>
                      <Button disabled={busy || !enrolId || !queue.trim()}
                              onClick={() => act(async () => {
                                await enrolWorkforceAgent(enrolId, {
                                  escalation_queue: queue.trim(),
                                  skills: skills.split(",").map(s => s.trim()).filter(Boolean),
                                  // Empty means "no cap" — an ABSENT value, never
                                  // a large number, so it reads as a decision.
                                  max_concurrent: maxConcurrent ? Number(maxConcurrent) : null,
                                  token_budget_per_hour: budget ? Number(budget) : null,
                                });
                                setQueue(""); setSkills("");
                                setMaxConcurrent(""); setBudget("");
                              }, "Enrolled. The agent can now be assigned case steps whose rule allows one.")}>
                        Enrol
                      </Button>
                    </div>
                  )}
                </Card>
              </>
            )}

            {tab === "scorecards" && (
              <Card>
                <div style={{ display: "flex", justifyContent: "space-between",
                              alignItems: "center", marginBottom: 4 }}>
                  <h3 style={{ margin: 0, fontSize: 14 }}>Scorecards</h3>
                  <label style={{ fontSize: 12, color: "var(--text-muted)" }}>
                    Review window{" "}
                    <TextInput type="number" min={1} max={365} value={windowDays}
                               onChange={e => setWindowDays(Number(e.target.value) || 7)}
                               style={{ width: 72 }} /> days
                  </label>
                </div>
                <p style={{ margin: "0 0 12px", fontSize: 12,
                            color: "var(--text-muted)" }}>
                  Every number is counted from the AI decision ledger and the
                  recorded costs — nothing is measured twice, so these reconcile
                  with those reports by construction.
                </p>
                {cards.length === 0 ? (
                  <EmptyState title="No scorecards yet"
                    description="Use Review on the roster to compute one, or switch on the nightly sweep." />
                ) : (
                  <table style={TABLE.table}>
                    <thead>
                      <tr>
                        <th style={TABLE.th}>Agent</th>
                        <th style={TABLE.th}>Window</th>
                        <th style={TABLE.th}>Decisions</th>
                        <th style={TABLE.th}>Reviewed</th>
                        <th style={TABLE.th}>Override rate</th>
                        <th style={TABLE.th}>Cost</th>
                        <th style={TABLE.th}>Per decision</th>
                        <th style={TABLE.th}>Weight proposal</th>
                      </tr>
                    </thead>
                    <tbody>
                      {cards.map(c => (
                        <tr key={c.id}
                            style={TABLE.tr}>
                          <td style={{ ...TABLE.td, ...MONO }}>{nameFor(c.agent_id)}</td>
                          <td style={{ ...TABLE.td, fontSize: 12 }}>
                            {when(c.window.start)} → {when(c.window.end)}
                          </td>
                          <td style={TABLE.td}>{c.decisions}</td>
                          <td style={TABLE.td}>
                            {c.reviewed} ({c.accepted} kept, {c.overridden} overridden)
                          </td>
                          <td style={TABLE.td}><Rate value={c.override_rate} /></td>
                          <td style={TABLE.td}>
                            <Money total={c.cost_total} currency={c.currency} />
                          </td>
                          <td style={TABLE.td}>
                            {c.cost_per_decision === null ? "—" : c.cost_per_decision}
                          </td>
                          <td style={TABLE.td}>
                            {c.proposal_status ? (
                              <Pill tone={c.proposal_status === "approved" ? "ok"
                                        : c.proposal_status === "rejected" ? "muted" : "warn"}>
                                {c.proposal_status} {c.proposed_weight}
                              </Pill>
                            ) : (
                              <div style={{ display: "flex", gap: 4 }}>
                                <TextInput placeholder="weight" type="number" step="0.1"
                                       min={0} max={10} style={{ width: 82, padding: "5px 10px" }}
                                       value={weights[c.id] ?? ""}
                                       onChange={e => setWeights(
                                         { ...weights, [c.id]: e.target.value })} />
                                <TextInput placeholder="why" style={{ width: 140, padding: "5px 10px" }}
                                       value={reasons[c.id] ?? ""}
                                       onChange={e => setReasons(
                                         { ...reasons, [c.id]: e.target.value })} />
                                <Button size="sm" variant="secondary"
                                        disabled={busy || !weights[c.id] || !reasons[c.id]?.trim()}
                                        onClick={() => act(
                                          () => proposeWorkforceWeight(
                                            c.id, Number(weights[c.id]), reasons[c.id]),
                                          "Proposed. Nothing changes until a supervisor approves it on the Approvals tab.")}>
                                  Propose
                                </Button>
                              </div>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </Card>
            )}

            {tab === "approvals" && (
              <Card>
                <h3 style={{ margin: "0 0 4px", fontSize: 14 }}>
                  Routing weight changes awaiting a person
                </h3>
                <p style={{ margin: "0 0 12px", fontSize: 12,
                            color: "var(--text-muted)" }}>
                  A weight decides how much work an agent is offered. These
                  proposals change nothing until approved here — a system that
                  adjusted its own routing from its own performance data would be
                  a feedback loop nobody is steering.
                </p>
                {pending.length === 0 ? (
                  <EmptyState title="Nothing waiting"
                    description="Propose a weight change from a scorecard to raise one." />
                ) : pending.map(c => (
                  <div key={c.id} style={{ borderTop: "1px solid var(--border-subtle)",
                                           padding: "12px 0" }}>
                    <div style={{ display: "flex", gap: 8, alignItems: "center",
                                  flexWrap: "wrap" }}>
                      <strong style={MONO}>{nameFor(c.agent_id)}</strong>
                      <Pill tone="warn">weight → {c.proposed_weight}</Pill>
                      <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
                        proposed {when(c.created_at)}
                      </span>
                    </div>
                    <p style={{ margin: "6px 0", fontSize: 13 }}>{c.proposal_reason}</p>
                    {/* The evidence the proposal rests on, as it was when the
                        proposal was made — not what happens to be true now. */}
                    <p style={{ margin: "0 0 8px", fontSize: 12,
                                color: "var(--text-muted)" }}>
                      Evidence: {c.decisions} decisions, {c.reviewed} reviewed,
                      override rate <Rate value={c.override_rate} />, cost{" "}
                      <Money total={c.cost_total} currency={c.currency} /> over{" "}
                      {when(c.window.start)} → {when(c.window.end)}.
                    </p>
                    <Button size="sm" disabled={busy}
                            onClick={() => act(() => decideWorkforceWeight(c.id, true),
                                               "Approved — the roster now uses the new weight.")}>
                      Approve
                    </Button>{" "}
                    <Button size="sm" variant="secondary" disabled={busy}
                            onClick={() => act(() => decideWorkforceWeight(c.id, false),
                                               "Rejected. The weight is unchanged.")}>
                      Reject
                    </Button>
                  </div>
                ))}
              </Card>
            )}
          </>
        )}
      </div>
    </div>
  );
}
