/**
 * "Move to stage" for a selection of cases — the bulk bar's one two-step action.
 *
 * Every other bulk action changes a field. Entering a stage FIRES things per
 * case — its SLA, its on_enter_status, the workflow signals — and moving the
 * cases back undoes none of it. So:
 *
 * 1. **Preview first, and the server enforces it.** The confirm button sends the
 *    token the preview returned, for exactly the cases the preview found
 *    movable. Changing the stage or the selection throws the preview away.
 * 2. **What will fire is said before the button**, per case type, in the
 *    designer's own words (the SLA's name, the status the stage sets).
 * 3. **Refusals are shown by case and reason twice** — at preview, and again
 *    after the run, because every case is re-checked when it moves.
 */
import React, { useEffect, useMemo, useState } from "react";
import { Button } from "@shared/components";
import {
  bulkStageExecute,
  bulkStagePreview,
  getCaseType,
  type BulkStagePreview,
  type BulkStageResult,
} from "@shared/api/client";

interface Props {
  selectedIds: string[];
  /** Case types of the selected cases the screen can see; stages are offered from these. */
  caseTypeIds: string[];
  onDone: () => void;
}

interface StageOption {
  id: string;
  label: string;
}

const muted: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)", marginTop: "var(--space-sm)" };
const danger: React.CSSProperties = { fontSize: 12, color: "var(--danger)", marginTop: "var(--space-sm)" };

function Refusals({ rows }: { rows: { case_id: string; ok: boolean; error?: string }[] }) {
  const refused = rows.filter((r) => !r.ok);
  if (refused.length === 0) return null;
  return (
    <ul style={{ marginTop: 4, paddingLeft: 18, color: "var(--text-muted)", fontSize: 12 }}>
      {refused.map((r) => (
        <li key={r.case_id}>
          <code style={{ fontFamily: "var(--font-mono)" }}>{r.case_id.slice(0, 8)}</code>
          {" — "}{r.error}
        </li>
      ))}
    </ul>
  );
}

export default function BulkStageMove({ selectedIds, caseTypeIds, onDone }: Props) {
  const [stages, setStages] = useState<StageOption[]>([]);
  const [target, setTarget] = useState("");
  const [reason, setReason] = useState("");
  const [preview, setPreview] = useState<BulkStagePreview | null>(null);
  const [result, setResult] = useState<BulkStageResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const typeKey = [...new Set(caseTypeIds)].sort().join(",");
  useEffect(() => {
    let live = true;
    const ids = typeKey ? typeKey.split(",") : [];
    Promise.all(ids.map((id) => getCaseType(id).catch(() => null)))
      .then((types) => {
        if (!live) return;
        // One entry per stage id. A stage id that exists in only some of the
        // selected case types is still offered — the server refuses the cases
        // whose type lacks it, by name, rather than this screen guessing.
        const byId = new Map<string, StageOption>();
        for (const t of types) {
          const def = (t as any)?.definition_json;
          for (const st of def?.stages ?? []) {
            if (st?.id && !byId.has(st.id)) byId.set(st.id, { id: st.id, label: st.name || st.id });
          }
        }
        setStages([...byId.values()]);
      });
    return () => { live = false; };
  }, [typeKey]);

  // A preview confirms one selection and one stage. Either changing voids it.
  const selectionKey = useMemo(() => [...selectedIds].sort().join(","), [selectedIds]);
  useEffect(() => { setPreview(null); setResult(null); }, [selectionKey, target]);

  async function runPreview() {
    setBusy(true); setError(null); setResult(null);
    try {
      setPreview(await bulkStagePreview(selectedIds, target));
    } catch (e: any) {
      setError(e?.message || "Could not preview the move");
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    if (!preview?.confirmation_token) return;
    setBusy(true); setError(null);
    try {
      setResult(await bulkStageExecute(preview, reason));
      setPreview(null);
      onDone();
    } catch (e: any) {
      // Most likely an expired confirmation. Throw the preview away so the
      // only button left is the one that fixes it.
      setPreview(null);
      setError(e?.message || "The move could not be run");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ marginTop: "var(--space-sm)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: "var(--space-md)", flexWrap: "wrap" }}>
        <select aria-label="Target stage" value={target} onChange={(e) => setTarget(e.target.value)}
                style={{ padding: "4px 8px", fontSize: 13 }}>
          <option value="">Stage…</option>
          {stages.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
        </select>
        <input aria-label="Reason" value={reason} onChange={(e) => setReason(e.target.value)}
               placeholder="reason (recorded on each case)" maxLength={2000}
               style={{ padding: "4px 8px", fontSize: 13, minWidth: 0 }} />
        <Button size="sm" disabled={!target || busy} onClick={runPreview}>
          {busy && !preview ? "Checking…" : "Preview move"}
        </Button>
      </div>

      {error && <p style={danger}>{error}</p>}

      {preview && (
        <div style={{ marginTop: "var(--space-sm)", fontSize: 12 }}>
          <div>
            <strong>{preview.would_move}</strong> will move
            {preview.unchanged > 0 && <> · {preview.unchanged} already there</>}
            {preview.refused > 0 && (
              <> · <strong style={{ color: "var(--danger)" }}>{preview.refused}</strong> refused</>
            )}
          </div>
          {preview.effects.length > 0 && (
            <ul style={{ marginTop: 4, paddingLeft: 18 }}>
              {preview.effects.map((e) => (
                <li key={e.case_type_id}>
                  {e.cases} × {e.case_type_name || e.case_type_id} → {e.stage_name}
                  {e.starts_sla && <> · starts SLA <em>{e.starts_sla}</em></>}
                  {e.on_enter_status && <> · sets status <em>{e.on_enter_status}</em></>}
                  {e.signals_workflows && <> · notifies the case workflows</>}
                </li>
              ))}
            </ul>
          )}
          <Refusals rows={preview.results} />
          <p style={muted}>These effects run once per case and are not undone by moving the cases back.</p>
          {preview.confirmation_token ? (
            <Button size="sm" disabled={busy} onClick={confirm}>
              {busy ? "Moving…" : `Move ${preview.would_move} case${preview.would_move === 1 ? "" : "s"}`}
            </Button>
          ) : (
            <p style={muted}>Nothing to move.</p>
          )}
        </div>
      )}

      {result && (
        <div style={{ marginTop: "var(--space-sm)", fontSize: 12 }}>
          <div>
            <strong>{result.moved}</strong> moved
            {result.unchanged > 0 && <> · {result.unchanged} already there</>}
            {result.failed > 0 && (
              <> · <strong style={{ color: "var(--danger)" }}>{result.failed}</strong> refused</>
            )}
          </div>
          <Refusals rows={result.results} />
        </div>
      )}
    </div>
  );
}
