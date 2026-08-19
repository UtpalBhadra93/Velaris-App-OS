// WASM rules migration report (#28 / report §7.4).
//
// `/rules/lint` answers "which rules need work". This answers the question that
// actually gates making WASM the default for new rules: **which case types can
// move**. The switch is per case type, and one unmigrated rule blocks the case
// type it belongs to.
//
// The scope arithmetic is the substance: a GLOBAL rule applies to every case
// type, so one bad global blocks all of them; a STAGE-scoped rule belongs to
// the case type owning that stage. A report counting only case-type-scoped
// rules would read as ready when it is not.
import React, { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@shared/api/client";

interface Blocking {
  rule_id: string;
  name: string;
  scope: string;
  classification: string;
  reason: string;
}

interface CaseTypeRow {
  case_type_id: string;
  case_type_name: string;
  version: string;
  summary: Record<string, number>;
  ready_for_wasm_default: boolean;
  blocking_rules: Blocking[];
}

interface Report {
  case_types: CaseTypeRow[];
  ready_count: number;
  total_case_types: number;
  global_rules: { summary: Record<string, number>; blocking_rules: Blocking[] };
  unattributed_rules: { summary: Record<string, number>; blocking_rules: Blocking[]; note: string };
}

export default function RuleMigrationReport() {
  const [data, setData] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<Record<string, boolean>>({});

  const load = useCallback(async () => {
    try {
      const r = await apiFetch("/rules/lint/report");
      if (!r.ok) throw new Error(await r.text());
      setData(await r.json());
      setError(null);
    } catch (e: any) {
      setError(e?.message ?? "Could not load the migration report");
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  if (error) {
    return <div style={{ fontSize: 11, color: "var(--status-failed)" }}>{error}</div>;
  }
  if (!data) return <div style={{ fontSize: 12, color: "var(--text-muted)" }}>Loading…</div>;

  return (
    <div>
      <h4 style={{ margin: "0 0 4px", fontSize: 13 }}>WASM Migration Readiness</h4>
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 12 }}>
        {data.ready_count} of {data.total_case_types} case type(s) could move to WASM
        rules today. A case type is blocked by its own unmigrated rules and by any
        non-conforming <strong>global</strong> rule, which applies everywhere.
      </div>

      {data.global_rules.blocking_rules.length > 0 && (
        <div style={{ padding: 10, marginBottom: 12, borderRadius: 4, fontSize: 11,
                      background: "var(--bg-input)", color: "var(--status-failed)" }}>
          ⚠ {data.global_rules.blocking_rules.length} global rule(s) block <em>every</em> case type:{" "}
          {data.global_rules.blocking_rules.map(b => b.name).join(", ")}
        </div>
      )}

      {data.case_types.map(ct => (
        <div key={ct.case_type_id} style={{ borderBottom: "1px solid var(--border-subtle)", padding: "8px 0" }}>
          <div
            onClick={() => setOpen({ ...open, [ct.case_type_id]: !open[ct.case_type_id] })}
            style={{ display: "flex", gap: 8, alignItems: "center", cursor: ct.blocking_rules.length ? "pointer" : "default" }}
          >
            <span style={{ color: ct.ready_for_wasm_default ? "var(--accent)" : "var(--status-warning, #d98c00)" }}>
              {ct.ready_for_wasm_default ? "✓" : "•"}
            </span>
            <span style={{ fontSize: 12, flex: 1 }}>
              {ct.case_type_name} <span style={{ color: "var(--text-muted)" }}>v{ct.version}</span>
            </span>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
              {ct.blocking_rules.length
                ? `${ct.blocking_rules.length} blocking`
                : "ready"}
            </span>
          </div>

          {open[ct.case_type_id] && ct.blocking_rules.map(b => (
            <div key={b.rule_id + b.name} style={{ fontSize: 11, padding: "4px 0 4px 20px", color: "var(--text-muted)" }}>
              <strong>{b.name}</strong> ({b.scope}) — {b.classification}: {b.reason}
            </div>
          ))}
        </div>
      ))}

      {data.unattributed_rules.blocking_rules.length > 0 && (
        <div style={{ marginTop: 12, fontSize: 11, color: "var(--text-muted)" }}>
          <strong>{data.unattributed_rules.blocking_rules.length} unattributed rule(s)</strong> —
          {" "}{data.unattributed_rules.note} They are surfaced rather than dropped so a rule
          pointing at a deleted case type is not silently forgotten.
        </div>
      )}
    </div>
  );
}
