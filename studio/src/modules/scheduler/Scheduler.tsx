/* ═══════════════════════════════════════════════════════════════════
   Scheduler — what runs while nobody is watching

   A schedule is the one thing on the platform that acts with no person behind
   it, so the screen is built around the three facts that make that safe:

   1. **Nothing fires until a second person approves it.** A draft has no next
      firing time at all, and the list shows the author and the approver side by
      side. Saving an approved schedule sends it back to draft — the banner in
      the editor says so before the first keystroke.
   2. **The next firing times are shown before anyone saves.** A cron line is
      easy to misread, and `0 0 * * *` versus `0 * * * *` is the difference
      between one email and twenty-four.
   3. **Every firing is on the record, including the ones that did nothing.** A
      missed window reads `skipped`, not silence — the platform was down, the run
      was not replayed, and that is a different answer from "it never ran".

   Copyright (c) 2024-2026 Velaris Contributors
   SPDX-License-Identifier: BSL-1.1
   ═══════════════════════════════════════════════════════════════════ */
import React, { useCallback, useEffect, useState } from "react";

import {
  approveSchedule,
  createSchedule,
  deleteSchedule,
  disableSchedule,
  getSchedule,
  listCaseTypes,
  listScheduleActions,
  listSchedulerTargets,
  listSchedules,
  previewSchedule,
  runScheduleNow,
  updateSchedule,
  type Schedule,
  type ScheduleRun,
} from "@shared/api/client";
import { Button, Card, EmptyState, Spinner } from "@shared/components";

const cell: React.CSSProperties = {
  padding: "var(--space-sm)", borderBottom: "1px solid var(--border)",
  textAlign: "left", verticalAlign: "top",
};
const label: React.CSSProperties = {
  display: "block", fontSize: "0.75rem", color: "var(--text-muted)",
  marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.04em",
};
const field: React.CSSProperties = {
  width: "100%", padding: "var(--space-sm)", boxSizing: "border-box",
  background: "var(--surface)", color: "var(--text-primary)",
  border: "1px solid var(--border)", borderRadius: "var(--radius-sm)",
  fontSize: "0.875rem", fontFamily: "inherit",
};
const mono: React.CSSProperties = { ...field, fontFamily: "var(--font-mono, monospace)" };

/** Starting points, so nobody writes an action config from a blank box. Each is
 *  the smallest thing the server will accept for that kind. */
type Targets = Awaited<ReturnType<typeof listSchedulerTargets>>
  & { caseTypes: { id: string; name: string }[] };

const EMPTY_TARGETS: Targets = {
  bots: [], connectors: [], emailAccounts: [], syncDestinations: [], caseTypes: [],
};

/** Which config key each action picks by name, and where the options come from.
 *  Without this the editor asked for a UUID in a JSON box, which is not a thing
 *  anybody knows off the top of their head — and getting it wrong produced a
 *  schedule that failed at 2am rather than at the keyboard. */
const PICKERS: Record<string, {
  key: string; label: string; from: keyof Targets; note?: string;
}> = {
  bot:       { key: "bot_id", label: "Which bot", from: "bots",
               note: "Only an approved bot can run — a draft one is refused at fire time." },
  case:      { key: "case_type_id", label: "Which case type", from: "caseTypes" },
  connector: { key: "connector_id", label: "Which connector", from: "connectors" },
  email:     { key: "account_id", label: "Send from", from: "emailAccounts",
               note: "Leave unset to use the default outbound account." },
};

const TEMPLATES: Record<string, Record<string, unknown>> = {
  bot: { bot_id: "", input: {} },
  email: { to: ["someone@example.com"], subject: "Nightly summary",
           body_text: "…", purpose: "case_processing" },
  case: { case_type_id: "", data: {}, priority: "normal" },
  connector: { connector_id: "", input: {} },
  case_bulk: { match: { status: "open", older_than_days: 7 },
               apply: { set_variables: { chase_sent: true } }, max_cases: 100 },
  report: { query: { metric: "snapshot" }, format: "csv",
            email_to: ["ops@example.com"] },
  sync: { all_destinations: true, graph: true },
};

