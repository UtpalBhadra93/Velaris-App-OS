// HxSentinel P3b — supervisor oversight dashboard.
// Per-reviewer scoring stats for the tenant: score distribution, outcome-agreement
// reliability (disputed scores excluded), median time-to-score, and a flag for
// reviewers who need attention. Read-only; gated `risk.scores.review`.
import React, { useEffect, useState } from "react";
import { apiFetch, errorFromResponse } from "@shared/api/client";

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

interface OutcomeLabel { key: string; display: string; description?: string | null; is_core: boolean }
interface UnlabelledCase { case_id: string; case_number: string; status: string; closed_at: string | null }

/**
 * Retro-labelling queue — the bootstrap path for HxSentinel.
 *
 * Every later phase (rules, tuning, the local model) compares new cases against
 * confirmed outcomes, so with an empty `case_outcomes` the whole module is inert
 * no matter how many cases exist. This is the only surface that fills it in bulk.
 */
function LabellingTab() {
  const [rows, setRows] = useState<UnlabelledCase[]>([]);
  const [labels, setLabels] = useState<OutcomeLabel[]>([]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [outcome, setOutcome] = useState("");
  const [note, setNote] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const t = await apiFetch("/risk/taxonomy");
      if (t.ok) setLabels((await t.json()).labels || []);
      const r = await apiFetch("/risk/outcomes/unlabelled?limit=200");
      if (!r.ok) throw await errorFromResponse(r, "Could not load the queue");
      setRows((await r.json()).cases || []);
      setPicked(new Set());
    } catch (e: any) { setErr(e?.message || "Could not load the queue"); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, []);

  const toggle = (id: string) => setPicked((s) => {
    const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n;
  });
  const allPicked = rows.length > 0 && picked.size === rows.length;

  const submit = async () => {
    if (!outcome || picked.size === 0) return;
    setBusy(true); setErr(null); setResult(null);
    try {
      const r = await apiFetch("/risk/outcomes/bulk", {
        method: "POST",
        body: JSON.stringify({ case_ids: [...picked], outcome, note: note.trim() || null }),
      });
      if (!r.ok) throw await errorFromResponse(r, "Bulk labelling failed");
      const b = await r.json();
      // Partial success is reported per case by design — one unreadable case in
      // a large selection is skipped, not fatal. Saying "done" would hide that.
      setResult(
        b.skipped_count
          ? `Labelled ${b.labelled_count}; skipped ${b.skipped_count} (not found or not permitted).`
          : `Labelled ${b.labelled_count} case(s) as ${outcome}.`,
      );
      setNote("");
      await load();
    } catch (e: any) { setErr(e?.message || "Bulk labelling failed"); }
    finally { setBusy(false); }
  };

  if (loading) return <div style={{ color: "var(--text-muted)", fontSize: 13 }}>Loading queue…</div>;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
        Closed cases with no confirmed outcome. Labelling these is what gives the rule engine
        and the local model something to be right or wrong about — until it has ground truth,
        HxSentinel produces nothing. Training needs at least 20 labelled cases with 5 or more
        of each class.
      </div>
      {err && <div style={{ color: "var(--status-failed)", fontSize: 13 }}>{err}</div>}
      {result && (
        <div style={{ color: "var(--text-secondary)", fontSize: 13,
          borderLeft: "2px solid var(--accent)", paddingLeft: 10 }}>{result}</div>
      )}

      {rows.length === 0 ? (
        <div style={{ color: "var(--text-muted)", fontSize: 13 }}>
          Nothing unlabelled — every closed case has a confirmed outcome.
        </div>
      ) : (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <select value={outcome} onChange={(e) => setOutcome(e.target.value)}
              style={{ fontSize: 12, padding: "5px 8px", borderRadius: 6,
                border: "1px solid var(--border-default)", background: "var(--bg-secondary)",
                color: "var(--text-primary)" }}>
              <option value="">Outcome to apply…</option>
              {labels.map((l) => <option key={l.key} value={l.key}>{l.display}</option>)}
            </select>
            <input value={note} onChange={(e) => setNote(e.target.value)}
              placeholder="Note (optional)"
              style={{ flex: 1, minWidth: 200, fontSize: 12, padding: "5px 8px", borderRadius: 6,
                border: "1px solid var(--border-default)", background: "var(--bg-secondary)",
                color: "var(--text-primary)" }} />
            <button onClick={submit} disabled={!outcome || picked.size === 0 || busy}
              style={{ fontSize: 12, padding: "5px 14px", borderRadius: 6, border: "none",
                background: outcome && picked.size ? "var(--accent)" : "var(--bg-secondary)",
                color: outcome && picked.size ? "#fff" : "var(--text-muted)",
                cursor: outcome && picked.size ? "pointer" : "default" }}>
              {busy ? "Labelling…" : `Label ${picked.size || ""} selected`}
            </button>
          </div>

          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ textAlign: "left", color: "var(--text-muted)", fontSize: 11, textTransform: "uppercase" }}>
                  <th style={{ padding: "6px 8px", width: 28 }}>
                    <input type="checkbox" checked={allPicked}
                      onChange={() => setPicked(allPicked ? new Set() : new Set(rows.map((r) => r.case_id)))} />
                  </th>
                  <th style={{ padding: "6px 8px" }}>Case</th>
                  <th style={{ padding: "6px 8px" }}>Status</th>
                  <th style={{ padding: "6px 8px" }}>Closed</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((c) => (
                  <tr key={c.case_id} style={{ borderTop: "1px solid var(--border-subtle)" }}>
                    <td style={{ padding: "6px 8px" }}>
                      <input type="checkbox" checked={picked.has(c.case_id)} onChange={() => toggle(c.case_id)} />
                    </td>
                    <td style={{ padding: "6px 8px", fontFamily: "var(--font-mono)" }}>{c.case_number}</td>
                    <td style={{ padding: "6px 8px", color: "var(--text-muted)" }}>{c.status}</td>
                    <td style={{ padding: "6px 8px", color: "var(--text-muted)" }}>
                      {c.closed_at ? new Date(c.closed_at).toLocaleDateString() : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

/**
 * The verify gate — HxSentinel's second switch.
 *
 * The release flag makes the module reachable; this makes it *run*. While no
 * severity is set, `advance_blocked()` short-circuits before evaluating, so no
 * findings are ever produced on the stage-advance path however many cases flow
 * through. Turning it on is what arms evaluation.
 */
function GateTab() {
  const [severity, setSeverity] = useState<string>("off");
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      const r = await apiFetch("/risk/config");
      if (r.ok) setSeverity((await r.json()).verify_gate_severity || "off");
      setLoaded(true);
    })();
  }, []);

  const save = async (next: string) => {
    setBusy(true); setErr(null); setSaved(null);
    try {
      const r = await apiFetch("/risk/config", {
        method: "PUT",
        body: JSON.stringify({ verify_gate_severity: next === "off" ? null : next }),
      });
      if (!r.ok) throw await errorFromResponse(r, "Could not update the gate");
      const b = await r.json();
      setSeverity(b.verify_gate_severity || "off");
      setSaved(b.verify_gate_severity
        ? `Gate armed at ${b.verify_gate_severity}. Findings at or above this severity now open a hold that blocks stage advance until a human clears it.`
        : "Gate off. Rules are no longer evaluated on stage advance.");
    } catch (e: any) { setErr(e?.message || "Could not update the gate"); }
    finally { setBusy(false); }
  };

  if (!loaded) return <div style={{ color: "var(--text-muted)", fontSize: 13 }}>Loading…</div>;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, maxWidth: 700 }}>
      <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
        The verify gate decides whether HxSentinel runs at all on the stage-advance path.
        While it is off, rules are never evaluated there and no findings appear. When armed,
        a finding at or above the chosen severity opens a hold, and the case cannot advance
        until someone records what they verified and clears it.
      </div>
      {err && <div style={{ color: "var(--status-failed)", fontSize: 13 }}>{err}</div>}
      {saved && (
        <div style={{ color: "var(--text-secondary)", fontSize: 13,
          borderLeft: "2px solid var(--accent)", paddingLeft: 10 }}>{saved}</div>
      )}
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span style={{ fontSize: 13 }}>Open a hold at severity</span>
        <select value={severity} disabled={busy} onChange={(e) => save(e.target.value)}
          style={{ fontSize: 12, padding: "5px 8px", borderRadius: 6,
            border: "1px solid var(--border-default)", background: "var(--bg-secondary)",
            color: "var(--text-primary)" }}>
          <option value="off">Off — never hold</option>
          <option value="low">Low and above</option>
          <option value="medium">Medium and above</option>
          <option value="high">High only</option>
        </select>
        <span style={{ fontSize: 11, fontWeight: 700,
          color: severity === "off" ? "var(--text-muted)" : "var(--accent)" }}>
          {severity === "off" ? "NOT ARMED" : "ARMED"}
        </span>
      </div>
      <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
        The gate fails open: if HxSentinel errors, cases advance normally rather than wedging.
      </div>
    </div>
  );
}

