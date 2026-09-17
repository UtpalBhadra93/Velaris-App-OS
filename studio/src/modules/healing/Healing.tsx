/**
 * Self-healing (F-08) — the approval card.
 *
 * One human approval is the whole design, and this screen is where it happens.
 * That puts a burden on it: **approving here APPLIES the change to live
 * config**, so the card must show what was checked rather than a verdict.
 *
 * Four things it is careful about:
 *
 *   1. **Every rung is listed, the ones that passed included.** A reviewer
 *      approving a change to their own live process is entitled to see what was
 *      checked, not a single green tick.
 *   2. **The button says what it does.** "Approve and apply" — because it does
 *      both, and a button labelled "Approve" would imply a later step that does
 *      not exist.
 *   3. **`refused` and `rejected` are shown differently.** The ladder declining
 *      to put something in front of anybody is not a person saying no.
 *   4. **Deployed is not healed.** A `watching` episode says so, with the
 *      window; only a closed window earns the word.
 *
 * Layout follows the platform shell (`/help`, `/work-center`): the app header
 * comes from `AppLayout`, standing facts sit in a `Banner` above the bar, the
 * `Tabs` bar is flush under it, and the body is the only thing that scrolls.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useState } from "react";

import {
  approveHealingEpisode,
  getHealingStatus,
  listHealingEpisodes,
  rejectHealingEpisode,
  rollbackHealingEpisode,
  verifyHealingChain,
  type HealingEpisode,
  type HealingStatus,
} from "@shared/api/client";
import {
  Banner, Button, Card, EmptyState, ErrorNote, MONO, Pill, SectionTitle,
  Spinner, SubPanel, Tabs, TextInput, type PillTone,
} from "@shared/components";

const TONE: Record<string, PillTone> = {
  ready: "warn", watching: "warn", healed: "ok", applied: "ok",
  refused: "muted", rejected: "muted", rolled_back: "bad", failed: "bad",
  pending: "muted",
};

const muted: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)" };

/** Every rung, passes included. A single green tick is not evidence. */
function Ladder({ episode }: { episode: HealingEpisode }) {
  const rungs = episode.ladder ?? [];
  if (!rungs.length) return null;
  return (
    <div style={{ marginTop: "var(--space-md)" }}>
      <div style={{ ...muted, fontFamily: "var(--font-mono)", textTransform: "uppercase",
                    letterSpacing: "0.06em", fontSize: 11, marginBottom: 8 }}>
        What was checked
      </div>
      <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: 8 }}>
        {rungs.map((r) => (
          <li key={r.rung} style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
            <Pill tone={r.ok ? (r.applicable === false ? "muted" : "ok") : "bad"}>
              {r.ok ? (r.applicable === false ? "n/a" : "pass") : "stopped"}
            </Pill>
            <div style={{ minWidth: 0 }}>
              <div style={{ ...MONO, color: "var(--text-primary)" }}>{r.rung}</div>
              <div style={{ ...muted, lineHeight: 1.5 }}>{r.detail}</div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** The diagnosis, as rows. `JSON.stringify` of a dict is not a reading of it. */
function Diagnosis({ value }: { value: unknown }) {
  const entries =
    value && typeof value === "object" && !Array.isArray(value)
      ? Object.entries(value as Record<string, unknown>)
      : null;
  return (
    <div style={{ marginTop: "var(--space-md)" }}>
      <div style={{ ...muted, fontFamily: "var(--font-mono)", textTransform: "uppercase",
                    letterSpacing: "0.06em", fontSize: 11, marginBottom: 6 }}>
        Diagnosed
      </div>
      {entries && entries.length ? (
        <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "4px 14px", fontSize: 13 }}>
          {entries.map(([k, v]) => (
            <React.Fragment key={k}>
              <span style={{ ...MONO, color: "var(--text-muted)" }}>{k}</span>
              <span style={{ ...MONO, color: "var(--text-primary)", wordBreak: "break-word" }}>
                {typeof v === "object" ? JSON.stringify(v) : String(v)}
              </span>
            </React.Fragment>
          ))}
        </div>
      ) : (
        <div style={{ ...MONO, color: "var(--text-primary)" }}>
          {value == null ? "—" : JSON.stringify(value)}
        </div>
      )}
    </div>
  );
}

function EpisodeCard({ episode, onChanged }: {
  episode: HealingEpisode; onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const tone = TONE[episode.status] ?? "muted";

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); onChanged(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  return (
    <Card style={{ borderLeft: `3px solid ${
      tone === "ok" ? "var(--status-completed)"
      : tone === "warn" ? "var(--status-running)"
      : tone === "bad" ? "var(--status-failed)" : "var(--border-default)"}` }}>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <Pill tone={tone}>{episode.status}</Pill>
        <strong style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>
          {episode.operator}
        </strong>
        <span style={muted}>
          case type {episode.case_type_id.slice(0, 8)} · #{episode.sequence}
        </span>
      </div>

      <Diagnosis value={episode.diagnosis} />

      {/* `refused` (the ladder would not put this in front of anybody) reads
          differently from `rejected` (a person said no). */}
      {episode.status === "refused" && (
        <SubPanel tone="warn">
          <div style={{ fontSize: 13, color: "var(--text-primary)" }}>
            Stopped at <strong style={MONO}>{episode.stopped_at}</strong> — {episode.stopped_reason}
          </div>
          <div style={{ ...muted, marginTop: 4 }}>
            The ladder declined to put this in front of anybody. Nobody rejected it.
          </div>
        </SubPanel>
      )}
      {episode.status === "rejected" && (
        <SubPanel>
          <div style={{ fontSize: 13, color: "var(--text-primary)" }}>
            Rejected by {episode.approved_by ?? "a person"} — {episode.decided_reason}
          </div>
        </SubPanel>
      )}

      <Ladder episode={episode} />

      {episode.status === "watching" && (
        <SubPanel tone="warn">
          {/* Deployed is not healed. */}
          <strong style={{ fontSize: 13, color: "var(--text-primary)" }}>
            Applied, and being watched.
          </strong>
          <p style={{ fontSize: 13, color: "var(--text-secondary)", margin: "4px 0 0", lineHeight: 1.55 }}>
            It is not <em>healed</em> until the window closes without a
            regression{episode.watch_until
              ? ` — until ${new Date(episode.watch_until).toLocaleString()}`
              : ""}. Watching <span style={MONO}>{episode.watch_metric}</span>
            {episode.watch_bound_pct != null
              ? `, bound ${episode.watch_bound_pct}%` : ""}.
          </p>
        </SubPanel>
      )}

      {episode.status === "rolled_back" && (
        <SubPanel tone="bad">
          <Pill tone="bad">rolled back automatically</Pill>
          <p style={{ fontSize: 13, color: "var(--text-primary)", margin: "6px 0 0", lineHeight: 1.55 }}>
            {episode.rollback_reason}
          </p>
          {episode.incident_case_id && (
            <p style={{ ...muted, margin: "4px 0 0" }}>
              Incident case {episode.incident_case_id.slice(0, 8)} was opened.
            </p>
          )}
        </SubPanel>
      )}

      {episode.status === "ready" && (
        <div style={{ display: "grid", gap: "var(--space-sm)", marginTop: "var(--space-md)",
                      paddingTop: "var(--space-md)", borderTop: "1px solid var(--border-subtle)" }}>
          <TextInput value={reason} onChange={(e) => setReason(e.target.value)}
                     placeholder="Why (recorded against your approval)"
                     style={{ width: "100%" }} />
          <div style={{ display: "flex", gap: "var(--space-sm)" }}>
            {/* Says what it does: approving APPLIES. A button labelled
                "Approve" would imply a later step that does not exist. */}
            <Button disabled={busy}
                    onClick={() => act(() => approveHealingEpisode(episode.id,
                                                                  { reason }))}>
              Approve and apply
            </Button>
            <Button variant="secondary" disabled={busy || reason.trim().length < 5}
                    onClick={() => act(() => rejectHealingEpisode(episode.id, reason))}>
              Reject
            </Button>
          </div>
          <p style={{ ...muted, margin: 0, lineHeight: 1.55 }}>
            Approving writes this change to live config and signs its rollback.
            A rejection needs a reason — a later scan will propose the same thing
            again.
          </p>
        </div>
      )}

      {(episode.status === "watching" || episode.status === "healed") && (
        <div style={{ display: "flex", gap: "var(--space-sm)", alignItems: "center",
                      flexWrap: "wrap", marginTop: "var(--space-md)",
                      paddingTop: "var(--space-md)", borderTop: "1px solid var(--border-subtle)" }}>
          <TextInput value={reason} onChange={(e) => setReason(e.target.value)}
                     placeholder="Why you are undoing this" style={{ width: 320 }} />
          <Button variant="danger" disabled={busy || reason.trim().length < 5}
                  onClick={() => act(() => rollbackHealingEpisode(episode.id, reason))}>
            Roll back
          </Button>
        </div>
      )}

      {error && <ErrorNote style={{ marginTop: "var(--space-md)" }}>{error}</ErrorNote>}
    </Card>
  );
}

type TabKey = "awaiting" | "history" | "boundary";

export default function Healing() {
  const [tab, setTab] = useState<TabKey>("awaiting");
  const [status, setStatus] = useState<HealingStatus | null>(null);
  const [episodes, setEpisodes] = useState<HealingEpisode[]>([]);
  const [chain, setChain] = useState<{ verified: boolean; episodes: number } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const s = await getHealingStatus();
      setStatus(s);
      if (s.enabled) {
        const [e, c] = await Promise.all([listHealingEpisodes(), verifyHealingChain()]);
        setEpisodes(e.episodes); setChain(c);
      }
    } catch (e) { setError(String(e)); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const awaiting = episodes.filter(e => e.status === "ready");
  const watching = episodes.filter(e => e.status === "watching");

  const TABS = [
    { key: "awaiting" as const, label: "Needs you", badge: awaiting.length || undefined },
    { key: "history"  as const, label: "Episodes",  badge: episodes.length || undefined },
    { key: "boundary" as const, label: "What it can reach" },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", width: "100%", height: "100%",
                  overflow: "hidden", boxSizing: "border-box" }}>
      {/* No page title here: `AppLayout` already renders one from `nav-data`,
          so a module adding its own gets two stacked title bars. */}

      {/* The one fact that changes how every tab reads, stated once at the top.
          Says WHICH switch is off, because the two send you to different
          screens. */}
      {status && !status.enabled && (
        <Banner tone="muted" title="Self-healing is off.">
          {status.reason} Operator switch{" "}
          <span style={MONO}>{String(status.operator_switch)}</span>
          {" · "}tenant opted in{" "}
          <span style={MONO}>{String(status.tenant_settings?.enabled)}</span>
        </Banner>
      )}

      {/* Deployed is not healed. An episode inside its watch window is live
          config nobody has finished judging, and it is invisible from the
          approval queue it has already left. */}
      {status?.enabled && watching.length > 0 && (
        <Banner
          tone="warn"
          title={`${watching.length} change${watching.length === 1 ? " is" : "s are"} applied and still being watched.`}
        >
          Applied is not healed — the window has to close without a regression first.
        </Banner>
      )}

      <Tabs ariaLabel="Self-healing" tabs={TABS} value={tab} onChange={k => setTab(k as TabKey)} />

      <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-lg) var(--space-2xl)",
                    boxSizing: "border-box" }}>
        {error && <ErrorNote style={{ marginBottom: "var(--space-md)" }}>{error}</ErrorNote>}

        {loading ? (
          <div style={{ display: "flex", justifyContent: "center", padding: "var(--space-2xl)" }}>
            <Spinner size={24} />
          </div>
        ) : !status?.enabled ? (
          <EmptyState
            title="Nothing to show while it is off"
            description="No episode is proposed, and no config can be changed by this module. Switch it on to let HxEvolve's findings reach the ladder." />
        ) : (
          <>
            {tab === "awaiting" && (
              awaiting.length === 0
                ? <EmptyState
                    title="Nothing is waiting for you"
                    description="No proposal has cleared the ladder and stopped at a person. Refused ones are on the Episodes tab, with the rung that stopped them." />
                : <div style={{ display: "grid", gap: "var(--space-md)" }}>
                    {awaiting.map((e) => (
                      <EpisodeCard key={e.id} episode={e} onChanged={load} />
                    ))}
                  </div>
            )}

            {tab === "history" && (
              <>
                {chain && (
                  <Card style={{ marginBottom: "var(--space-md)", display: "flex",
                                 alignItems: "center", gap: "var(--space-sm)" }}>
                    <Pill tone={chain.verified ? "ok" : "bad"}>
                      {chain.verified ? "record intact" : "record broken"}
                    </Pill>
                    <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>
                      {chain.episodes} episode{chain.episodes === 1 ? "" : "s"} chained.
                    </span>
                  </Card>
                )}
                {episodes.length === 0
                  ? <EmptyState title="No episodes"
                                description="HxEvolve has not surfaced anything that reached the ladder." />
                  : <div style={{ display: "grid", gap: "var(--space-md)" }}>
                      {episodes.map((e) => (
                        <EpisodeCard key={e.id} episode={e} onChanged={load} />
                      ))}
                    </div>}
              </>
            )}

            {tab === "boundary" && status && (
              <Card>
                <SectionTitle>What a healing change can reach</SectionTitle>
                <div style={{ display: "grid", gridTemplateColumns: "auto 1fr",
                              gap: "8px 16px", fontSize: 13 }}>
                  <span style={{ ...muted }}>Operators</span>
                  <span style={{ ...MONO, color: "var(--text-primary)" }}>
                    {status.boundary.operators.join(", ") || "—"}
                  </span>
                  <span style={{ ...muted }}>Reaches</span>
                  <span style={{ ...MONO, color: "var(--text-primary)" }}>
                    {Object.keys(status.boundary.reaches).length
                      ? [...new Set(Object.values(status.boundary.reaches)
                          .flatMap((r) => Object.keys(r)))].join(", ")
                      : "—"}
                  </span>
                </div>
                <SubPanel>
                  <p style={{ ...muted, margin: 0, lineHeight: 1.55 }}>
                    It cannot name authentication, policy, connectors, credentials
                    or agents — {status.boundary.forbidden.length} surfaces,
                    intersection empty.
                  </p>
                </SubPanel>
              </Card>
            )}
          </>
        )}
      </div>
    </div>
  );
}
