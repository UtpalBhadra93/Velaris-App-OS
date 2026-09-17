/**
 * Rep desk P6 — the two AI-era panels beside the 360: suggested next actions and
 * a grounded recap. Plus the wrap-up picker P5 needs.
 *
 * What each is careful about:
 *
 *  * **Next actions show their WHY and can be disagreed with on the record.** A
 *    suggestion a rep cannot argue with is an instruction. "Not useful" asks for
 *    a reason, because a rejection with no reason tunes nothing.
 *  * **The recap shows its sources beside every point.** A point the server
 *    could not tie to a row was already dropped; the screen says how many, so a
 *    short recap is not mistaken for a quiet customer.
 *  * **Wrap-up offers the tenant's codes when it has named some**, and free text
 *    when it has not — the server enforces the same rule.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useState } from "react";

import {
  engageDispositionCodes,
  engageNextActionFeedback,
  engageNextActions,
  engageRecap,
  type DispositionCode,
  type NextAction,
  type RecapPoint,
} from "@shared/api/client";
import {
  Button, Card, ErrorNote, MONO, Pill, SectionTitle, Select, SubPanel, TextInput,
} from "@shared/components";

const muted: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)" };

export function NextActions({ customerId, interactionId, tier }: {
  customerId: string; interactionId: string; tier: string;
}) {
  const [actions, setActions] = useState<NextAction[] | null>(null);
  const [decided, setDecided] = useState<Record<string, string>>({});
  const [rejecting, setRejecting] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setActions((await engageNextActions(customerId, interactionId)).actions);
    } catch (e) { setError(String(e)); }
  }, [customerId, interactionId]);
  // The tier is a dependency on purpose: verifying the caller opens rules that
  // were withheld a moment ago.
  useEffect(() => { void load(); }, [load, tier]);

  const decide = async (key: string, verdict: "accepted" | "rejected", why?: string) => {
    setError(null);
    try {
      await engageNextActionFeedback(customerId, {
        action_key: key, verdict, reason: why ?? null, interaction_id: interactionId });
      setDecided((d) => ({ ...d, [key]: verdict }));
      setRejecting(null); setReason("");
    } catch (e) { setError(String(e)); }
  };

  return (
    <Card>
      <SectionTitle>Suggested next</SectionTitle>
      {actions === null && !error && <p style={{ ...muted, margin: 0 }}>Loading…</p>}
      {actions?.length === 0 && (
        <p style={{ ...muted, margin: 0 }}>Nothing to suggest from what you can see on this call.</p>
      )}
      <div style={{ display: "grid", gap: 10 }}>
        {actions?.map((a) => (
          <div key={a.key} style={{ paddingBottom: 10, borderBottom: "1px solid var(--border-subtle)" }}>
            <div style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
              <strong style={{ fontSize: 13, color: "var(--text-primary)" }}>{a.title}</strong>
              {decided[a.key] && (
                <Pill tone={decided[a.key] === "accepted" ? "ok" : "muted"}>
                  {decided[a.key] === "accepted" ? "done" : "not useful"}
                </Pill>
              )}
            </div>
            <p style={{ fontSize: 13, color: "var(--text-secondary)", margin: "4px 0 0", lineHeight: 1.5 }}>
              {a.why}
            </p>
            {!decided[a.key] && (
              rejecting === a.key ? (
                <div style={{ display: "flex", gap: 8, marginTop: 6, flexWrap: "wrap" }}>
                  <TextInput value={reason} onChange={(e) => setReason(e.target.value)}
                             placeholder="Why it does not apply" style={{ width: 240 }} autoFocus />
                  <Button size="sm" disabled={reason.trim().length < 3}
                          onClick={() => decide(a.key, "rejected", reason)}>Record</Button>
                  <Button size="sm" variant="ghost"
                          onClick={() => { setRejecting(null); setReason(""); }}>Cancel</Button>
                </div>
              ) : (
                <div style={{ display: "flex", gap: 8, marginTop: 6 }}>
                  <Button size="sm" variant="secondary" onClick={() => decide(a.key, "accepted")}>Done</Button>
                  <Button size="sm" variant="ghost" onClick={() => setRejecting(a.key)}>Not useful</Button>
                </div>
              )
            )}
          </div>
        ))}
      </div>
      {error && <ErrorNote style={{ marginTop: "var(--space-sm)" }}>{error}</ErrorNote>}
    </Card>
  );
}

export function Recap({ customerId, interactionId, tier }: {
  customerId: string; interactionId: string; tier: string;
}) {
  const [points, setPoints] = useState<RecapPoint[] | null>(null);
  const [meta, setMeta] = useState<{ withheld: string[]; dropped: number; note?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const verified = tier === "partial" || tier === "full";

  const run = async () => {
    setBusy(true); setError(null);
    try {
      const out = await engageRecap(customerId, interactionId);
      setPoints(out.points);
      setMeta({ withheld: out.withheld, dropped: out.dropped, note: out.note });
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  return (
    <Card>
      <SectionTitle>Recap</SectionTitle>
      {!verified ? (
        <p style={{ ...muted, margin: 0 }}>Verify the caller to get a recap of their history.</p>
      ) : (
        <Button size="sm" disabled={busy} onClick={run}>
          {busy ? "Summarising…" : points ? "Refresh recap" : "Recap their history"}
        </Button>
      )}
      {points && points.length > 0 && (
        <ul style={{ margin: "10px 0 0", paddingLeft: 18, display: "grid", gap: 6 }}>
          {points.map((p, i) => (
            <li key={i} style={{ fontSize: 13, color: "var(--text-primary)", lineHeight: 1.5 }}>
              {p.text}{" "}
              {p.sources.map((s) => (
                <span key={s.sid} title={`${s.kind} ${s.ref}`}
                      style={{ ...MONO, fontSize: 11, color: "var(--text-muted)", marginLeft: 4 }}>
                  [{s.kind}]
                </span>
              ))}
            </li>
          ))}
        </ul>
      )}
      {points && points.length === 0 && (
        <p style={{ ...muted, margin: "10px 0 0" }}>{meta?.note ?? "No point could be tied to a record."}</p>
      )}
      {meta && (meta.dropped > 0 || meta.withheld.length > 0) && (
        <SubPanel>
          <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>
            {meta.dropped > 0 && `${meta.dropped} point${meta.dropped === 1 ? "" : "s"} dropped for citing no record. `}
            {meta.withheld.length > 0 && `Not used: ${meta.withheld.join("; ")}.`}
          </span>
        </SubPanel>
      )}
      {error && <ErrorNote style={{ marginTop: "var(--space-sm)" }}>{error}</ErrorNote>}
    </Card>
  );
}

/** The wrap-up control: the tenant's codes if it named some, free text if not. */
export function WrapUpPicker({ busy, onWrap }: {
  busy: boolean; onWrap: (code: string) => void;
}) {
  const [codes, setCodes] = useState<DispositionCode[] | null | undefined>(undefined);
  const [value, setValue] = useState("");

  useEffect(() => {
    engageDispositionCodes().then((r) => setCodes(r.codes)).catch(() => setCodes(null));
  }, []);

  return (
    <div style={{ display: "flex", gap: "var(--space-sm)", alignItems: "center", flexWrap: "wrap" }}>
      {codes ? (
        <Select aria-label="Wrap-up code" value={value} onChange={(e) => setValue(e.target.value)}>
          <option value="">Wrap-up code…</option>
          {codes.map((c) => <option key={c.code} value={c.code}>{c.label}</option>)}
        </Select>
      ) : (
        <TextInput aria-label="Wrap-up code" value={value} onChange={(e) => setValue(e.target.value)}
                   placeholder="Wrap-up code, e.g. resolved" style={{ width: 200 }} />
      )}
      <Button variant="secondary" disabled={busy || !value.trim()} onClick={() => onWrap(value.trim())}>
        Wrap up
      </Button>
    </div>
  );
}