interface RuleConfig {
  key: string;
  display: string;
  description: string;
  default_severity: string;
  default_params: Record<string, any>;
  enabled: boolean;
  severity: string;
  params: Record<string, any>;
}

const SEVERITIES = ["low", "medium", "high"];

// Rule thresholds were API-only: `PUT /risk/rules/{key}` existed, nothing in
// Studio called it. A tenant could arm the gate but not tune what trips it, so
// the only way to widen identity_velocity's ceiling was curl.
//
// The whole `params` object is sent on every save, deliberately. The stored
// override is REPLACED wholesale (`row.params = params`), while reads merge it
// over the code defaults — so a partial write silently resets every key it
// omits back to default.
function RulesTab() {
  const [rules, setRules] = useState<RuleConfig[]>([]);
  const [draft, setDraft] = useState<Record<string, Record<string, string>>>({});
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  const load = async () => {
    const r = await apiFetch("/risk/rules");
    if (r.ok) {
      const list: RuleConfig[] = (await r.json()).rules || [];
      setRules(list);
      const d: Record<string, Record<string, string>> = {};
      for (const rule of list) {
        d[rule.key] = {};
        for (const [k, v] of Object.entries(rule.params || {})) {
          d[rule.key][k] = Array.isArray(v) ? v.join(", ") : String(v);
        }
      }
      setDraft(d);
    } else {
      setErr("Could not load the rule catalogue — you may not have the risk.rules permission.");
    }
    setLoaded(true);
  };

  useEffect(() => { load(); }, []);

  // Text back to the type the rule expects. The default value is the reference:
  // a param that ships as a number stays a number, a list stays a list.
  const coerce = (rule: RuleConfig, key: string, raw: string): any => {
    const ref = (rule.default_params || {})[key];
    if (Array.isArray(ref)) {
      return raw.split(",").map((s) => s.trim()).filter(Boolean);
    }
    if (typeof ref === "number") {
      const n = Number(raw);
      if (raw.trim() === "" || Number.isNaN(n)) throw new Error(`${key} must be a number`);
      return Number.isInteger(ref) ? Math.trunc(n) : n;
    }
    return raw;
  };

  const save = async (rule: RuleConfig, patch: Partial<RuleConfig>) => {
    setBusy(rule.key); setErr(null); setSaved(null);
    try {
      const params: Record<string, any> = {};
      for (const [k, raw] of Object.entries(draft[rule.key] || {})) {
        params[k] = coerce(rule, k, raw);
      }
      const body = {
        enabled: patch.enabled ?? rule.enabled,
        severity: patch.severity ?? rule.severity,
        params,
      };
      const r = await apiFetch(`/risk/rules/${rule.key}`, {
        method: "PUT", body: JSON.stringify(body),
      });
      if (!r.ok) throw await errorFromResponse(r, "Could not save the rule");
      await load();
      setSaved(`${rule.display} saved.`);
    } catch (e: any) { setErr(e?.message || "Could not save the rule"); }
    finally { setBusy(null); }
  };

  const reset = async (rule: RuleConfig) => {
    setDraft((d) => ({
      ...d,
      [rule.key]: Object.fromEntries(
        Object.entries(rule.default_params || {}).map(([k, v]) => [
          k, Array.isArray(v) ? v.join(", ") : String(v)])),
    }));
    setSaved(`${rule.display} reset to defaults — press Save to apply.`);
  };

  if (!loaded) return <div style={{ color: "var(--text-muted)", fontSize: 13 }}>Loading…</div>;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14, maxWidth: 860 }}>
      <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
        Thresholds for the deterministic rules, per workspace. Defaults live in code;
        anything changed here is stored as an override for this workspace only.
        A rule turned off is never evaluated and can never open a hold.
      </div>
      {err && <div style={{ color: "var(--status-failed)", fontSize: 13 }}>{err}</div>}
      {saved && (
        <div style={{ color: "var(--text-secondary)", fontSize: 13,
          borderLeft: "2px solid var(--accent)", paddingLeft: 10 }}>{saved}</div>
      )}

      {rules.map((rule) => {
        const dirty = Object.entries(draft[rule.key] || {}).some(([k, v]) => {
          const cur = (rule.params || {})[k];
          return v !== (Array.isArray(cur) ? cur.join(", ") : String(cur));
        });
        return (
          <div key={rule.key} style={{ border: "1px solid var(--border-subtle)",
            borderRadius: 8, padding: 12, opacity: rule.enabled ? 1 : 0.6 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
              <span style={{ fontSize: 13, fontWeight: 600 }}>{rule.display}</span>
              <code style={{ fontSize: 11, color: "var(--text-muted)" }}>{rule.key}</code>
              <span style={{ flex: 1 }} />
              <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 5 }}>
                <input type="checkbox" checked={rule.enabled} disabled={busy === rule.key}
                  onChange={(e) => save(rule, { enabled: e.target.checked })} />
                Enabled
              </label>
              <select value={rule.severity} disabled={busy === rule.key}
                onChange={(e) => save(rule, { severity: e.target.value })}
                style={{ fontSize: 12, padding: "4px 7px", borderRadius: 6,
                  border: "1px solid var(--border-default)", background: "var(--bg-secondary)",
                  color: "var(--text-primary)" }}>
                {SEVERITIES.map((s) => (
                  <option key={s} value={s}>
                    {s}{s === rule.default_severity ? " (default)" : ""}
                  </option>
                ))}
              </select>
            </div>

            <div style={{ fontSize: 12, color: "var(--text-muted)", margin: "6px 0 10px" }}>
              {rule.description}
            </div>

            <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
              {Object.keys(rule.default_params || {}).map((k) => (
                <label key={k} style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                  <span style={{ fontSize: 11, color: "var(--text-secondary)" }}>{k}</span>
                  <input
                    value={draft[rule.key]?.[k] ?? ""}
                    disabled={busy === rule.key}
                    onChange={(e) => setDraft((d) => ({
                      ...d, [rule.key]: { ...d[rule.key], [k]: e.target.value } }))}
                    style={{ fontSize: 12, padding: "5px 8px", borderRadius: 6, width: 120,
                      border: "1px solid var(--border-default)", background: "var(--bg-secondary)",
                      color: "var(--text-primary)" }} />
                  <span style={{ fontSize: 10, color: "var(--text-muted)" }}>
                    default {Array.isArray(rule.default_params[k])
                      ? rule.default_params[k].join(", ") : String(rule.default_params[k])}
                  </span>
                </label>
              ))}
              <button onClick={() => save(rule, {})}
                disabled={busy === rule.key || !dirty}
                style={{ fontSize: 12, padding: "6px 12px", borderRadius: 6, cursor: dirty ? "pointer" : "default",
                  border: "1px solid var(--border-default)",
                  background: dirty ? "var(--accent)" : "var(--bg-secondary)",
                  color: dirty ? "#fff" : "var(--text-muted)" }}>
                {busy === rule.key ? "Saving…" : "Save"}
              </button>
              <button onClick={() => reset(rule)} disabled={busy === rule.key}
                style={{ fontSize: 12, padding: "6px 10px", borderRadius: 6, cursor: "pointer",
                  border: "1px solid var(--border-default)", background: "transparent",
                  color: "var(--text-muted)" }}>
                Reset
              </button>
            </div>

            {rule.key === "identity_velocity" && (
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 8 }}>
                <code>max_cases</code> is a ceiling, not a target: a value shared by more
                cases than this is treated as non-identifying and ignored, which is what
                stops a shared support address firing on every case. Raising it re-admits
                those; lowering it hides a genuine ring that spans more cases than the
                limit. <code>0</code> disables the ceiling.
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
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
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [tab, setTab] = useState<"oversight" | "labelling" | "gate" | "rules">("oversight");

  const load = async () => {
    setLoading(true); setErr(null);
    try {
      const r = await apiFetch("/risk/scores/dashboard");
      if (r.status === 404) {
        // Two different 404s arrive here. HxSentinel answers 404 *by design*
        // when its release flag is off, so a dark module is indistinguishable
        // from an absent one — that one carries VEL-RISK-NOTFOUND-404. An
        // uncoded 404 is a routing mistake, and calling that "not enabled"
        // sends you hunting for a feature flag that was never the problem.
        const body = await r.json().catch(() => null);
        if (body?.error?.code === "VEL-RISK-NOTFOUND-404") { setOff(true); return; }
        throw new Error(body?.detail || "Risk endpoints did not resolve (routing error)");
      }
      if (!r.ok) throw await errorFromResponse(r, "Failed to load");
      setReviewers((await r.json()).reviewers || []);

      // A failed section must not render as an empty one — "No pending
      // proposals" is a claim about the data, and we only get to make it
      // when the request actually succeeded.
      const failed: string[] = [];
      const pr = await apiFetch("/risk/rules/proposals");
      if (pr.ok) setProposals((await pr.json()).proposals || []); else failed.push("proposals");
      const pk = await apiFetch("/risk/packs");
      if (pk.ok) setPacks((await pk.json()).packs || []); else failed.push("installed packs");
      const cat = await apiFetch("/risk/packs/catalogue");
      if (cat.ok) setCatalogue((await cat.json()).catalogue || []); else failed.push("pack catalogue");
      const md = await apiFetch("/risk/model");
      if (md.ok) setModels((await md.json()).models || []); else failed.push("models");
      if (failed.length) setErr(`Could not load: ${failed.join(", ")} — those sections are incomplete.`);
    } catch (e: any) { setErr(e?.message || "Failed to load"); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, []);

  /**
   * Run a mutation and report what actually happened.
   *
   * `apiFetch` resolves with the Response on 4xx/5xx — it never throws — so a
   * bare `await apiFetch(...)` inside try/catch discards every server error and
   * the button looks inert. Checking `ok` is mandatory, not defensive.
   *
   * `describe` reports success, including the *successful no-op*: these
   * endpoints return 200 `{created: []}` when there is nothing to propose, and
   * silence there is what makes a working feature look broken.
   *
   * Returns true when the call succeeded.
   */
  const run = async (
    key: string,
    call: () => Promise<Response>,
    fallback: string,
    describe?: (body: any) => string,
  ): Promise<boolean> => {
    setBusy(key); setErr(null); setNote(null);
    try {
      const r = await call();
      if (!r.ok) throw await errorFromResponse(r, fallback);
      const body = await r.json().catch(() => null);   // 204 has no body
      await load();
      if (describe) setNote(describe(body));
      return true;
    } catch (e: any) {
      setErr(e?.message || fallback);
      return false;
    } finally { setBusy(null); }
  };

  const trainModel = () =>
    run("train", () => apiFetch("/risk/model/train", { method: "POST" }),
      "Training failed",
      (b) => `Trained model v${b?.version ?? "?"}.`);

  const activateModel = (version: number) =>
    run(`m${version}`, () => apiFetch(`/risk/model/${version}/activate`, { method: "POST" }),
      "Activate failed", () => `Model v${version} is now active.`);

  const installOfficial = (packId: string) =>
    run(`cat-${packId}`, () => apiFetch(`/risk/packs/catalogue/${packId}/install`, { method: "POST" }),
      "Install failed", () => `Installed ${packId}.`);

  const installPack = async () => {
    let manifest: unknown;
    try { manifest = JSON.parse(manifestText); }
    catch { setErr("Manifest is not valid JSON."); return; }
    const ok = await run("install", () => apiFetch("/risk/packs/install", {
      method: "POST", body: JSON.stringify({ manifest }) }),
      "Install failed", (b) => `Installed ${b?.pack_id ?? "pack"}.`);
    if (ok) setManifestText("");
  };

  const uninstallPack = (packId: string) =>
    run(packId, () => apiFetch(`/risk/packs/${packId}`, { method: "DELETE" }),
      "Uninstall failed", () => `Removed ${packId}.`);

  const analyze = () =>
    run("analyze", () => apiFetch("/risk/rules/tuning/analyze", { method: "POST" }),
      "Analyze failed",
      (b) => (b?.created?.length
        ? `${b.created.length} proposal(s) created from reviewer scores.`
        : "Analyzed — nothing to propose. Score tuning reads reviewer scores on findings; none are recorded yet."));

  const mineNotes = () =>
    run("mine", () => apiFetch("/risk/rules/tuning/mine-notes", { method: "POST" }),
      "Notes mining failed",
      (b) => (b?.created?.length
        ? `${b.created.length} proposal(s) created from reviewer notes.`
        : "Mined — nothing to propose. Note mining reads reviewers' notes on findings; none are recorded yet."));

  const decide = (id: string, decision: "approved" | "rejected") =>
    run(id, () => apiFetch(`/risk/rules/proposals/${id}/decision`, {
      method: "POST", body: JSON.stringify({ decision }) }),
      "Decision failed", () => `Proposal ${decision}.`);

  if (loading) return <div style={{ padding: 24, color: "var(--text-muted)" }}>Loading…</div>;
  if (off) return (
    <div style={{ padding: 24, color: "var(--text-muted)", maxWidth: 640 }}>
      HxSentinel is not enabled on this deployment.
    </div>
  );

  return (
    <div style={{ padding: "var(--space-xl)", maxWidth: 1000 }}>
      <h2 style={{ margin: "0 0 10px" }}>Risk Review</h2>

      <div style={{ display: "flex", gap: 4, borderBottom: "1px solid var(--border-subtle)", marginBottom: 16 }}>
        {([
          ["oversight", "Oversight"],
          ["labelling", "Labelling"],
          ["gate", "Verify gate"],
          ["rules", "Rules"],
        ] as const).map(([k, lbl]) => (
          <button key={k} onClick={() => setTab(k)}
            style={{ fontSize: 13, padding: "6px 14px", border: "none", background: "transparent",
              cursor: "pointer", color: tab === k ? "var(--accent)" : "var(--text-muted)",
              fontWeight: tab === k ? 600 : 400,
              borderBottom: tab === k ? "2px solid var(--accent)" : "2px solid transparent" }}>
            {lbl}
          </button>
        ))}
      </div>

      {tab === "labelling" && <LabellingTab />}
      {tab === "gate" && <GateTab />}
      {tab === "rules" && <RulesTab />}

      {tab === "oversight" && (<>
      <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 16 }}>
        How each reviewer scores HxSentinel findings. Reliability is agreement with confirmed
        outcomes (disputed scores excluded). Flagged = low reliability or suspiciously fast scoring.
      </div>
      {err && <div style={{ color: "var(--status-failed)", fontSize: 13, marginBottom: 12 }}>{err}</div>}
      {note && (
        <div style={{ color: "var(--text-secondary)", fontSize: 13, marginBottom: 12,
          borderLeft: "2px solid var(--accent)", paddingLeft: 10 }}>{note}</div>
      )}
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
      </>)}
    </div>
  );
}
