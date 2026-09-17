// HxPrivacy (§8.10) — PII discovery & classification.
//
// The review queue is the product here. Detection is heuristic and drives
// retention and residency policy, so a human decides before it changes
// anything — and the screen must make a low-confidence guess *look* like a
// question rather than a verdict.
//
// Copyright (c) 2024-2026 Velaris Contributors
// SPDX-License-Identifier: BSL-1.1
import React, { useEffect, useState } from "react";
import { apiJSON, postJSON, Banner, EmptyHint, Kpi, btn, card, h2, inp, mono, table, td, th } from "./shared";

type Finding = {
  id: string;
  resource_type: string;
  resource_name: string | null;
  field_path: string;
  field_label: string | null;
  category: string;
  confidence: number;
  reason: string;
  special_category: boolean;
  status: string;
  reviewed_by: string | null;
  last_seen_at: string | null;
};

type Summary = {
  total: number;
  by_status: Record<string, number>;
  by_category: Record<string, number>;
  special_category: number;
  awaiting_review: number;
};

const STATUSES = ["", "detected", "confirmed", "dismissed", "stale"];

export default function PrivacyPanel() {
  const [findings, setFindings] = useState<Finding[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [status, setStatus] = useState("detected");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  async function load(s = status) {
    setErr(null);
    try {
      const q = s ? `?status=${encodeURIComponent(s)}` : "";
      const r = await apiJSON<{ summary: Summary; findings: Finding[] }>(
        `/api/v1/compliance/privacy/findings${q}`,
      );
      setSummary(r.summary);
      setFindings(r.findings);
    } catch (e: any) { setErr(e.message); }
  }

  useEffect(() => { load(); }, []);           // eslint-disable-line react-hooks/exhaustive-deps

  async function scan() {
    setBusy("scan"); setErr(null); setNote(null);
    try {
      const r = await apiJSON<Record<string, number>>(
        "/api/v1/compliance/privacy/scan", { method: "POST" },
      );
      setNote(
        `Scanned ${r.scanned_forms} forms and ${r.scanned_data_models} data models — ` +
        `${r.findings_created} new, ${r.findings_updated} refreshed, ${r.findings_marked_stale} stale.`,
      );
      await load();
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(null); }
  }

  async function review(id: string, decision: "confirmed" | "dismissed") {
    setBusy(id); setErr(null);
    try {
      await postJSON(`/api/v1/compliance/privacy/findings/${id}/review`, { decision });
      await load();
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(null); }
  }

  return (
    <>
      <section style={card}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
          <h2 style={h2}>Personal data inventory</h2>
          <button onClick={scan} disabled={!!busy} style={btn(true)}>
            {busy === "scan" ? "Scanning…" : "Run scan"}
          </button>
        </div>

        {summary && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
            <Kpi label="Findings" value={summary.total} />
            <Kpi label="Awaiting review" value={summary.awaiting_review} warn={summary.awaiting_review > 0} />
            <Kpi label="Special category" value={summary.special_category} warn={summary.special_category > 0} />
            <Kpi label="Confirmed" value={summary.by_status?.confirmed ?? 0} />
          </div>
        )}

        <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 10 }}>
          Re-running a scan never overwrites a human verdict — a confirmed or dismissed finding
          only has its last-seen time refreshed. Fields that disappear are marked stale, not deleted.
        </div>

        {err && <Banner tone="bad">⚠ {err}</Banner>}
        {note && <Banner tone="ok">{note}</Banner>}
        {summary?.special_category ? (
          <Banner tone="warn">
            {summary.special_category} finding(s) are Article 9 special category (health or biometric).
            These carry a higher bar than ordinary personal data.
          </Banner>
        ) : null}
      </section>

      <section style={card}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 10 }}>
          <h2 style={{ ...h2, margin: 0 }}>Review queue</h2>
          <select
            value={status}
            onChange={e => { setStatus(e.target.value); load(e.target.value); }}
            style={{ ...inp, marginLeft: "auto" }}
          >
            {STATUSES.map(s => <option key={s} value={s}>{s || "all statuses"}</option>)}
          </select>
        </div>

        {findings.length === 0 ? (
          <EmptyHint>
            Nothing to show. If you have never run a scan, the inventory is empty because
            nothing has looked — not because no personal data exists.
          </EmptyHint>
        ) : (
          <table style={table}>
            <thead>
              <tr>
                <th style={th}>Field</th>
                <th style={th}>Where</th>
                <th style={th}>Category</th>
                <th style={th}>Confidence</th>
                <th style={th}>Why</th>
                <th style={th}>Status</th>
                <th style={th}>Decide</th>
              </tr>
            </thead>
            <tbody>
              {findings.map(f => (
                <tr key={f.id} style={f.special_category ? { background: "rgba(247,183,49,0.10)" } : undefined}>
                  <td style={{ ...td, ...mono }}>
                    {f.field_path}
                    {f.special_category && <span title="Article 9 special category"> ⚠</span>}
                  </td>
                  <td style={td}>
                    {f.resource_name || "—"}
                    <span style={{ color: "var(--text-muted)" }}> ({f.resource_type})</span>
                  </td>
                  <td style={td}>{f.category}</td>
                  <td style={td}>
                    <Confidence value={f.confidence} />
                  </td>
                  <td style={{ ...td, color: "var(--text-secondary)" }}>{f.reason}</td>
                  <td style={td}>{f.status}{f.reviewed_by ? ` (${f.reviewed_by})` : ""}</td>
                  <td style={td}>
                    {f.status === "detected" ? (
                      <>
                        <button onClick={() => review(f.id, "confirmed")} disabled={!!busy} style={btn()}>
                          Confirm
                        </button>{" "}
                        <button onClick={() => review(f.id, "dismissed")} disabled={!!busy} style={btn()}>
                          Dismiss
                        </button>
                      </>
                    ) : <span style={{ color: "var(--text-muted)" }}>—</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}

/** A 0.4 guess must not look like a 0.95 fact. */
function Confidence({ value }: { value: number }) {
  const low = value < 0.6;
  return (
    <span style={{ color: low ? "var(--status-running)" : "var(--text-primary)", fontWeight: low ? 400 : 600 }}>
      {(value * 100).toFixed(0)}%{low ? " — needs a human" : ""}
    </span>
  );
}
