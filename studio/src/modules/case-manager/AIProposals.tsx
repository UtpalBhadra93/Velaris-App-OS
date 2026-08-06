// AI Action proposals — the human half of the loop (#33 / report §5.1).
//
// An `ai_action` step calls the model, stores what it proposed, and stops.
// Nothing reaches case data until someone here accepts, edits or rejects it.
// Without this panel the model proposed into a table nobody could see, which
// made the entire human-in-the-loop guarantee unusable in practice.
//
// Override keeps BOTH what the model said and what the human chose — that pair
// is what makes the model's real accuracy measurable later, rather than a
// matter of opinion.
import React, { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@shared/api/client";

interface Proposal {
  id: string;
  step_id: string;
  stage_id: string | null;
  status: string;
  proposed: { value?: unknown; reasoning?: string };
  final_output: { value?: unknown; source?: string };
  model: string | null;
  provider: string | null;
  prompt_sha256: string | null;
  error: string | null;
  decided_by: string | null;
  decided_at: string | null;
  created_at: string | null;
}

const STATUS_COLOR: Record<string, string> = {
  pending: "var(--status-warning, #d98c00)",
  failed: "var(--status-failed)",
  accepted: "var(--accent)",
  overridden: "var(--accent)",
  auto_accepted: "var(--text-muted)",
  rejected: "var(--text-muted)",
};

function render(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

export default function AIProposals({ caseId }: { caseId: string }) {
  const [rows, setRows] = useState<Proposal[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await apiFetch(`/api/v1/cases/${caseId}/ai-actions`);
      if (!r.ok) throw new Error(await r.text());
      setRows((await r.json()).proposals ?? []);
      setError(null);
    } catch (e: any) {
      setError(e?.message ?? "Could not load proposals");
    } finally {
      setLoading(false);
    }
  }, [caseId]);

  useEffect(() => { void load(); }, [load]);

  async function decide(id: string, decision: "accept" | "reject" | "override") {
    setBusy(id);
    setError(null);
    try {
      const body: Record<string, unknown> = { decision };
      if (decision === "override") body.value = editing[id] ?? "";
      const r = await apiFetch(`/api/v1/cases/${caseId}/ai-actions/${id}/decide`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(await r.text());
      await load();
    } catch (e: any) {
      setError(e?.message ?? "Decision failed");
    } finally {
      setBusy(null);
    }
  }

  if (loading) return <div style={{ padding: 16, color: "var(--text-muted)" }}>Loading…</div>;

  const open = rows.filter(r => r.status === "pending" || r.status === "failed");
  const settled = rows.filter(r => !open.includes(r));

  return (
    <div style={{ padding: 16 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 12 }}>
        <h3 style={{ margin: 0, fontSize: 14 }}>AI Proposals</h3>
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
          The model proposes — you decide. Nothing is written until you accept or override.
        </span>
      </div>

      {error && (
        <div style={{ marginBottom: 12, padding: 8, fontSize: 11, borderRadius: 4,
                      background: "var(--bg-input)", color: "var(--status-failed)" }}>
          {error}
        </div>
      )}

      {rows.length === 0 && (
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
          No AI proposals on this case.
        </div>
      )}

      {open.map(p => (
        <div key={p.id} style={{ border: "1px solid var(--border-default)", borderRadius: 6,
                                 padding: 12, marginBottom: 10, background: "var(--bg-card)" }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
            <strong style={{ fontSize: 12 }}>Step {p.step_id}</strong>
            <span style={{ fontSize: 11, color: STATUS_COLOR[p.status] ?? "var(--text-muted)" }}>
              {p.status}
            </span>
          </div>

          {p.status === "failed" ? (
            <div style={{ fontSize: 11, color: "var(--status-failed)", marginBottom: 8 }}>
              The model produced nothing usable: {p.error}. You can still supply a value
              yourself, or reject the step.
            </div>
          ) : (
            <>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 2 }}>Proposed</div>
              <pre style={{ margin: 0, marginBottom: 8, padding: 8, fontSize: 11,
                            background: "var(--bg-input)", borderRadius: 4, overflowX: "auto" }}>
                {render(p.proposed?.value)}
              </pre>
              {p.proposed?.reasoning && (
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 8 }}>
                  Reasoning: {p.proposed.reasoning}
                </div>
              )}
            </>
          )}

          <input
            value={editing[p.id] ?? ""}
            onChange={e => setEditing({ ...editing, [p.id]: e.target.value })}
            placeholder="Your value (for override)"
            style={{ width: "100%", padding: "6px 8px", fontSize: 11, marginBottom: 8,
                     background: "var(--bg-input)", border: "1px solid var(--border-default)",
                     borderRadius: 4, color: "var(--text-primary)", boxSizing: "border-box" }}
          />

          <div style={{ display: "flex", gap: 8 }}>
            <button disabled={busy === p.id || p.status === "failed"}
                    onClick={() => decide(p.id, "accept")}
                    title={p.status === "failed"
                      ? "Nothing was proposed — override or reject instead"
                      : "Write the model's value"}>
              Accept
            </button>
            <button disabled={busy === p.id || !(editing[p.id] ?? "").trim()}
                    onClick={() => decide(p.id, "override")}>
              Override
            </button>
            <button disabled={busy === p.id} onClick={() => decide(p.id, "reject")}>
              Reject
            </button>
          </div>

          <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 8 }}>
            {p.provider ?? "—"} · {p.model ?? "—"}
            {p.prompt_sha256 && <> · prompt {p.prompt_sha256.slice(0, 12)}…</>}
          </div>
        </div>
      ))}

      {settled.length > 0 && (
        <>
          <h4 style={{ fontSize: 12, margin: "16px 0 8px" }}>Decided</h4>
          {settled.map(p => (
            <div key={p.id} style={{ fontSize: 11, padding: "6px 0",
                                     borderBottom: "1px solid var(--border-default)" }}>
              <span style={{ color: STATUS_COLOR[p.status] ?? "var(--text-muted)" }}>
                {p.status}
              </span>{" "}
              · step {p.step_id} · {render(p.final_output?.value)}
              {p.decided_by && <> · by {p.decided_by}</>}
              {p.status === "overridden" && (
                <span style={{ color: "var(--text-muted)" }}>
                  {" "}(model said {render(p.proposed?.value)})
                </span>
              )}
            </div>
          ))}
        </>
      )}
    </div>
  );
}
