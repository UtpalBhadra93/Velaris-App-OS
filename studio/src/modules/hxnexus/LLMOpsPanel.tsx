// LLMOps — per-call AI metrics (#32 / report §5.4).
//
// Answers the three questions that had no answer before this shipped: what is
// AI costing, which caller is spending it, and did quality change when the
// model changed. Admin-only, because the metrics are platform-wide rather than
// tenant-scoped — the recording point sits below the request layer and has no
// tenant to attribute a call to.
//
// Token counts are ESTIMATES from character length; no backend here reports
// provider usage. The panel says so rather than presenting a guess as an
// invoice.
import React, { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@shared/api/client";
import { errorFromResponse } from "@shared/api/client";
import ErrorCodeLink from "@shared/errors/ErrorCodeLink";

interface Group {
  key: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
  avg_latency_ms: number;
}

interface Metrics {
  group_by: string;
  total_calls: number;
  total_cost_usd: number;
  total_tokens: number;
  error_calls: number;
  error_rate: number;
  tokens_estimated: boolean;
  note: string;
  groups: Group[];
}

const AXES = ["model", "provider", "purpose", "route"] as const;
const WINDOWS: Array<[string, number | null]> = [
  ["24h", 24], ["7d", 168], ["30d", 720], ["All", null],
];

export default function LLMOpsPanel() {
  const [data, setData] = useState<Metrics | null>(null);
  const [groupBy, setGroupBy] = useState<string>("model");
  const [hours, setHours] = useState<number | null>(24);
  const [error, setError] = useState<string | null>(null);
  const [errObj, setErrObj] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const q = `group_by=${groupBy}${hours ? `&hours=${hours}` : ""}`;
      const r = await apiFetch(`/hxnexus/llmops/metrics?${q}`);
      if (r.status === 403 || r.status === 401) {
        setError("Admin access required — these metrics are platform-wide.");
        setErrObj(null);
        setData(null);
        return;
      }
      // `r.text()` threw away a JSON body that carries the code. Parse it,
      // and keep the text path for a non-JSON response (a gateway page, say).
      if (!r.ok) throw await errorFromResponse(r);
      setData(await r.json());
      setError(null);
      setErrObj(null);
    } catch (e: any) {
      setError(e?.message ?? "Could not load metrics");
      setErrObj(e);
    } finally {
      setLoading(false);
    }
  }, [groupBy, hours]);

  useEffect(() => { void load(); }, [load]);

  return (
    <div style={{ padding: 16 }}>
      <h3 style={{ margin: "0 0 4px", fontSize: 14 }}>AI Usage & Cost</h3>
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 12 }}>
        Every completion the platform makes, recorded at the single chokepoint all
        AI calls pass through — including failures.
      </div>

      <div style={{ display: "flex", gap: 12, marginBottom: 12, flexWrap: "wrap" }}>
        <div style={{ display: "flex", gap: 4 }}>
          {AXES.map(a => (
            <button key={a} onClick={() => setGroupBy(a)} style={{
              padding: "4px 10px", fontSize: 11, borderRadius: 4, cursor: "pointer",
              border: "1px solid var(--border-default)",
              background: groupBy === a ? "var(--accent)" : "transparent",
              color: groupBy === a ? "var(--bg-base)" : "var(--text-muted)",
            }}>{a}</button>
          ))}
        </div>
        <div style={{ display: "flex", gap: 4 }}>
          {WINDOWS.map(([label, h]) => (
            <button key={label} onClick={() => setHours(h)} style={{
              padding: "4px 10px", fontSize: 11, borderRadius: 4, cursor: "pointer",
              border: "1px solid var(--border-default)",
              background: hours === h ? "var(--accent)" : "transparent",
              color: hours === h ? "var(--bg-base)" : "var(--text-muted)",
            }}>{label}</button>
          ))}
        </div>
      </div>

      {error && (
        <div style={{ padding: 8, fontSize: 11, borderRadius: 4,
                      background: "var(--bg-input)", color: "var(--status-failed)" }}>
          {error}<ErrorCodeLink error={errObj} componentKey="hxnexus" />
        </div>
      )}

      {loading && !data && (
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>Loading…</div>
      )}

      {data && (
        <>
          <div style={{ display: "flex", gap: 16, marginBottom: 12, flexWrap: "wrap" }}>
            <Stat label="Calls" value={data.total_calls.toLocaleString()} />
            <Stat label="Tokens (est.)" value={data.total_tokens.toLocaleString()} />
            <Stat label="Cost (est.)" value={`$${data.total_cost_usd.toFixed(4)}`} />
            <Stat label="Errors" value={`${data.error_calls} (${(data.error_rate * 100).toFixed(1)}%)`}
                  warn={data.error_rate > 0.05} />
          </div>

          {data.total_calls === 0 ? (
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              No AI calls recorded in this window.
            </div>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
              <thead>
                <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
                  <th style={{ padding: "6px 4px" }}>{data.group_by}</th>
                  <th style={{ padding: "6px 4px" }}>Calls</th>
                  <th style={{ padding: "6px 4px" }}>Tokens in / out</th>
                  <th style={{ padding: "6px 4px" }}>Cost</th>
                  <th style={{ padding: "6px 4px" }}>Avg latency</th>
                </tr>
              </thead>
              <tbody>
                {data.groups.map(g => (
                  <tr key={g.key} style={{ borderTop: "1px solid var(--border-subtle)" }}>
                    <td style={{ padding: "6px 4px" }}>{g.key}</td>
                    <td style={{ padding: "6px 4px" }}>{g.calls.toLocaleString()}</td>
                    <td style={{ padding: "6px 4px" }}>
                      {g.prompt_tokens.toLocaleString()} / {g.completion_tokens.toLocaleString()}
                    </td>
                    <td style={{ padding: "6px 4px" }}>${g.cost_usd.toFixed(4)}</td>
                    <td style={{ padding: "6px 4px" }}>{g.avg_latency_ms.toFixed(0)} ms</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 10, lineHeight: 1.5 }}>
            {data.note}
          </div>
        </>
      )}
    </div>
  );
}

function Stat({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <div>
      <div style={{ fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase" }}>
        {label}
      </div>
      <div style={{ fontSize: 16, color: warn ? "var(--status-failed)" : "var(--text-primary)" }}>
        {value}
      </div>
    </div>
  );
}
