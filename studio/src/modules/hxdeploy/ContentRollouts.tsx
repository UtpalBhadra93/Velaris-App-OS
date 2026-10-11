/**
 * Oct item 18a — staged rollouts of case type versions, on THIS environment.
 *
 * A rollout chooses which version of a case type a NEW case gets: at once, behind
 * a switch, to a sticky percentage of customers, or blue-green. Cases already
 * created keep their version whatever happens here.
 *
 * Requested rollouts do nothing until somebody other than the requester approves
 * them (a request that arrived with a promoted package lands here too). One open
 * rollout per case type: the server's refusal names the open one, and this
 * screen offers the three ways forward — promote, roll back, supersede.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";

import {
  approveRollout, closeRollout, getRollout, listRollouts, requestRollout, rolloutVersions,
  setRolloutPercent, supersedeRollout, type ContentRollout, type ContentRolloutEvent,
  type RolloutStrategy,
} from "@shared/api/client";
import RolloutDetails from "./RolloutDetails";
import {
  Button, Card, EmptyState, ErrorNote, FIELD_STYLE, Pill, SectionTitle,
} from "@shared/components";
import { getAccessToken } from "@/auth/tokenManager";

const STRATEGIES: { key: RolloutStrategy; label: string; hint: string }[] = [
  { key: "immediate", label: "Immediate", hint: "Every new case gets it as soon as it is approved." },
  { key: "flag", label: "Feature flag", hint: "Installed and dormant until switched on." },
  { key: "percentage", label: "Percentage", hint: "A sticky share of customers, raised step by step." },
  { key: "blue_green", label: "Blue-green", hint: "One switch moves everyone between the two." },
];
const STATE_TONE: Record<string, "ok" | "warn" | "info" | "muted" | "bad"> = {
  requested: "warn", dormant: "muted", rolling: "info", promoted: "ok",
  rolled_back: "bad", superseded: "muted",
};
const muted: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)" };
const row: React.CSSProperties = {
  display: "flex", gap: "var(--space-sm)", alignItems: "center", flexWrap: "wrap",
};

interface CaseTypeLite { id: string; name: string; version: string }

async function loadCaseTypes(): Promise<CaseTypeLite[]> {
  const t = getAccessToken();
  let d: any;
  try {
    const r = await fetch("/api/v1/case-types?page_size=200",
                          { headers: t ? { Authorization: `Bearer ${t}` } : {} });
    if (!r.ok) return [];
    d = await r.json();
  } catch { return []; }
  const items = Array.isArray(d) ? d : (d.items ?? []);
  return items.map((c: any) => ({ id: String(c.id), name: String(c.name), version: String(c.version) }));
}

function RequestForm({ caseTypes, onDone }: { caseTypes: CaseTypeLite[]; onDone: () => void }) {
  const names = useMemo(() => [...new Set(caseTypes.map((c) => c.name))].sort(), [caseTypes]);
  const [name, setName] = useState("");
  const [baseline, setBaseline] = useState("");
  const [candidate, setCandidate] = useState("");
  const [strategy, setStrategy] = useState<RolloutStrategy>("percentage");
  const [percent, setPercent] = useState(10);
  const [error, setError] = useState<string | null>(null);
  const versions = caseTypes.filter((c) => c.name === name);
  return (
    <div style={{ marginTop: 8 }}>
      <div style={row}>
        <select aria-label="Case type" value={name} style={FIELD_STYLE}
                onChange={(e) => { setName(e.target.value); setBaseline(""); setCandidate(""); }}>
          <option value="">Case type…</option>
          {names.map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
        <select aria-label="Baseline version" value={baseline} style={FIELD_STYLE}
                onChange={(e) => setBaseline(e.target.value)}>
          <option value="">Baseline (today)…</option>
          {versions.map((v) => <option key={v.id} value={v.id}>{v.version}</option>)}
        </select>
        <select aria-label="Candidate version" value={candidate} style={FIELD_STYLE}
                onChange={(e) => setCandidate(e.target.value)}>
          <option value="">New version…</option>
          {versions.filter((v) => v.id !== baseline).map((v) =>
            <option key={v.id} value={v.id}>{v.version}</option>)}
        </select>
        <select aria-label="Strategy" value={strategy} style={FIELD_STYLE}
                onChange={(e) => setStrategy(e.target.value as RolloutStrategy)}>
          {STRATEGIES.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
        </select>
        {strategy === "percentage" && (
          <input aria-label="Percent" type="number" min={0} max={100} value={percent}
                 onChange={(e) => setPercent(Number(e.target.value))}
                 style={{ ...FIELD_STYLE, width: 80 }} />
        )}
        <Button size="sm" disabled={!baseline || !candidate} onClick={async () => {
          setError(null);
          try {
            await requestRollout({ baseline_id: baseline, candidate_id: candidate, strategy,
                                   percent: strategy === "percentage" ? percent : 0 });
            onDone();
          } catch (e) { setError((e as Error).message); }
        }}>Request</Button>
      </div>
      <p style={{ ...muted, margin: "6px 0 0" }}>
        {STRATEGIES.find((s) => s.key === strategy)?.hint} Nothing changes until somebody else approves it.
      </p>
      {error && <ErrorNote style={{ marginTop: 8 }}>{error}</ErrorNote>}
    </div>
  );
}

function RolloutCard({ r, caseTypes, onChanged }: {
  r: ContentRollout; caseTypes: CaseTypeLite[]; onChanged: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [events, setEvents] = useState<ContentRolloutEvent[] | null>(null);
  const [pct, setPct] = useState(r.percent);
  const [superseding, setSuperseding] = useState(false);
  const [details, setDetails] = useState(false);
  const [newCandidate, setNewCandidate] = useState("");
  const act = async (fn: () => Promise<unknown>) => {
    setError(null);
    try { await fn(); onChanged(); }
    catch (e) {
      const msg = (e as Error).message;
      setError(/\b403\b|forbidden/i.test(msg) && !/somebody other/i.test(msg)
        ? "You do not hold the privilege for that." : msg);
    }
  };
  const others = caseTypes.filter((c) => c.name === r.artifact_name &&
                                         c.id !== r.baseline_id && c.id !== r.candidate_id);
  return (
    <Card style={{ marginBottom: "var(--space-sm)" }}>
      <div style={row}>
        <strong style={{ fontSize: 14 }}>{r.artifact_name}</strong>
        <span style={muted}>{r.baseline_version} → {r.candidate_version}</span>
        <Pill tone="muted">{STRATEGIES.find((s) => s.key === r.strategy)?.label ?? r.strategy}</Pill>
        <Pill tone={STATE_TONE[r.state] ?? "muted"}>{r.state.replace("_", " ")}</Pill>
        {r.open && r.state !== "requested" && <Pill tone="info">{r.percent}% new version</Pill>}
        {r.source === "hxdeploy" && <Pill tone="muted">from a promotion</Pill>}
        <span style={{ flex: 1 }} />
        <span style={muted}>cases: {r.arms.baseline} baseline · {r.arms.candidate} new</span>
      </div>
      <div style={{ ...muted, marginTop: 4 }}>
        requested by {r.requested_by ?? "—"}
        {r.approved_by && <> · approved by {r.approved_by}</>}
        {r.closed_by && <> · closed by {r.closed_by}</>}
      </div>

      <div style={{ ...row, marginTop: 8 }}>
        {r.state === "requested" && (
          <Button size="sm" onClick={() => act(() => approveRollout(r.id))}>Approve</Button>
        )}
        {r.open && r.state !== "requested" && r.strategy === "percentage" && (
          <>
            <input aria-label="New percent" type="number" min={0} max={100} value={pct}
                   onChange={(e) => setPct(Number(e.target.value))}
                   style={{ ...FIELD_STYLE, width: 80 }} />
            <Button size="sm" variant="secondary"
                    onClick={() => act(() => setRolloutPercent(r.id, pct))}>Set %</Button>
          </>
        )}
        {r.open && r.state !== "requested" && (r.strategy === "flag" || r.strategy === "blue_green") && (
          <Button size="sm" variant="secondary"
                  onClick={() => act(() => setRolloutPercent(r.id, r.percent ? 0 : 100))}>
            {r.strategy === "flag" ? (r.percent ? "Switch off" : "Switch on")
                                   : (r.percent ? "Switch to baseline" : "Switch to new version")}
          </Button>
        )}
        {r.open && r.state !== "requested" && (
          <Button size="sm" onClick={() => act(() => closeRollout(r.id, "promote"))}>Promote</Button>
        )}
        {r.open && (
          <Button size="sm" variant="ghost"
                  onClick={() => act(() => closeRollout(r.id, "rollback"))}>Roll back</Button>
        )}
        {r.open && others.length > 0 && (
          <Button size="sm" variant="ghost" onClick={() => setSuperseding((v) => !v)}>Supersede…</Button>
        )}
        <Button size="sm" variant="ghost" onClick={() => setDetails((v) => !v)}>
          {details ? "Hide details" : "Policy & outcomes"}
        </Button>
        <Button size="sm" variant="ghost" onClick={async () => {
          if (events) { setEvents(null); return; }
          try { setEvents((await getRollout(r.id)).events); } catch (e) { setError((e as Error).message); }
        }}>{events ? "Hide history" : "History"}</Button>
      </div>

      {superseding && (
        <div style={{ ...row, marginTop: 8 }}>
          <select aria-label="Superseding version" value={newCandidate} style={FIELD_STYLE}
                  onChange={(e) => setNewCandidate(e.target.value)}>
            <option value="">Replace the new version with…</option>
            {others.map((v) => <option key={v.id} value={v.id}>{v.version}</option>)}
          </select>
          <Button size="sm" disabled={!newCandidate} onClick={() => act(() => supersedeRollout(
            r.id, { candidate_id: newCandidate, strategy: r.strategy, percent: 0 }))}>
            Roll this back and request it
          </Button>
        </div>
      )}
      {events && (
        <ul style={{ ...muted, margin: "8px 0 0", paddingLeft: 18 }}>
          {events.map((e, i) => (
            <li key={i}>
              {e.at?.replace("T", " ").slice(0, 16)} — {e.action.replace("_", " ")}
              {e.to_percent != null && <> ({e.from_percent ?? 0}% → {e.to_percent}%)</>}
              {e.actor && <> by {e.actor}</>}{e.reason && <> — {e.reason}</>}
            </li>
          ))}
        </ul>
      )}
      {details && <RolloutDetails r={r} onChanged={onChanged} />}
      {error && <ErrorNote style={{ marginTop: 8 }}>{error}</ErrorNote>}
    </Card>
  );
}

function VersionStatus() {
  const [rows, setRows] = useState<Awaited<ReturnType<typeof rolloutVersions>>["versions"]>([]);
  useEffect(() => { rolloutVersions().then((d) => setRows(d.versions)).catch(() => setRows([])); }, []);
  if (rows.length === 0) return null;
  return (
    <Card style={{ marginTop: "var(--space-md)" }}>
      <SectionTitle>Versions a rollout touched</SectionTitle>
      <p style={{ ...muted, margin: "0 0 6px" }}>
        A retired or withdrawn version still exists; a request for it is sent where it says.
      </p>
      <table style={{ fontSize: 12, borderCollapse: "collapse" }}>
        <tbody>
          {rows.map((v) => (
            <tr key={v.case_type_id}>
              <td style={{ paddingRight: 16 }}>{v.name} {v.version}</td>
              <td style={{ paddingRight: 16 }}><Pill tone="muted">{v.status}</Pill></td>
              <td style={muted}>{v.new_cases_go_to && v.new_cases_go_to !== v.case_type_id
                ? `new cases go to ${v.new_cases_go_to.slice(0, 8)}` : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

export default function ContentRollouts() {
  const [rollouts, setRollouts] = useState<ContentRollout[]>([]);
  const [caseTypes, setCaseTypes] = useState<CaseTypeLite[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [showClosed, setShowClosed] = useState(false);

  const load = useCallback(async () => {
    try {
      setRollouts((await listRollouts(showClosed ? "all" : "open")).rollouts);
      setError(null);
    } catch (e) {
      const msg = (e as Error).message;
      setError(/\b403\b|forbidden/i.test(msg) ? "You do not hold the privilege to see rollouts." : msg);
    }
  }, [showClosed]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { void loadCaseTypes().then(setCaseTypes); }, []);

  return (
    <div style={{ maxWidth: 960 }}>
      <Card style={{ marginBottom: "var(--space-md)" }}>
        <SectionTitle right={(
          <div style={row}>
            <label style={muted}>
              <input type="checkbox" checked={showClosed}
                     onChange={(e) => setShowClosed(e.target.checked)} /> show closed
            </label>
            {!adding && <Button size="sm" onClick={() => setAdding(true)}>Request a rollout</Button>}
          </div>)}>
          Content rollouts
        </SectionTitle>
        <p style={{ ...muted, margin: 0 }}>
          Which version of a case type NEW cases get on this environment. Cases already created keep
          theirs. One open rollout per case type; each change is recorded. Rules and processes roll
          out inside a case type version, not on their own.
        </p>
        {adding && <RequestForm caseTypes={caseTypes}
                                onDone={() => { setAdding(false); void load(); }} />}
      </Card>
      {error && <ErrorNote style={{ marginBottom: 8 }}>{error}</ErrorNote>}
      {rollouts.length === 0 && !error && (
        <EmptyState title={showClosed ? "No rollouts" : "No open rollouts"}
                    description="Every case type gives new cases the version they are asked for." />
      )}
      {rollouts.map((r) => (
        <RolloutCard key={r.id} r={r} caseTypes={caseTypes} onChanged={() => void load()} />
      ))}
      <VersionStatus />
    </div>
  );
}
