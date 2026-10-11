/**
 * Oct item 18a, phases 2–3 — one rollout's policy, outcomes, shadow and clean-up.
 *
 * Policy: shadow, targeting (pilot / exclude / field), a scheduled ramp, and a
 * guard that holds the ramp or rolls back when the new version does measurably
 * worse. Outcomes: the A/B comparison, with "not enough cases" and "no clear
 * difference" shown as such rather than as a finding. Shadow: what the new
 * version would have done on baseline cases — it changed nothing. Rebase: after
 * a rollback, move open cases back where no work would be lost (dry run first).
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useEffect, useState } from "react";

import {
  rebaseRollout, rolloutOutcomes, rolloutShadow, setRolloutPolicy,
  type ContentRollout, type RolloutOutcomes,
} from "@shared/api/client";
import { Button, ErrorNote, FIELD_STYLE, Pill } from "@shared/components";

const muted: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)" };
const row: React.CSSProperties = {
  display: "flex", gap: "var(--space-sm)", alignItems: "center", flexWrap: "wrap",
};
const label: React.CSSProperties = { ...muted, minWidth: 110 };
const METRIC_LABEL: Record<string, string> = {
  sla_breach_rate: "SLA breach rate", reopen_rate: "Reopen rate",
  resolved_rate: "Resolved rate", avg_resolution_hours: "Avg resolution (h)",
};
const VERDICT_TONE: Record<string, "ok" | "bad" | "muted" | "info"> = {
  better: "ok", worse: "bad", "no clear difference": "info", "not enough cases": "muted",
};

const lines = (s: string) => s.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);

function PolicyEditor({ r, onChanged }: { r: ContentRollout; onChanged: () => void }) {
  const p = r.policy;
  const [shadow, setShadow] = useState(!!p?.shadow);
  const [include, setInclude] = useState((p?.targeting?.include ?? []).join("\n"));
  const [exclude, setExclude] = useState((p?.targeting?.exclude ?? []).join("\n"));
  const [field, setField] = useState(p?.targeting?.field ?? "");
  const [values, setValues] = useState((p?.targeting?.values ?? []).join(", "));
  const [steps, setSteps] = useState((p?.ramp?.steps ?? []).join(", "));
  const [hours, setHours] = useState(String(p?.ramp?.every_hours ?? 24));
  const [metric, setMetric] = useState(p?.guard?.metric ?? "");
  const [maxDelta, setMaxDelta] = useState(String(p?.guard?.max_delta ?? 0.05));
  const [minCases, setMinCases] = useState(String(p?.guard?.min_cases ?? 30));
  const [action, setAction] = useState<"hold" | "rollback">(p?.guard?.action ?? "hold");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const save = async () => {
    setError(null); setSaved(false);
    const targeting = (include || exclude || field)
      ? { include: lines(include), exclude: lines(exclude),
          ...(field ? { field, values: lines(values) } : {}) }
      : null;
    const ramp = steps.trim()
      ? { steps: lines(steps).map(Number), every_hours: Number(hours) } : null;
    const guard = metric
      ? { metric, max_delta: Number(maxDelta), min_cases: Number(minCases), action } : null;
    try {
      await setRolloutPolicy(r.id, { shadow, targeting, ramp, guard });
      setSaved(true); onChanged();
    } catch (e) { setError((e as Error).message); }
  };

  return (
    <div style={{ marginTop: 8 }}>
      <label style={{ ...row, fontSize: 13 }}>
        <input type="checkbox" checked={shadow} onChange={(e) => setShadow(e.target.checked)} />
        Shadow: replay baseline cases through the new version's rules (no effect)
      </label>
      <div style={{ ...row, marginTop: 8, alignItems: "flex-start" }}>
        <span style={label}>Pilot (always new)</span>
        <textarea aria-label="Pilot subjects" rows={2} value={include}
                  onChange={(e) => setInclude(e.target.value)}
                  placeholder="customer ids or emails, one per line"
                  style={{ ...FIELD_STYLE, flex: 1, minWidth: 200 }} />
        <span style={label}>Exclude (never)</span>
        <textarea aria-label="Excluded subjects" rows={2} value={exclude}
                  onChange={(e) => setExclude(e.target.value)}
                  style={{ ...FIELD_STYLE, flex: 1, minWidth: 200 }} />
      </div>
      <div style={{ ...row, marginTop: 8 }}>
        <span style={label}>Only cases where</span>
        <input aria-label="Target field" value={field} placeholder="field, e.g. region"
               onChange={(e) => setField(e.target.value)} style={{ ...FIELD_STYLE, width: 160 }} />
        <span style={muted}>is one of</span>
        <input aria-label="Target values" value={values} placeholder="EU, UK"
               onChange={(e) => setValues(e.target.value)} style={{ ...FIELD_STYLE, width: 200 }} />
      </div>
      {r.strategy === "percentage" && (
        <div style={{ ...row, marginTop: 8 }}>
          <span style={label}>Ramp steps (%)</span>
          <input aria-label="Ramp steps" value={steps} placeholder="10, 25, 50, 100"
                 onChange={(e) => setSteps(e.target.value)} style={{ ...FIELD_STYLE, width: 180 }} />
          <span style={muted}>every</span>
          <input aria-label="Ramp hours" type="number" value={hours}
                 onChange={(e) => setHours(e.target.value)} style={{ ...FIELD_STYLE, width: 80 }} />
          <span style={muted}>hours — never promotes by itself</span>
        </div>
      )}
      <div style={{ ...row, marginTop: 8 }}>
        <span style={label}>Guard</span>
        <select aria-label="Guard metric" value={metric} onChange={(e) => setMetric(e.target.value)}
                style={FIELD_STYLE}>
          <option value="">none</option>
          <option value="sla_breach_rate">SLA breach rate</option>
          <option value="reopen_rate">Reopen rate</option>
          <option value="avg_resolution_hours">Avg resolution hours</option>
        </select>
        {metric && (<>
          <span style={muted}>worse by more than</span>
          <input aria-label="Guard max delta" type="number" step="0.01" value={maxDelta}
                 onChange={(e) => setMaxDelta(e.target.value)} style={{ ...FIELD_STYLE, width: 80 }} />
          <span style={muted}>with at least</span>
          <input aria-label="Guard min cases" type="number" value={minCases}
                 onChange={(e) => setMinCases(e.target.value)} style={{ ...FIELD_STYLE, width: 80 }} />
          <span style={muted}>cases per arm →</span>
          <select aria-label="Guard action" value={action}
                  onChange={(e) => setAction(e.target.value as "hold" | "rollback")} style={FIELD_STYLE}>
            <option value="hold">hold the ramp</option>
            <option value="rollback">roll back</option>
          </select>
        </>)}
      </div>
      <div style={{ ...row, marginTop: 8 }}>
        <Button size="sm" onClick={save}>Save policy</Button>
        {p && <Pill tone={p.guard_state === "ok" ? "ok" : "bad"}>guard {p.guard_state}</Pill>}
        {p?.next_step_at && <span style={muted}>next ramp step {p.next_step_at.slice(0, 16)}</span>}
        {saved && <span style={muted}>saved</span>}
      </div>
      {error && <ErrorNote style={{ marginTop: 8 }}>{error}</ErrorNote>}
    </div>
  );
}

function Outcomes({ id }: { id: string }) {
  const [data, setData] = useState<RolloutOutcomes | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    rolloutOutcomes(id).then(setData).catch((e) => setError((e as Error).message));
  }, [id]);
  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!data) return <div style={muted}>Loading outcomes…</div>;
  const a = data.arms;
  return (
    <div style={{ marginTop: 8 }}>
      <div style={muted}>
        baseline {a.baseline.cases} cases ({a.baseline.resolved} resolved) · new version{" "}
        {a.candidate.cases} cases ({a.candidate.resolved} resolved)
      </div>
      <table style={{ fontSize: 12, marginTop: 6, borderCollapse: "collapse" }}>
        <thead><tr style={{ textAlign: "left" }}>
          <th style={{ paddingRight: 16 }}>Metric</th><th style={{ paddingRight: 16 }}>Baseline</th>
          <th style={{ paddingRight: 16 }}>New</th><th style={{ paddingRight: 16 }}>p</th><th>Verdict</th>
        </tr></thead>
        <tbody>
          {Object.entries(data.metrics).map(([k, m]) => (
            <tr key={k}>
              <td style={{ paddingRight: 16 }}>{METRIC_LABEL[k] ?? k}</td>
              <td style={{ paddingRight: 16 }}>{m.baseline ?? "—"}</td>
              <td style={{ paddingRight: 16 }}>{m.candidate ?? "—"}</td>
              <td style={{ paddingRight: 16 }}>{m.p_value ?? "—"}</td>
              <td><Pill tone={VERDICT_TONE[m.verdict] ?? "muted"}>{m.verdict}</Pill></td>
            </tr>
          ))}
        </tbody>
      </table>
      <div style={{ ...muted, marginTop: 6 }}>{data.note}</div>
    </div>
  );
}

function Shadow({ id }: { id: string }) {
  const [data, setData] = useState<Awaited<ReturnType<typeof rolloutShadow>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    rolloutShadow(id).then(setData).catch((e) => setError((e as Error).message));
  }, [id]);
  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!data) return <div style={muted}>Loading shadow results…</div>;
  return (
    <div style={{ marginTop: 8, ...muted }}>
      {data.evaluated} baseline case(s) replayed through the new version; {data.diverged} would
      have gone differently. It changed nothing.
      {data.examples.length > 0 && (
        <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
          {data.examples.slice(0, 5).map((e) => (
            <li key={e.case_id}>
              {e.case_id.slice(0, 8)} — {e.determinacy}
              {Array.isArray((e.detail as any)?.stages_missing_in_candidate) &&
                <> · stages missing in the new version:{" "}
                  {((e.detail as any).stages_missing_in_candidate as string[]).join(", ")}</>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Rebase({ id, onChanged }: { id: string; onChanged: () => void }) {
  const [reason, setReason] = useState("");
  const [plan, setPlan] = useState<Awaited<ReturnType<typeof rebaseRollout>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const run = async (dry: boolean) => {
    setError(null);
    try { const p = await rebaseRollout(id, reason, dry); setPlan(p); if (!dry) onChanged(); }
    catch (e) { setError((e as Error).message); }
  };
  return (
    <div style={{ marginTop: 8 }}>
      <div style={row}>
        <input aria-label="Rebase reason" value={reason} onChange={(e) => setReason(e.target.value)}
               placeholder="why these cases go back" style={{ ...FIELD_STYLE, flex: 1, minWidth: 220 }} />
        <Button size="sm" variant="secondary" disabled={reason.trim().length < 5}
                onClick={() => run(true)}>Preview</Button>
        {plan?.dry_run && plan.movable.length > 0 && (
          <Button size="sm" onClick={() => run(false)}>Move {plan.movable.length} case(s) back</Button>
        )}
      </div>
      {plan && (
        <div style={{ ...muted, marginTop: 6 }}>
          {plan.dry_run ? "Would move" : "Moved"} {plan.movable.length}; left alone{" "}
          {plan.blocked.length}{plan.blocked[0] && <> (e.g. {plan.blocked[0].problems[0]})</>}.{" "}
          {plan.note}
        </div>
      )}
      {error && <ErrorNote style={{ marginTop: 8 }}>{error}</ErrorNote>}
    </div>
  );
}

export default function RolloutDetails({ r, onChanged }: {
  r: ContentRollout; onChanged: () => void;
}) {
  const [tab, setTab] = useState<"policy" | "outcomes" | "shadow" | "rebase">(
    r.open ? "policy" : "outcomes");
  const tabs: { key: typeof tab; label: string; show: boolean }[] = [
    { key: "policy", label: "Policy", show: r.open },
    { key: "outcomes", label: "Outcomes (A/B)", show: true },
    { key: "shadow", label: "Shadow", show: !!r.policy?.shadow || !r.open },
    { key: "rebase", label: "Move cases back", show: r.state === "rolled_back" || r.state === "superseded" },
  ];
  return (
    <div style={{ marginTop: 10, paddingTop: 8, borderTop: "1px solid var(--border-subtle)" }}>
      <div style={row}>
        {tabs.filter((t) => t.show).map((t) => (
          <Button key={t.key} size="sm" variant={tab === t.key ? "secondary" : "ghost"}
                  onClick={() => setTab(t.key)}>{t.label}</Button>
        ))}
      </div>
      {tab === "policy" && r.open && <PolicyEditor r={r} onChanged={onChanged} />}
      {tab === "outcomes" && <Outcomes id={r.id} />}
      {tab === "shadow" && <Shadow id={r.id} />}
      {tab === "rebase" && <Rebase id={r.id} onChanged={onChanged} />}
    </div>
  );
}
