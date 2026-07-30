// HxSentinel P3b — supervisor oversight dashboard.
// Per-reviewer scoring stats for the tenant: score distribution, outcome-agreement
// reliability (disputed scores excluded), median time-to-score, and a flag for
// reviewers who need attention. Read-only; gated `risk.scores.review`.
import React, { useEffect, useState } from "react";
import { apiFetch } from "@shared/api/client";

interface Reviewer {
  reviewer: string;
  count: number;
  disputed: number;
  distribution: Record<string, number>;
  determinable: number;
  reliability: number | null;
  median_time_to_score_s: number | null;
  flagged: boolean;
}

function relColor(r: number | null): string {
  if (r === null) return "var(--text-muted)";
  if (r < 0.6) return "var(--status-failed)";
  if (r < 0.8) return "var(--status-warning, #d98c00)";
  return "var(--accent)";
}

interface Proposal {
  id: string; rule_key: string; proposed_change: Record<string, any>;
  rationale: string; evidence: Record<string, any>; status: string;
}

interface Pack {
  pack_id: string; name: string; vertical: string | null;
  version: string; enabled: boolean; rule_count: number;
}

interface CatalogueEntry {
  pack_id: string; name: string; vertical: string | null;
  version: string; description: string; rule_count: number; installed: boolean;
}

interface RiskModel {
  version: number; status: string; trained_on: number;
  metrics: { holdout_auc: number | null; positives: number; negatives: number };
}

