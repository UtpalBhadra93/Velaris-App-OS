/**
 * Rep desk P5 — the floor: handling time, wrap-up mix, repeat contact, FCR, and
 * the tenant's wrap-up codes.
 *
 * **Every number is shown with its definition.** Each of these has at least two
 * readings in the industry, and a supervisor comparing this screen with last
 * year's spreadsheet needs to know which one this is. A number the platform does
 * not measure (verification failure rate) is named as not measured, not shown
 * as zero.
 *
 * Supervisor-only (`engage.analytics.read`); the code editor needs
 * `engage.config.write`. A 403 on either is said in words.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useEffect, useState } from "react";

import {
  engageAnalytics,
  engageDispositionCodes,
  engageSetDispositionCodes,
  type EngageAnalytics,
} from "@shared/api/client";
import {
  Button, Card, ErrorNote, MONO, SectionTitle, Select, SubPanel,
} from "@shared/components";

const muted: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)" };

const pct = (v: number | null) => (v == null ? "—" : `${(v * 100).toFixed(1)}%`);
const secs = (v: number | null) =>
  v == null ? "—" : v >= 60 ? `${Math.floor(v / 60)}m ${Math.round(v % 60)}s` : `${Math.round(v)}s`;

function Stat({ label, value, definition }: { label: string; value: string; definition?: string }) {
  return (
    <div title={definition} style={{ minWidth: 140 }}>
      <div style={{ ...muted, textTransform: "uppercase", letterSpacing: 0.4 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, color: "var(--text-primary)" }}>{value}</div>
    </div>
  );
}

function Codes() {
  const [text, setText] = useState("");
  const [freeText, setFreeText] = useState(true);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    engageDispositionCodes().then((r) => {
      setFreeText(r.free_text);
      setText((r.codes ?? []).map((c) => (c.label && c.label !== c.code ? `${c.code} = ${c.label}` : c.code)).join("\n"));
    }).catch((e) => setError(String(e)));
  }, []);

  const save = async (clear: boolean) => {
    setError(null); setMsg(null);
    try {
      const codes = clear ? null : text.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
        const [code, ...rest] = l.split("=");
        return { code: code.trim(), label: (rest.join("=").trim() || code.trim()) };
      });
      const r = await engageSetDispositionCodes(codes);
      setFreeText(r.free_text);
      if (clear) setText("");
      setMsg(r.free_text ? "Reps record wrap-up as free text." : `${r.codes?.length} codes saved.`);
    } catch (e) { setError(String(e)); }
  };

  return (
    <Card>
      <SectionTitle>Wrap-up codes</SectionTitle>
      <p style={{ ...muted, margin: "0 0 8px" }}>
        {freeText
          ? "None named — reps type their own. Name codes to make the wrap-up mix comparable."
          : "Reps choose from these; anything else is refused."}
        {" "}One per line: <code>code</code> or <code>code = Label</code>.
      </p>
      <textarea aria-label="Wrap-up codes" value={text} onChange={(e) => setText(e.target.value)}
                rows={6} style={{ width: "100%", boxSizing: "border-box", ...MONO, fontSize: 12 }} />
      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
        <Button size="sm" disabled={!text.trim()} onClick={() => save(false)}>Save codes</Button>
        {!freeText && <Button size="sm" variant="ghost" onClick={() => save(true)}>Back to free text</Button>}
      </div>
      {msg && <SubPanel><span style={{ fontSize: 12 }}>{msg}</span></SubPanel>}
      {error && <ErrorNote style={{ marginTop: 8 }}>{error}</ErrorNote>}
    </Card>
  );
}

export default function FloorAnalytics() {
  const [days, setDays] = useState(30);
  const [data, setData] = useState<EngageAnalytics | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setError(null);
    engageAnalytics(days).then(setData).catch((e) => {
      const s = String(e);
      setData(null);
      setError(/403/.test(s) ? "Floor analytics is a supervisor's view — engage.analytics.read." : s);
    });
  }, [days]);

  const d = data?.definitions ?? {};
  return (
    <div style={{ display: "grid", gap: "var(--space-md)" }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <span style={muted}>Window</span>
        <Select aria-label="Window" value={String(days)} onChange={(e) => setDays(Number(e.target.value))}>
          <option value="7">7 days</option>
          <option value="30">30 days</option>
          <option value="90">90 days</option>
        </Select>
      </div>
      {error && <ErrorNote>{error}</ErrorNote>}
      {data && (
        <>
          <Card>
            <div style={{ display: "flex", gap: "var(--space-lg)", flexWrap: "wrap" }}>
              <Stat label="Contacts" value={String(data.interactions)} />
              <Stat label="Avg handling" value={secs(data.aht_seconds)} definition={d.aht_seconds} />
              <Stat label="First-contact resolution" value={pct(data.first_contact_resolution)}
                    definition={d.first_contact_resolution} />
              <Stat label="Repeat contact" value={pct(data.repeat_contact_rate)}
                    definition={d.repeat_contact_rate} />
              <Stat label="Transferred" value={pct(data.transfer_rate)} definition={d.transfer_rate} />
              <Stat label="Unresolved caller" value={pct(data.unresolved_rate)} definition={d.unresolved_rate} />
              <Stat label="Abandoned" value={pct(data.abandoned_rate)} />
            </div>
            {data.truncated && (
              <SubPanel tone="warn">
                <span style={{ fontSize: 12 }}>Too many contacts in this window; the numbers cover the oldest 50,000. Narrow the window.</span>
              </SubPanel>
            )}
          </Card>

          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
                        gap: "var(--space-md)", alignItems: "start" }}>
            <Card>
              <SectionTitle>Wrap-up mix</SectionTitle>
              {Object.keys(data.wrap_up_mix).length === 0
                ? <p style={{ ...muted, margin: 0 }}>No wrapped contacts in this window.</p>
                : <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}><tbody>
                    {Object.entries(data.wrap_up_mix).map(([code, n]) => (
                      <tr key={code} style={{ borderTop: "1px solid var(--border-subtle)" }}>
                        <td style={{ padding: "4px 0", ...MONO }}>{code}</td>
                        <td style={{ padding: "4px 0", textAlign: "right" }}>{n}</td>
                      </tr>))}
                  </tbody></table>}
            </Card>
            <Card>
              <SectionTitle>By rep</SectionTitle>
              {data.per_agent.length === 0
                ? <p style={{ ...muted, margin: 0 }}>No contacts in this window.</p>
                : <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
                    <thead><tr style={{ ...muted, textAlign: "left" }}>
                      <th>Rep</th><th style={{ textAlign: "right" }}>Contacts</th><th style={{ textAlign: "right" }}>Avg handling</th>
                    </tr></thead>
                    <tbody>{data.per_agent.map((a) => (
                      <tr key={a.agent} style={{ borderTop: "1px solid var(--border-subtle)" }}>
                        <td style={{ padding: "4px 0", ...MONO, fontSize: 12 }}>{a.agent}</td>
                        <td style={{ padding: "4px 0", textAlign: "right" }}>{a.interactions}</td>
                        <td style={{ padding: "4px 0", textAlign: "right" }}>{secs(a.aht_seconds)}</td>
                      </tr>))}
                    </tbody></table>}
            </Card>
            <Codes />
          </div>

          <Card>
            <SectionTitle>What these numbers mean</SectionTitle>
            <dl style={{ margin: 0, display: "grid", gap: 6 }}>
              {Object.entries(d).map(([k, v]) => (
                <div key={k}><dt style={{ ...MONO, fontSize: 12 }}>{k}</dt>
                  <dd style={{ margin: 0, fontSize: 13, color: "var(--text-secondary)" }}>{v}</dd></div>
              ))}
            </dl>
          </Card>
        </>
      )}
    </div>
  );
}
