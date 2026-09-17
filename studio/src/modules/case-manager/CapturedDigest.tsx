/**
 * F-06 — the captured digest: confirm-or-edit, never a form.
 *
 * A meeting, an email or a dictated note has been read and has PROPOSED
 * changes to this case. Nothing here has happened yet. Three things this screen
 * has to make obvious, because each is invisible in the API alone:
 *
 *   1. **The before, next to the after.** A reviewer deciding whether 4,300 is
 *      right needs to see what the case says today, and re-reading it at
 *      decision time would show a value that may have moved since — so the
 *      proposal carries the before it was made against.
 *   2. **Where it came from.** The snippet is the evidence. A proposal a
 *      reviewer cannot trace back to a sentence is one they can only guess at,
 *      and guessing is how a digest gets accepted in bulk.
 *   3. **What capture is allowed to touch.** The whitelist is published, so a
 *      reviewer can see for themselves that authorisation and deployment
 *      surfaces are not on it — and are not on it by construction.
 *
 * Accepting writes as YOU, through the same grant-checked path your own edit
 * takes. If the write is refused, the proposal stays pending and says so: it
 * must never read as applied when it was not.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useState } from "react";

import {
  decideCaptureProposal,
  getCaptureDigest,
  getCaptureTargets,
  runCapture,
  type CaptureDigest,
  type CaptureProposal,
  type CaptureTargets,
} from "@shared/api/client";
import { Card, ErrorNote, MONO } from "@shared/components";

/** Why a run produced nothing, in the words an operator can act on. */
const REFUSAL: Record<string, string> = {
  capture_disabled: "Capture is switched off for this tenant.",
  no_lawful_basis: "No lawful basis for AI processing on this case.",
  no_analysed_transcript:
    "That meeting has no completed HxMeet analysis. Capture reads the transcript HxMeet produced under its own consent gate — it never opens a recording itself.",
  not_on_this_case: "That source does not belong to this case.",
  empty_source: "There was nothing to read.",
  no_defined_variables: "This case type defines no variables capture may propose values for.",
  unknown_source_kind: "That is not a source capture knows how to read.",
  missing_source_ref: "That source needs an id.",
  transcript_unreadable: "The transcript could not be read.",
  tenant_unresolved: "This case's tenant could not be resolved.",
  no_such_case: "No such case.",
};