export default function RiskReview() {
  const [reviewers, setReviewers] = useState<Reviewer[]>([]);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [packs, setPacks] = useState<Pack[]>([]);
  const [catalogue, setCatalogue] = useState<CatalogueEntry[]>([]);
  const [models, setModels] = useState<RiskModel[]>([]);
  const [manifestText, setManifestText] = useState("");
  const [loading, setLoading] = useState(true);
  const [off, setOff] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = async () => {
    setLoading(true); setErr(null);
    try {
      const r = await apiFetch("/api/v1/risk/scores/dashboard");
      if (r.status === 404) { setOff(true); return; }
      if (!r.ok) throw new Error(await r.text());
      setReviewers((await r.json()).reviewers || []);
      const pr = await apiFetch("/api/v1/risk/rules/proposals");
      if (pr.ok) setProposals((await pr.json()).proposals || []);
      const pk = await apiFetch("/api/v1/risk/packs");
      if (pk.ok) setPacks((await pk.json()).packs || []);
      const cat = await apiFetch("/api/v1/risk/packs/catalogue");
      if (cat.ok) setCatalogue((await cat.json()).catalogue || []);
      const md = await apiFetch("/api/v1/risk/model");
      if (md.ok) setModels((await md.json()).models || []);
    } catch (e: any) { setErr(e?.message || "Failed to load"); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, []);

  const trainModel = async () => {
    setBusy("train"); setErr(null);
    try {
      const r = await apiFetch("/api/v1/risk/model/train", { method: "POST" });
      if (!r.ok) throw new Error(await r.text());
      await load();
    } catch (e: any) { setErr(e?.message || "Not enough labelled data to train yet"); }
    finally { setBusy(null); }
  };

  const activateModel = async (version: number) => {
    setBusy(`m${version}`);
    try { await apiFetch(`/api/v1/risk/model/${version}/activate`, { method: "POST" }); await load(); }
    catch (e: any) { setErr(e?.message || "Activate failed"); }
    finally { setBusy(null); }
  };

  const installOfficial = async (packId: string) => {
    setBusy(`cat-${packId}`);
    try { await apiFetch(`/api/v1/risk/packs/catalogue/${packId}/install`, { method: "POST" }); await load(); }
    catch (e: any) { setErr(e?.message || "Install failed"); }
    finally { setBusy(null); }
  };

  const installPack = async () => {
    setBusy("install"); setErr(null);
    try {
      const manifest = JSON.parse(manifestText);
      const r = await apiFetch("/api/v1/risk/packs/install", {
        method: "POST", body: JSON.stringify({ manifest }) });
      if (!r.ok) throw new Error(await r.text());
      setManifestText(""); await load();
    } catch (e: any) { setErr(e?.message || "Install failed (check the manifest JSON)"); }
    finally { setBusy(null); }
  };

  const uninstallPack = async (packId: string) => {
    setBusy(packId);
    try { await apiFetch(`/api/v1/risk/packs/${packId}`, { method: "DELETE" }); await load(); }
    catch (e: any) { setErr(e?.message || "Uninstall failed"); }
    finally { setBusy(null); }
  };

  const analyze = async () => {
    setBusy("analyze");
    try { await apiFetch("/api/v1/risk/rules/tuning/analyze", { method: "POST" }); await load(); }
    catch (e: any) { setErr(e?.message || "Analyze failed"); }
    finally { setBusy(null); }
  };

  const mineNotes = async () => {
    setBusy("mine");
    try { await apiFetch("/api/v1/risk/rules/tuning/mine-notes", { method: "POST" }); await load(); }
    catch (e: any) { setErr(e?.message || "Notes mining failed"); }
    finally { setBusy(null); }
  };

  const decide = async (id: string, decision: "approved" | "rejected") => {
    setBusy(id);
    try {
      await apiFetch(`/api/v1/risk/rules/proposals/${id}/decision`, {
        method: "POST", body: JSON.stringify({ decision }) });
      await load();
    } catch (e: any) { setErr(e?.message || "Decision failed"); }
    finally { setBusy(null); }
  };

  if (loading) return <div style={{ padding: 24, color: "var(--text-muted)" }}>Loading…</div>;
  if (off) return (
    <div style={{ padding: 24, color: "var(--text-muted)", maxWidth: 640 }}>
      HxSentinel is not enabled for this workspace.
    </div>
  );

  return (
    <div style={{ padding: "var(--space-xl)", maxWidth: 1000 }}>
      <h2 style={{ margin: "0 0 4px" }}>Risk Review</h2>
      <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 16 }}>
        How each reviewer scores HxSentinel findings. Reliability is agreement with confirmed
        outcomes (disputed scores excluded). Flagged = low reliability or suspiciously fast scoring.
      </div>
      {err && <div style={{ color: "var(--status-failed)", fontSize: 13, marginBottom: 12 }}>{err}</div>}
      {reviewers.length === 0 ? (
        <div style={{ color: "var(--text-muted)", fontSize: 13 }}>No scores recorded yet.</div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--text-muted)", fontSize: 11, textTransform: "uppercase" }}>
                <th style={{ padding: "6px 8px" }}>Reviewer</th>
                <th style={{ padding: "6px 8px" }}>Scores</th>
                <th style={{ padding: "6px 8px" }}>Distribution (1–5)</th>
                <th style={{ padding: "6px 8px" }}>Reliability</th>
                <th style={{ padding: "6px 8px" }}>Median time</th>
                <th style={{ padding: "6px 8px" }}>Disputed</th>
                <th style={{ padding: "6px 8px" }} />
              </tr>
            </thead>
            <tbody>
              {reviewers.map((r) => (
                <tr key={r.reviewer} style={{ borderTop: "1px solid var(--border-subtle)" }}>
                  <td style={{ padding: "8px", fontFamily: "var(--font-mono)" }}>{r.reviewer}</td>
                  <td style={{ padding: "8px" }}>{r.count}</td>
                  <td style={{ padding: "8px", fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>
                    {[1, 2, 3, 4, 5].map((n) => `${r.distribution[n] || 0}`).join(" · ")}
                  </td>
                  <td style={{ padding: "8px", fontWeight: 700, color: relColor(r.reliability) }}>
                    {r.reliability === null ? "—" : `${Math.round(r.reliability * 100)}%`}
                    {r.determinable > 0 && (
                      <span style={{ fontSize: 10, color: "var(--text-muted)", fontWeight: 400 }}> ({r.determinable})</span>
                    )}
                  </td>
                  <td style={{ padding: "8px", color: "var(--text-muted)" }}>
                    {r.median_time_to_score_s === null ? "—" : `${r.median_time_to_score_s}s`}
                  </td>
                  <td style={{ padding: "8px" }}>{r.disputed || "—"}</td>
                  <td style={{ padding: "8px" }}>
                    {r.flagged && (
                      <span style={{ fontSize: 10, fontWeight: 700, color: "#fff",
                        background: "var(--status-failed)", padding: "1px 8px", borderRadius: 8 }}>
                        REVIEW
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div style={{ display: "flex", alignItems: "center", gap: 12, margin: "28px 0 12px" }}>
        <h3 style={{ margin: 0 }}>Rule tuning proposals</h3>
        <button onClick={analyze} disabled={busy === "analyze"} style={{ fontSize: 12,
          padding: "4px 12px", borderRadius: 6, border: "1px solid var(--border-default)",
          background: "var(--bg-secondary)", color: "var(--text-secondary)", cursor: "pointer" }}>
          {busy === "analyze" ? "Analyzing…" : "Analyze scores"}
        </button>
        <button onClick={mineNotes} disabled={busy === "mine"} style={{ fontSize: 12,
          padding: "4px 12px", borderRadius: 6, border: "1px solid var(--border-default)",
          background: "var(--bg-secondary)", color: "var(--text-secondary)", cursor: "pointer" }}>
          {busy === "mine" ? "Mining…" : "Mine notes"}
        </button>
      </div>
      <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
        Reliability-weighted scores and reviewers' notes (mined locally) propose rule changes.
        Nothing applies until you approve it.
      </div>
      {proposals.length === 0 ? (
        <div style={{ color: "var(--text-muted)", fontSize: 13 }}>No pending proposals.</div>
      ) : proposals.map((p) => (
        <div key={p.id} style={{ border: "1px solid var(--border-subtle)", borderRadius: 8,
          padding: 12, marginBottom: 8, display: "flex", flexDirection: "column", gap: 6 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{p.rule_key}</span>
            <span style={{ fontSize: 11, color: "var(--accent)", fontWeight: 600 }}>
              {Object.keys(p.proposed_change || {}).length ? JSON.stringify(p.proposed_change) : "advisory"}
            </span>
            <span style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
              {p.evidence?.source === "notes_mining" ? "🗒 notes" : "📊 scores"}
            </span>
          </div>
          <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>{p.rationale}</div>
          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={() => decide(p.id, "approved")} disabled={busy === p.id}
              style={{ fontSize: 12, padding: "4px 12px", borderRadius: 6, border: "none",
                background: "var(--accent)", color: "#fff", cursor: "pointer" }}>Approve</button>
            <button onClick={() => decide(p.id, "rejected")} disabled={busy === p.id}
              style={{ fontSize: 12, padding: "4px 12px", borderRadius: 6,
                border: "1px solid var(--border-default)", background: "transparent",
                color: "var(--text-secondary)", cursor: "pointer" }}>Reject</button>
          </div>
        </div>
      ))}

      <div style={{ display: "flex", alignItems: "center", gap: 12, margin: "28px 0 8px" }}>
        <h3 style={{ margin: 0 }}>Local risk model</h3>
        <button onClick={trainModel} disabled={busy === "train"} style={{ fontSize: 12,
          padding: "4px 12px", borderRadius: 6, border: "1px solid var(--border-default)",
          background: "var(--bg-secondary)", color: "var(--text-secondary)", cursor: "pointer" }}>
          {busy === "train" ? "Training…" : "Train new version"}
        </button>
      </div>
      <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 10 }}>
        A local, explainable logistic regression over label-independent features (amounts,
        velocities). Recommends, never decides — activate a version to score cases.
      </div>
      {models.length === 0 ? (
        <div style={{ color: "var(--text-muted)", fontSize: 13, marginBottom: 8 }}>No model trained yet.</div>
      ) : models.map((mdl) => (
        <div key={mdl.version} style={{ border: "1px solid var(--border-subtle)", borderRadius: 8,
          padding: 10, marginBottom: 6, display: "flex", alignItems: "center", gap: 10, fontSize: 13 }}>
          <span style={{ fontWeight: 600 }}>v{mdl.version}</span>
          <span style={{ fontSize: 11, padding: "1px 8px", borderRadius: 8, color: "#fff",
            background: mdl.status === "active" ? "var(--accent)" : "var(--text-muted)" }}>{mdl.status}</span>
          <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
            AUC {mdl.metrics.holdout_auc ?? "—"} · {mdl.trained_on} cases ({mdl.metrics.positives} bad / {mdl.metrics.negatives} clean)
          </span>
          {mdl.status !== "active" && (
            <button onClick={() => activateModel(mdl.version)} disabled={busy === `m${mdl.version}`}
              style={{ marginLeft: "auto", fontSize: 12, padding: "3px 12px", borderRadius: 6, border: "none",
                background: "var(--accent)", color: "#fff", cursor: "pointer" }}>Activate</button>
          )}
        </div>
      ))}

      <h3 style={{ margin: "28px 0 8px" }}>Signal packs</h3>
      <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
        Vertical rule bundles (content, never code). Rule conditions are safe expressions,
        validated on install and run by the in-core engine.
      </div>

      {catalogue.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase",
            letterSpacing: "0.04em", marginBottom: 6 }}>Official catalogue</div>
          {catalogue.map((c) => (
            <div key={c.pack_id} style={{ border: "1px solid var(--border-subtle)", borderRadius: 8,
              padding: 10, marginBottom: 6, display: "flex", alignItems: "center", gap: 10 }}>
              <div style={{ display: "flex", flexDirection: "column" }}>
                <span style={{ fontWeight: 600, fontSize: 13 }}>{c.name}</span>
                <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                  {c.description} · {c.rule_count} rules{c.vertical ? ` · ${c.vertical}` : ""}
                </span>
              </div>
              {c.installed ? (
                <span style={{ marginLeft: "auto", fontSize: 11, color: "var(--accent)", fontWeight: 600 }}>Installed</span>
              ) : (
                <button onClick={() => installOfficial(c.pack_id)} disabled={busy === `cat-${c.pack_id}`}
                  style={{ marginLeft: "auto", fontSize: 12, padding: "3px 12px", borderRadius: 6,
                    border: "none", background: "var(--accent)", color: "#fff", cursor: "pointer" }}>
                  {busy === `cat-${c.pack_id}` ? "…" : "Install"}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {packs.map((p) => (
        <div key={p.pack_id} style={{ border: "1px solid var(--border-subtle)", borderRadius: 8,
          padding: 10, marginBottom: 8, display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontWeight: 600, fontSize: 13 }}>{p.name}</span>
          <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
            {p.pack_id} · v{p.version} · {p.rule_count} rules{p.vertical ? ` · ${p.vertical}` : ""}
          </span>
          <button onClick={() => uninstallPack(p.pack_id)} disabled={busy === p.pack_id}
            style={{ marginLeft: "auto", fontSize: 12, padding: "3px 10px", borderRadius: 6,
              border: "1px solid var(--border-default)", background: "transparent",
              color: "var(--text-secondary)", cursor: "pointer" }}>Uninstall</button>
        </div>
      ))}
      <textarea value={manifestText} onChange={(e) => setManifestText(e.target.value)}
        placeholder='Paste a pack manifest JSON to install…'
        rows={4} style={{ width: "100%", fontSize: 12, fontFamily: "var(--font-mono)", padding: 8,
          borderRadius: 6, border: "1px solid var(--border-default)", background: "var(--bg-secondary)",
          color: "var(--text-primary)", marginTop: 4 }} />
      <button onClick={installPack} disabled={!manifestText.trim() || busy === "install"}
        style={{ marginTop: 6, fontSize: 12, padding: "4px 14px", borderRadius: 6, border: "none",
          background: "var(--accent)", color: "#fff", cursor: "pointer" }}>
        {busy === "install" ? "Installing…" : "Install pack"}
      </button>
    </div>
  );
}
