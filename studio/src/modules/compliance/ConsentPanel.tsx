// HxConsent (§8.1) — consent & purpose-limitation ledger.
//
// The screen answers the question a regulator asks: on what basis was this
// subject's data processed, and when did that basis arise. It shows the
// derived current state AND the ledger it came from, because the ledger is
// the evidence and the state is only a summary of it.
//
// Copyright (c) 2024-2026 Velaris Contributors
// SPDX-License-Identifier: BSL-1.1
import React, { useState } from "react";
import { apiJSON, postJSON, Banner, EmptyHint, btn, card, h2, inp, mono, table, td, th, when } from "./shared";

const PURPOSES = [
  "case_processing", "marketing", "recording", "biometric",
  "voice_processing", "ai_processing", "data_sharing",
];

const BASES = [
  "consent", "contract", "legal_obligation",
  "legitimate_interest", "vital_interests", "public_task",
];

/** Only `consent` can be withdrawn by the subject — the API refuses the rest,
 *  so the form must not offer a combination it knows will 400. */
const WITHDRAWABLE = new Set(["consent"]);

type Purpose = { purpose: string; granted: boolean; basis: string; source: string; at: string | null };
type Entry = { id: string; purpose: string; action: string; basis: string; source: string; recorded_at: string | null };
type State = { subject_ref: string; purposes: Record<string, Purpose>; ledger: Entry[] };

export default function ConsentPanel() {
  const [subject, setSubject] = useState("");
  const [state, setState] = useState<State | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const [purpose, setPurpose] = useState(PURPOSES[0]);
  const [basis, setBasis] = useState(BASES[0]);
  const [action, setAction] = useState("granted");

  const withdrawBlocked = action === "withdrawn" && !WITHDRAWABLE.has(basis);

  async function load(ref?: string) {
    const s = (ref ?? subject).trim();
    if (!s) return;
    setBusy("load"); setErr(null); setNote(null);
    try {
      setState(await apiJSON<State>(`/api/v1/compliance/consent/${encodeURIComponent(s)}`));
    } catch (e: any) { setErr(e.message); setState(null); }
    finally { setBusy(null); }
  }

  async function record() {
    const s = subject.trim();
    if (!s) return;
    setBusy("record"); setErr(null); setNote(null);
    try {
      await postJSON("/api/v1/compliance/consent", {
        subject_ref: s, purpose, basis, action, source: "studio",
      });
      setNote(`Recorded ${action} / ${purpose} on a ${basis} basis.`);
      await load(s);
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(null); }
  }

  const purposes = state ? Object.values(state.purposes) : [];

  return (
    <>
      <section style={card}>
        <h2 style={h2}>Subject lookup</h2>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <input
            value={subject}
            onChange={e => setSubject(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") load(); }}
            placeholder="Email or subject reference"
            style={{ ...inp, width: 340, ...mono }}
          />
          <button onClick={() => load()} disabled={!!busy || !subject.trim()} style={btn(true)}>
            {busy === "load" ? "Loading…" : "Look up"}
          </button>
        </div>
        <div style={{ fontSize: 11, color: "#888", marginTop: 8 }}>
          Subjects match case-insensitively. A subject with no record is not a refusal —
          it means no basis was ever recorded, and processing is treated as unlawful.
        </div>
        {err && <Banner tone="bad">⚠ {err}</Banner>}
        {note && <Banner tone="ok">{note}</Banner>}
      </section>

      {state && (
        <section style={card}>
          <h2 style={h2}>Current basis per purpose</h2>
          {purposes.length === 0 ? (
            <EmptyHint>
              Nothing recorded for <code style={mono}>{state.subject_ref}</code>. Every purpose therefore
              checks as <strong>not lawful</strong> — absence of a basis is not permission.
            </EmptyHint>
          ) : (
            <table style={table}>
              <thead>
                <tr>
                  <th style={th}>Purpose</th>
                  <th style={th}>Lawful</th>
                  <th style={th}>Basis</th>
                  <th style={th}>Source</th>
                  <th style={th}>Since</th>
                </tr>
              </thead>
              <tbody>
                {purposes.map(p => (
                  <tr key={p.purpose}>
                    <td style={{ ...td, ...mono }}>{p.purpose}</td>
                    <td style={td}>
                      {p.granted
                        ? <span style={{ color: "#2a7" }}>✓ yes</span>
                        : <span style={{ color: "#c33" }}>✗ no</span>}
                    </td>
                    <td style={td}>{p.basis}</td>
                    <td style={td}>{p.source}</td>
                    <td style={td}>{when(p.at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}

      <section style={card}>
        <h2 style={h2}>Record a decision</h2>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <select value={action} onChange={e => setAction(e.target.value)} style={inp}>
            <option value="granted">granted</option>
            <option value="withdrawn">withdrawn</option>
          </select>
          <select value={purpose} onChange={e => setPurpose(e.target.value)} style={inp}>
            {PURPOSES.map(p => <option key={p} value={p}>{p}</option>)}
          </select>
          <select value={basis} onChange={e => setBasis(e.target.value)} style={inp}>
            {BASES.map(b => <option key={b} value={b}>{b}</option>)}
          </select>
          <button
            onClick={record}
            disabled={!!busy || !subject.trim() || withdrawBlocked}
            style={btn(true)}
          >
            {busy === "record" ? "Recording…" : "Append to ledger"}
          </button>
        </div>

        {withdrawBlocked && (
          <Banner tone="warn">
            A <strong>{basis}</strong> basis cannot be withdrawn by the subject — only consent can.
            Recording it would claim processing stopped when it lawfully continues.
          </Banner>
        )}
        {purpose === "biometric" && (
          <Banner tone="info">
            Biometric data is Article 9 special category — it needs <strong>explicit</strong> consent
            and is never implied by a broader grant.
          </Banner>
        )}
        <div style={{ fontSize: 11, color: "#888", marginTop: 10 }}>
          The ledger is append-only. A withdrawal adds a row; it never edits the grant it supersedes.
        </div>
      </section>

      {state && (
        <section style={card}>
          <h2 style={h2}>Ledger — the Article 15 answer</h2>
          <table style={table}>
            <thead>
              <tr>
                <th style={th}>When</th>
                <th style={th}>Action</th>
                <th style={th}>Purpose</th>
                <th style={th}>Basis</th>
                <th style={th}>Source</th>
              </tr>
            </thead>
            <tbody>
              {state.ledger.length === 0 && (
                <tr><td colSpan={5} style={{ ...td, color: "#888" }}>No entries.</td></tr>
              )}
              {state.ledger.map(e => (
                <tr key={e.id}>
                  <td style={td}>{when(e.recorded_at)}</td>
                  <td style={td}>
                    {e.action === "granted"
                      ? <span style={{ color: "#2a7" }}>granted</span>
                      : <span style={{ color: "#c33" }}>withdrawn</span>}
                  </td>
                  <td style={{ ...td, ...mono }}>{e.purpose}</td>
                  <td style={td}>{e.basis}</td>
                  <td style={td}>{e.source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </>
  );
}