function render(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

export default function CapturedDigest({ caseId }: { caseId: string }) {
  const [digest, setDigest] = useState<CaptureDigest | null>(null);
  const [targets, setTargets] = useState<CaptureTargets | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [showDecided, setShowDecided] = useState(false);
  const [showTargets, setShowTargets] = useState(false);
  const [note, setNote] = useState("");
  const [sourceRef, setSourceRef] = useState("");
  const [sourceKind, setSourceKind] = useState("voice_note");

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [d, t] = await Promise.all([
        getCaptureDigest(caseId, showDecided),
        getCaptureTargets(caseId),
      ]);
      setDigest(d); setTargets(t);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [caseId, showDecided]);

  useEffect(() => { void load(); }, [load]);

  async function act<T>(key: string, fn: () => Promise<T>,
                        message?: string) {
    setBusy(key); setError(null); setNotice(null);
    try {
      await fn();
      if (message) setNotice(message);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function capture() {
    await act("run", async () => {
      const out = await runCapture(caseId, {
        source_kind: sourceKind,
        source_ref: sourceKind === "voice_note" ? null : sourceRef.trim() || null,
        text: sourceKind === "voice_note" ? note : null,
      });
      // A refusal is a 201 with a reason — the gate working, not a fault. It is
      // shown in the gate's own words rather than as an error banner.
      if (out.status === "refused" && out.refusal_reason) {
        setNotice(REFUSAL[out.refusal_reason] ?? out.refusal_reason);
      } else if (out.status === "failed") {
        setNotice(`The model could not be read: ${out.error ?? "unknown"}`);
      } else {
        setNote("");
        setNotice(
          out.proposed === 0
            ? "Read, and it proposed nothing. That is a valid answer."
            : `${out.proposed} proposal${out.proposed === 1 ? "" : "s"} to review`
              + (out.dropped ? `, ${out.dropped} outside what capture may touch` : "")
              + (out.attempts > 1 ? " (took a second attempt)" : ""));
      }
    });
  }

  const pending = (digest?.proposals ?? []).filter(p => p.status === "pending");
  const decided = (digest?.proposals ?? []).filter(p => p.status !== "pending");

  if (loading) return <div style={{ padding: 20 }}>Loading…</div>;

  return (
    <div style={{ padding: 20, display: "flex", flexDirection: "column", gap: 16 }}>
      {digest && !digest.enabled && (
        <div style={{ padding: 12, borderRadius: 6, fontSize: 13,
                      background: "var(--bg-elevated)",
                      color: "var(--text-muted)" }}>
          <strong style={{ color: "var(--text-primary)" }}>
            Ambient capture is off for this tenant.
          </strong>{" "}
          Nothing is read and nothing is proposed. An administrator switches it
          on per tenant.
        </div>
      )}

      {notice && (
        <Card style={{ borderLeft: "3px solid var(--accent)" }}>
          <span style={{ fontSize: 13, color: "var(--text-primary)" }}>{notice}</span>
        </Card>
      )}
      {error && <ErrorNote>{error}</ErrorNote>}

      {/* ── capture from a source ─────────────────────────────────────── */}
      <section style={{ border: "1px solid var(--border-subtle)",
                        borderRadius: 6, padding: 12 }}>
        <h3 style={{ margin: "0 0 4px", fontSize: 14 }}>Read a source</h3>
        <p style={{ margin: "0 0 10px", fontSize: 12,
                    color: "var(--text-muted)" }}>
          Nothing is applied. Whatever is found comes back as proposals for you
          to confirm or edit.
        </p>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap",
                      alignItems: "flex-end" }}>
          <label style={{ fontSize: 12 }}>
            <div style={{ color: "var(--text-muted)" }}>Source</div>
            <select value={sourceKind} onChange={e => setSourceKind(e.target.value)}
                    style={{ padding: 6 }}>
              <option value="voice_note">A note I dictate or type</option>
              <option value="email">An email on this case</option>
              <option value="meeting">A meeting HxMeet has analysed</option>
            </select>
          </label>
          {sourceKind === "voice_note" ? (
            <label style={{ fontSize: 12, flex: 1, minWidth: 260 }}>
              <div style={{ color: "var(--text-muted)" }}>What was said</div>
              <input value={note} onChange={e => setNote(e.target.value)}
                     placeholder="The customer agreed 4,300 and will call back Tuesday"
                     style={{ padding: 6, width: "100%" }} />
            </label>
          ) : (
            <label style={{ fontSize: 12, flex: 1, minWidth: 260 }}>
              <div style={{ color: "var(--text-muted)" }}>
                {sourceKind === "email" ? "Message id" : "Session id"}
              </div>
              <input value={sourceRef} onChange={e => setSourceRef(e.target.value)}
                     placeholder="uuid" style={{ ...MONO, padding: 6, width: "100%" }} />
            </label>
          )}
          <button disabled={busy !== null || !digest?.enabled
                            || (sourceKind === "voice_note" ? !note.trim() : !sourceRef.trim())}
                  onClick={() => void capture()}
                  style={{ padding: "8px 16px", cursor: "pointer" }}>
            {busy === "run" ? "Reading…" : "Read it"}
          </button>
        </div>
      </section>

      {/* ── the digest ────────────────────────────────────────────────── */}
      <section>
        <div style={{ display: "flex", justifyContent: "space-between",
                      alignItems: "center", marginBottom: 8 }}>
          <h3 style={{ margin: 0, fontSize: 14 }}>
            Waiting on you ({pending.length})
          </h3>
          <label style={{ fontSize: 12, color: "var(--text-muted)" }}>
            <input type="checkbox" checked={showDecided}
                   onChange={e => setShowDecided(e.target.checked)} />{" "}
            show what has already been decided
          </label>
        </div>

        {pending.length === 0 ? (
          <p style={{ fontSize: 13, color: "var(--text-muted)" }}>
            Nothing to confirm. Read a source above, or wait for one to be read.
          </p>
        ) : pending.map(p => (
          <ProposalCard
            key={p.id} proposal={p} busy={busy === p.id}
            edit={edits[p.id] ?? ""}
            onEditChange={v => setEdits({ ...edits, [p.id]: v })}
            onDecide={(decision, value) => act(
              p.id,
              () => decideCaptureProposal(p.id, decision, value),
              decision === "reject" ? "Rejected. Nothing was written."
                : "Applied — as you, through your own permissions.")}
          />
        ))}

        {showDecided && decided.length > 0 && (
          <>
            <h4 style={{ margin: "16px 0 8px", fontSize: 13,
                         color: "var(--text-muted)" }}>
              Already decided
            </h4>
            {decided.map(p => (
              <div key={p.id} style={{ fontSize: 12, padding: "6px 0",
                                       borderTop: "1px solid var(--border-subtle)" }}>
                <span style={MONO}>{p.target ?? p.kind}</span>{" "}
                <strong>{p.status}</strong>
                {p.status === "superseded"
                  ? " — a later read of the same source replaced it; nobody looked at this one"
                  : ` by ${p.decided_by_name ?? p.decided_by ?? "—"}`}
                {p.status === "edited" && (
                  <> — proposed {render(p.proposed_value)},
                     applied {render(p.applied_value)}</>
                )}
              </div>
            ))}
          </>
        )}
      </section>

      {/* ── what capture may ever propose ─────────────────────────────── */}
      <section>
        <button onClick={() => setShowTargets(v => !v)}
                style={{ fontSize: 12, background: "none", cursor: "pointer",
                         border: "none", color: "var(--text-muted)",
                         padding: 0 }}>
          {showTargets ? "▾" : "▸"} What capture is allowed to change
          {targets ? ` (${targets.targets.length})` : ""}
        </button>
        {showTargets && targets && (
          <div style={{ marginTop: 8, fontSize: 12,
                        color: "var(--text-muted)" }}>
            <p style={{ margin: "0 0 8px" }}>
              This case type's own variables, and nothing else. A suggestion
              naming anything outside this list is discarded before it reaches
              you — including one planted in the text being read.
            </p>
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {targets.targets.map(t => (
                <li key={t.key}>
                  <span style={MONO}>{t.key}</span> — {t.label} ({t.type})
                </li>
              ))}
              {targets.targets.length === 0 && (
                <li>none — this case type defines no variables</li>
              )}
            </ul>
            <p style={{ margin: "8px 0 0" }}>
              Never reachable: <span style={MONO}>
                {targets.blocked_namespaces.join(", ")}
              </span>.
            </p>
          </div>
        )}
      </section>

      {/* ── the runs, for when a digest is empty and nobody knows why ── */}
      {digest && digest.runs.length > 0 && (
        <section style={{ fontSize: 12, color: "var(--text-muted)" }}>
          <h4 style={{ margin: "0 0 6px", fontSize: 13 }}>Reads</h4>
          {digest.runs.slice(0, 8).map(r => (
            <div key={r.id} style={{ padding: "4px 0",
                                     borderTop: "1px solid var(--border-subtle)" }}>
              <span style={MONO}>{r.source.kind}</span> — {r.status}
              {r.refusal_reason && ` — ${REFUSAL[r.refusal_reason] ?? r.refusal_reason}`}
              {r.status === "complete" && ` — ${r.proposed} proposed`}
              {r.dropped > 0 && `, ${r.dropped} discarded as out of scope`}
              {r.model && ` · ${r.provider}/${r.model}`}
              {r.attempts > 1 && ` · ${r.attempts} attempts`}
            </div>
          ))}
        </section>
      )}
    </div>
  );
}