const COMMON_CRONS = [
  ["0 2 * * *", "Every night at 02:00"],
  ["0 7 * * 1-5", "Weekday mornings at 07:00"],
  ["*/15 * * * *", "Every 15 minutes"],
  ["0 6 1 * *", "The 1st of the month, 06:00"],
  ["0 22 * * 5", "Friday nights at 22:00"],
];

type Draft = {
  id: string | null; name: string; description: string; cron: string;
  timezone: string; action_kind: string; action_config: string;
};

function toDraft(row: Schedule | null, kinds: string[]): Draft {
  return {
    id: row?.id ?? null,
    name: row?.name ?? "",
    description: row?.description ?? "",
    cron: row?.cron ?? "0 2 * * *",
    timezone: row?.timezone
      ?? Intl.DateTimeFormat().resolvedOptions().timeZone ?? "UTC",
    action_kind: row?.action_kind ?? kinds[0] ?? "email",
    action_config: JSON.stringify(
      row?.action_config ?? TEMPLATES[row?.action_kind ?? kinds[0] ?? "email"] ?? {},
      null, 2),
  };
}

export default function Scheduler() {
  const [rows, setRows] = useState<Schedule[] | null>(null);
  const [kinds, setKinds] = useState<{ kind: string; label: string }[]>([]);
  const [targets, setTargets] = useState<Targets>(EMPTY_TARGETS);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [preview, setPreview] = useState<string[]>([]);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [detail, setDetail] = useState<
    { schedule: Schedule; next: string[]; runs: ScheduleRun[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [s, a] = await Promise.all([listSchedules(), listScheduleActions()]);
      setRows(s); setKinds(a);
    } catch (e) { setError(String(e)); }
  }, []);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    void (async () => {
      const [t, types] = await Promise.all([
        listSchedulerTargets(),
        listCaseTypes().catch(() => ({ items: [] as { id: string; name: string }[] })),
      ]);
      const items = (types as { items?: { id: string; name: string }[] }).items ?? [];
      setTargets({ ...t, caseTypes: items.map(c => ({ id: c.id, name: c.name })) });
    })();
  }, []);

  /** Read/write one key of the action config without disturbing the rest of the
   *  JSON the author has typed. */
  const configValue = (key: string): string => {
    try { return String((JSON.parse(draft?.action_config || "{}") ?? {})[key] ?? ""); }
    catch { return ""; }
  };
  const setConfigValue = (key: string, value: string) => {
    if (!draft) return;
    let parsed: Record<string, unknown>;
    try { parsed = JSON.parse(draft.action_config || "{}"); }
    catch { return; }   // malformed JSON: leave the author's text alone
    parsed[key] = value;
    setDraft({ ...draft, action_config: JSON.stringify(parsed, null, 2) });
  };

  // The next firing times, refreshed as the cron line is typed. This is the
  // whole point of the preview: reading a cron expression wrong is the ordinary
  // failure here, and it is invisible until the wrong night.
  useEffect(() => {
    if (!draft?.cron) { setPreview([]); return; }
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      try {
        const p = await previewSchedule({
          cron: draft.cron, timezone: draft.timezone, count: 5 });
        if (!cancelled) { setPreview(p.next); setPreviewError(null); }
      } catch (e) {
        if (!cancelled) {
          setPreview([]);
          setPreviewError(e instanceof Error ? e.message : String(e));
        }
      }
    }, 350);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [draft?.cron, draft?.timezone]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  const save = async () => {
    if (!draft) return;
    let config: Record<string, unknown>;
    try {
      config = JSON.parse(draft.action_config || "{}");
    } catch {
      setError("The action settings must be valid JSON.");
      return;
    }
    await act(async () => {
      const body = {
        name: draft.name.trim(), description: draft.description,
        cron: draft.cron.trim(), timezone: draft.timezone,
        action_kind: draft.action_kind, action_config: config,
      };
      if (draft.id) await updateSchedule(draft.id, body);
      else await createSchedule(body);
      setDraft(null);
    });
  };

  const open = async (id: string) => {
    setError(null);
    try { setDetail(await getSchedule(id)); }
    catch (e) { setError(String(e)); }
  };

  return (
    <div style={{ padding: "var(--space-xl)", overflow: "auto", height: "100%",
                  boxSizing: "border-box" }}>
      {error && <p role="alert" style={{ color: "var(--danger)" }}>{error}</p>}

      {draft && (
        <Card style={{ marginBottom: "var(--space-xl)" }}>
          <h3 style={{ marginTop: 0 }}>
            {draft.id ? "Edit schedule" : "New schedule"}
          </h3>
          {draft.id && rows?.find(r => r.id === draft.id)?.status === "active" && (
            <p role="note" style={{
              fontSize: "0.8125rem", padding: "var(--space-sm)",
              border: "1px solid var(--warning, var(--border))",
              borderRadius: "var(--radius-sm)", marginTop: 0,
            }}>
              <strong>Saving returns this schedule to draft.</strong> It stops
              firing until somebody who is not you approves it again — a changed
              recipient list or cron line is exactly what a reviewer needs to see.
            </p>
          )}

          <div style={{ display: "flex", gap: "var(--space-md)",
                        marginBottom: "var(--space-md)" }}>
            <div style={{ flex: 2 }}>
              <label style={label} htmlFor="s-name">Name</label>
              <input id="s-name" style={field} value={draft.name}
                     onChange={e => setDraft({ ...draft, name: e.target.value })} />
            </div>
            <div style={{ flex: 2 }}>
              <label style={label} htmlFor="s-kind">Action</label>
              <select id="s-kind" style={field} value={draft.action_kind}
                      onChange={e => setDraft({
                        ...draft, action_kind: e.target.value,
                        action_config: JSON.stringify(
                          TEMPLATES[e.target.value] ?? {}, null, 2),
                      })}>
                {kinds.map(k => (
                  <option key={k.kind} value={k.kind}>{k.label}</option>
                ))}
              </select>
            </div>
          </div>

          <div style={{ marginBottom: "var(--space-md)" }}>
            <label style={label} htmlFor="s-desc">What it is for</label>
            <input id="s-desc" style={field} value={draft.description}
                   onChange={e => setDraft({ ...draft, description: e.target.value })} />
          </div>

          <div style={{ display: "flex", gap: "var(--space-md)",
                        marginBottom: "var(--space-md)" }}>
            <div style={{ flex: 1 }}>
              <label style={label} htmlFor="s-cron">When (cron)</label>
              <input id="s-cron" style={mono} value={draft.cron}
                     onChange={e => setDraft({ ...draft, cron: e.target.value })} />
              <div style={{ marginTop: 6, display: "flex", gap: 6, flexWrap: "wrap" }}>
                {COMMON_CRONS.map(([expr, human]) => (
                  <Button key={expr} size="sm" variant="ghost"
                          onClick={() => setDraft({ ...draft, cron: expr })}>
                    {human}
                  </Button>
                ))}
              </div>
            </div>
            <div style={{ flex: 1 }}>
              <label style={label} htmlFor="s-tz">Timezone</label>
              <input id="s-tz" style={field} value={draft.timezone}
                     onChange={e => setDraft({ ...draft, timezone: e.target.value })} />
              <small style={{ color: "var(--text-muted)" }}>
                The wall clock is held across daylight saving: 02:00 stays 02:00.
              </small>
            </div>
          </div>

          <div style={{ marginBottom: "var(--space-md)" }}>
            <span style={label}>Next firings</span>
            {previewError ? (
              <p role="alert" style={{ color: "var(--danger)", fontSize: "0.8125rem" }}>
                {previewError}
              </p>
            ) : preview.length === 0 ? (
              <Spinner size={14} />
            ) : (
              <ul style={{ margin: 0, paddingLeft: "1.2rem", fontSize: "0.8125rem" }}>
                {preview.map(t => (
                  <li key={t}>{new Date(t).toLocaleString()}</li>
                ))}
              </ul>
            )}
          </div>

          {PICKERS[draft.action_kind] && (() => {
            const picker = PICKERS[draft.action_kind];
            const options = targets[picker.from] as { id: string; name: string;
                                                     status?: string }[];
            const selected = configValue(picker.key);
            return (
              <div style={{ marginBottom: "var(--space-md)" }}>
                <label style={label} htmlFor="s-target">{picker.label}</label>
                <select id="s-target" style={field} value={selected}
                        onChange={e => setConfigValue(picker.key, e.target.value)}>
                  <option value="">— choose —</option>
                  {options.map(o => (
                    <option key={o.id} value={o.id}>
                      {o.name}{o.status && o.status !== "active" ? ` (${o.status})` : ""}
                    </option>
                  ))}
                </select>
                {options.length === 0 && (
                  <small style={{ color: "var(--text-muted)" }}>
                    Nothing to choose yet — register one first.
                  </small>
                )}
                {picker.note && (
                  <small style={{ display: "block", color: "var(--text-muted)" }}>
                    {picker.note}
                  </small>
                )}
              </div>
            );
          })()}

          {draft.action_kind === "sync" && targets.syncDestinations.length > 0 && (
            <div style={{ marginBottom: "var(--space-md)" }}>
              <span style={label}>Warehouse destinations</span>
              <small style={{ display: "block", color: "var(--text-muted)" }}>
                Names for the ids below — set <code>all_destinations</code> to
                sync every enabled one.
              </small>
              <ul style={{ margin: "4px 0 0", paddingLeft: "1.2rem",
                           fontSize: "0.8125rem", color: "var(--text-muted)" }}>
                {targets.syncDestinations.map(d => (
                  <li key={d.id}><code>{d.id}</code> — {d.name}</li>
                ))}
              </ul>
            </div>
          )}

          <div style={{ marginBottom: "var(--space-md)" }}>
            <label style={label} htmlFor="s-config">Action settings (JSON)</label>
            <textarea id="s-config" style={{ ...mono, minHeight: 160 }}
                      value={draft.action_config}
                      onChange={e => setDraft({
                        ...draft, action_config: e.target.value })} />
          </div>

          <div style={{ display: "flex", gap: "var(--space-sm)" }}>
            <Button disabled={busy || !draft.name.trim()} onClick={save}>
              {draft.id ? "Save (returns to draft)" : "Create draft"}
            </Button>
            <Button variant="secondary" onClick={() => setDraft(null)}>Cancel</Button>
          </div>
        </Card>
      )}

      <Card style={{ marginBottom: "var(--space-xl)" }}>
        <div style={{ display: "flex", alignItems: "center",
                      justifyContent: "space-between" }}>
          <h3 style={{ marginTop: 0 }}>Schedules</h3>
          <Button size="sm" disabled={busy}
                  onClick={() => setDraft(toDraft(null, kinds.map(k => k.kind)))}>
            New schedule
          </Button>
        </div>
        <p style={{ fontSize: "0.875rem", color: "var(--text-muted)" }}>
          A schedule runs with nobody watching, so it stays a draft until
          somebody other than its author approves it — and any edit sends it back
          to draft. Disabling clears its next firing outright.
        </p>

        {rows === null ? <Spinner /> : rows.length === 0 ? (
          <EmptyState title="Nothing scheduled"
                      description="Nightly reminders, a warehouse push, a bot that checks a supplier's portal before anyone is in." />
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse",
                          fontSize: "0.875rem" }}>
            <thead><tr>
              <th style={cell}>Name</th>
              <th style={cell}>When</th>
              <th style={cell}>Action</th>
              <th style={cell}>Next</th>
              <th style={cell}>Last</th>
              <th style={cell}>Status</th>
              <th style={cell} />
            </tr></thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.id}>
                  <td style={cell}>
                    <a href="#detail" onClick={e => { e.preventDefault(); void open(r.id); }}>
                      {r.name}
                    </a>
                    <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                      by {r.created_by}
                      {r.approved_by ? ` · approved by ${r.approved_by}` : ""}
                    </div>
                  </td>
                  <td style={cell}>
                    <code>{r.cron}</code>
                    <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                      {r.timezone}
                    </div>
                  </td>
                  <td style={cell}>{r.action_kind}</td>
                  <td style={cell}>
                    {r.next_run_at ? new Date(r.next_run_at).toLocaleString() : "—"}
                  </td>
                  <td style={cell}>
                    {r.last_run_at ? (
                      <>
                        {new Date(r.last_run_at).toLocaleString()}
                        <div style={{ fontSize: "0.75rem",
                                      color: r.last_status === "error"
                                        ? "var(--danger)" : "var(--text-muted)" }}>
                          {r.last_status}
                          {r.last_error ? ` — ${r.last_error.slice(0, 60)}` : ""}
                        </div>
                      </>
                    ) : "never"}
                  </td>
                  <td style={cell}>{r.status}</td>
                  <td style={{ ...cell, whiteSpace: "nowrap" }}>
                    {r.status === "draft" && (
                      <Button size="sm" disabled={busy}
                              onClick={() => act(() => approveSchedule(r.id))}>
                        Approve
                      </Button>
                    )}{" "}
                    {r.status === "active" && (
                      <Button size="sm" variant="secondary" disabled={busy}
                              onClick={() => act(async () => {
                                const out = await runScheduleNow(r.id);
                                if (out.status === "error") {
                                  setError(String(out.detail?.error ?? "The run failed."));
                                }
                              })}>
                        Run now
                      </Button>
                    )}{" "}
                    <Button size="sm" variant="secondary" disabled={busy}
                            onClick={() => setDraft(toDraft(r, kinds.map(k => k.kind)))}>
                      Edit
                    </Button>{" "}
                    {r.status !== "disabled" ? (
                      <Button size="sm" variant="danger" disabled={busy}
                              onClick={() => act(() => disableSchedule(r.id))}>
                        Disable
                      </Button>
                    ) : (
                      <Button size="sm" variant="danger" disabled={busy}
                              onClick={() => act(() => deleteSchedule(r.id))}>
                        Delete
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {detail && (
        <Card>
          <div style={{ display: "flex", alignItems: "center",
                        justifyContent: "space-between" }}>
            <h3 style={{ marginTop: 0 }}>{detail.schedule.name} — history</h3>
            <Button size="sm" variant="secondary" onClick={() => setDetail(null)}>
              Close
            </Button>
          </div>
          <p style={{ fontSize: "0.875rem", color: "var(--text-muted)" }}>
            Every firing is here, including the ones that did nothing —
            <strong> skipped</strong> means the platform was not running when it
            was due, and the run was deliberately not replayed.
          </p>
          {detail.runs.length === 0 ? (
            <EmptyState title="Not fired yet"
                        description={detail.next.length
                          ? `Next: ${new Date(detail.next[0]).toLocaleString()}`
                          : "No firing time — it is not approved."} />
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse",
                            fontSize: "0.875rem" }}>
              <thead><tr>
                <th style={cell}>When</th>
                <th style={cell}>Result</th>
                <th style={cell}>Took</th>
                <th style={cell}>Detail</th>
              </tr></thead>
              <tbody>
                {detail.runs.map(run => (
                  <tr key={run.id}>
                    <td style={cell}>
                      {run.fired_at ? new Date(run.fired_at).toLocaleString() : "—"}
                    </td>
                    <td style={{ ...cell,
                                 color: run.status === "error" ? "var(--danger)" : undefined }}>
                      {run.status}
                    </td>
                    <td style={cell}>
                      {run.duration_ms !== null ? `${run.duration_ms} ms` : "—"}
                    </td>
                    <td style={{ ...cell, maxWidth: "28rem", wordBreak: "break-word" }}>
                      <code style={{ fontSize: "0.75rem" }}>
                        {JSON.stringify(run.detail)}
                      </code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      )}
    </div>
  );
}
