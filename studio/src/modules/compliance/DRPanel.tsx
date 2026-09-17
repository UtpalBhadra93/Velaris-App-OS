// HxDR (§8.9) — disaster recovery posture.
//
// The one thing this screen must never do is show green for a backup nobody
// has restored. `untested` gets its own colour, not a rounding-up to "ok".
//
// Copyright (c) 2024-2026 Velaris Contributors
// SPDX-License-Identifier: BSL-1.1
import React, { useEffect, useState } from "react";
import { apiJSON, postJSON, Banner, EmptyHint, Kpi, btn, card, h2, inp, mono, table, td, th, when } from "./shared";

type Region = {
  region: string;
  region_id: string;
  is_primary: boolean;
  state: "ok" | "untested" | "rpo_breached" | "no_backup";
  detail: string;
  last_backup_at: string | null;
  last_restore_test_at: string | null;
};

type Posture = {
  rpo_hours: number;
  rto_hours: number;
  restore_test_valid_days: number;
  regions: Region[];
  all_regions_ok: boolean;
  regions_at_risk: string[];
};

const STATE_STYLE: Record<Region["state"], { fg: string; bg: string; label: string }> = {
  ok:           { fg: "var(--status-completed)", bg: "rgba(13,148,136,0.15)", label: "OK" },
  untested:     { fg: "var(--status-running)", bg: "rgba(247,183,49,0.15)", label: "UNTESTED" },
  rpo_breached: { fg: "var(--status-failed)", bg: "rgba(220,38,38,0.15)", label: "RPO BREACHED" },
  no_backup:    { fg: "var(--status-failed)", bg: "rgba(220,38,38,0.15)", label: "NO BACKUP" },
};

export default function DRPanel() {
  const [posture, setPosture] = useState<Posture | null>(null);
  const [rpo, setRpo] = useState(24);
  const [rto, setRto] = useState(4);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  async function load() {
    setBusy("load"); setErr(null);
    try {
      setPosture(await apiJSON<Posture>(
        `/api/v1/compliance/dr/posture?rpo_hours=${rpo}&rto_hours=${rto}`,
      ));
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(null); }
  }

  useEffect(() => { load(); }, []);           // eslint-disable-line react-hooks/exhaustive-deps

  const regions = posture?.regions ?? [];
  const atRisk = posture?.regions_at_risk ?? [];

  return (
    <>
      <section style={card}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 10, flexWrap: "wrap" }}>
          <h2 style={{ ...h2, margin: 0 }}>Recovery posture</h2>
          <div style={{ marginLeft: "auto", display: "flex", gap: 6, alignItems: "center" }}>
            <label style={{ fontSize: 12, color: "var(--text-secondary)" }}>RPO</label>
            <input type="number" min={1} value={rpo}
              onChange={e => setRpo(Number(e.target.value) || 24)} style={{ ...inp, width: 64 }} />
            <label style={{ fontSize: 12, color: "var(--text-secondary)" }}>RTO</label>
            <input type="number" min={1} value={rto}
              onChange={e => setRto(Number(e.target.value) || 4)} style={{ ...inp, width: 64 }} />
            <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>hours</span>
            <button onClick={load} disabled={!!busy} style={btn(true)}>
              {busy === "load" ? "Loading…" : "Refresh"}
            </button>
          </div>
        </div>

        {err && <Banner tone="bad">⚠ {err}</Banner>}
        {note && <Banner tone="ok">{note}</Banner>}

        {posture && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
            <Kpi label="Regions" value={regions.length} />
            <Kpi label="At risk" value={atRisk.length} warn={atRisk.length > 0} />
            <Kpi label="RPO" value={`${posture.rpo_hours}h`} />
            <Kpi label="Drill valid for" value={`${posture.restore_test_valid_days}d`} />
          </div>
        )}

        {posture && regions.length > 0 && (
          posture.all_regions_ok
            ? <Banner tone="ok">✓ Every region has a recent, demonstrably restorable backup.</Banner>
            : <Banner tone="bad">✗ At risk: {atRisk.join(", ")}. A single green light would hide these behind the healthy regions.</Banner>
        )}
      </section>

      <section style={card}>
        <h2 style={h2}>Per region</h2>
        {regions.length === 0 ? (
          <EmptyHint>
            No regions are registered, so there is nothing to report a posture against. Register a
            region in HxGlobal first — a backup recorded without one is stored but never shown here.
          </EmptyHint>
        ) : (
          <table style={table}>
            <thead>
              <tr>
                <th style={th}>Region</th>
                <th style={th}>State</th>
                <th style={th}>Why</th>
                <th style={th}>Last backup</th>
                <th style={th}>Last restore drill</th>
                <th style={th}>Record drill</th>
              </tr>
            </thead>
            <tbody>
              {regions.map(r => {
                const s = STATE_STYLE[r.state];
                return (
                  <tr key={r.region_id}>
                    <td style={{ ...td, ...mono }}>
                      {r.region}{r.is_primary && <span style={{ color: "var(--accent-text)" }}> ★</span>}
                    </td>
                    <td style={td}>
                      <span style={{ background: s.bg, color: s.fg, padding: "2px 6px",
                                     borderRadius: 3, fontSize: 11, fontWeight: 600 }}>
                        {s.label}
                      </span>
                    </td>
                    <td style={{ ...td, color: "var(--text-secondary)" }}>{r.detail}</td>
                    <td style={td}>{when(r.last_backup_at)}</td>
                    <td style={td}>{when(r.last_restore_test_at)}</td>
                    <td style={td}><DrillButtons region={r} onDone={m => { setNote(m); load(); }} onError={setErr} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 10 }}>
          Backups are taken by <code style={mono}>update-velaris.sh</code> and recorded from there —
          this platform is a registry, not an executor. A fresh backup nobody has restored reports
          as <strong>untested</strong>, never as healthy.
        </div>
      </section>
    </>
  );
}

/** A drill is recorded against the region's newest backup, which is the one
 *  the posture above is judging. A failed drill is recorded too — knowing a
 *  backup does NOT restore is worth more than any number of untested ones. */
function DrillButtons({ region, onDone, onError }: {
  region: Region;
  onDone: (msg: string) => void;
  onError: (msg: string) => void;
}) {
  const [busy, setBusy] = useState(false);

  async function record(result: "passed" | "failed") {
    setBusy(true);
    try {
      const backups = await apiJSON<{ backups: { id: string }[] }>(
        `/api/v1/compliance/dr/backups?region_id=${region.region_id}&limit=1`,
      );
      const newest = backups.backups?.[0];
      if (!newest) { onError(`${region.region} has no recorded backup to drill.`); return; }
      await postJSON(`/api/v1/compliance/dr/backups/${newest.id}/restore-test`, {
        result, notes: `Recorded from Studio for ${region.region}`,
      });
      onDone(`Restore drill recorded as ${result} for ${region.region}.`);
    } catch (e: any) { onError(e.message); }
    finally { setBusy(false); }
  }

  if (region.state === "no_backup") return <span style={{ color: "var(--text-muted)" }}>—</span>;
  return (
    <>
      <button onClick={() => record("passed")} disabled={busy} style={btn()}>Passed</button>{" "}
      <button onClick={() => record("failed")} disabled={busy} style={btn()}>Failed</button>
    </>
  );
}