function ProposalCard({ proposal, busy, edit, onEditChange, onDecide }: {
  proposal: CaptureProposal;
  busy: boolean;
  edit: string;
  onEditChange: (v: string) => void;
  onDecide: (decision: "accept" | "edit" | "reject", value?: unknown) => void;
}) {
  const p = proposal;
  return (
    <div style={{ border: "1px solid var(--border-subtle)",
                  borderRadius: 6, padding: 12, marginBottom: 10 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "baseline",
                    flexWrap: "wrap" }}>
        <strong style={MONO}>{p.target ?? "note"}</strong>
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
          {p.kind === "note" ? "a note for the timeline" : "a variable change"}
        </span>
        {/* NULL is not 0%: "the model reported no confidence" and "the model is
            not confident" are opposite claims. */}
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
          {p.confidence === null
            ? "no confidence reported"
            : `confidence ${(p.confidence * 100).toFixed(0)}%`}
        </span>
      </div>

      {p.kind === "variable_update" && (
        <div style={{ margin: "8px 0", fontSize: 13 }}>
          <span style={{ color: "var(--text-muted)" }}>now</span>{" "}
          <span style={MONO}>{render(p.current_value)}</span>
          {"  →  "}
          <span style={{ ...MONO, fontWeight: 600 }}>{render(p.proposed_value)}</span>
        </div>
      )}
      {p.kind === "note" && (
        <p style={{ margin: "8px 0", fontSize: 13 }}>{render(p.proposed_value)}</p>
      )}

      {p.snippet && (
        <blockquote style={{ margin: "8px 0", padding: "6px 10px", fontSize: 12,
                             borderLeft: "3px solid var(--border-subtle)",
                             color: "var(--text-muted)" }}>
          “{p.snippet}”
        </blockquote>
      )}

      <div style={{ display: "flex", gap: 6, flexWrap: "wrap",
                    alignItems: "center" }}>
        <button disabled={busy} onClick={() => onDecide("accept")}
                style={{ padding: "6px 12px", cursor: "pointer" }}>
          Accept
        </button>
        <input value={edit} onChange={e => onEditChange(e.target.value)}
               placeholder="or use this value instead"
               style={{ padding: 6, width: 220 }} />
        <button disabled={busy || !edit.trim()}
                onClick={() => onDecide("edit", coerce(edit))}
                style={{ padding: "6px 12px", cursor: "pointer" }}>
          Apply my value
        </button>
        <button disabled={busy} onClick={() => onDecide("reject")}
                style={{ padding: "6px 12px", cursor: "pointer" }}>
          Reject
        </button>
      </div>
    </div>
  );
}

/** A typed value from a text box, without pretending a word is a number.
 *
 *  `"4300"` becomes 4300 so a numeric variable stays numeric; `"4,300 GBP"`
 *  stays the string somebody typed. Guessing harder than this would be worse
 *  than not guessing: a silent coercion is exactly the class of edit nobody
 *  can see afterwards.
 */
function coerce(text: string): unknown {
  const t = text.trim();
  if (t === "true") return true;
  if (t === "false") return false;
  if (t !== "" && !Number.isNaN(Number(t))) return Number(t);
  return t;
}
