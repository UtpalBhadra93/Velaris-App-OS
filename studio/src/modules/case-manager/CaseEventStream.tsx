// Case event stream + drift check (#30a / report §2.2).
//
// The relational case row is a PROJECTION of an append-only stream. This shows
// the stream and, more usefully, the verify result: replay the events, derive
// what the case should look like, and compare against the row. Divergence means
// something wrote case state without going through the lifecycle chokepoints —
// a bug class otherwise invisible until a case turns up in an impossible state.
//
// Cases created before the event-sourcing cutover have no stream and are never
// backfilled (§7.5). The panel says so explicitly, because an empty list would
// otherwise read as "this case did nothing".
import React, { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@shared/api/client";

interface CaseEvent {
  seq: number;
  event_type: string;
  data: Record<string, unknown>;
  actor_id: string | null;
  actor_type: string;
  occurred_at: string | null;
}

interface Verify {
  event_sourced: boolean;
  verifiable: boolean;
  reason?: string;
  events?: number;
  sequence_gaps?: number[];
  consistent?: boolean;
  mismatches?: Array<{ field: string; projected: unknown; actual: unknown }>;
}

export default function CaseEventStream({ caseId }: { caseId: string }) {
  const [events, setEvents] = useState<CaseEvent[]>([]);
  const [sourced, setSourced] = useState<boolean | null>(null);
  const [verify, setVerify] = useState<Verify | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await apiFetch(`/cases/${caseId}/events`);
      if (!r.ok) throw new Error(await r.text());
      const body = await r.json();
      setEvents(body.events ?? []);
      setSourced(body.event_sourced);

      const v = await apiFetch(`/cases/${caseId}/events/verify`);
      if (v.ok) setVerify(await v.json());
      setError(null);
    } catch (e: any) {
      setError(e?.message ?? "Could not load the event stream");
    } finally {
      setLoading(false);
    }
  }, [caseId]);

  useEffect(() => { void load(); }, [load]);

  if (loading) return <div style={{ padding: 16, color: "var(--text-muted)" }}>Loading…</div>;

  return (
    <div style={{ padding: 16 }}>
      <h3 style={{ margin: "0 0 4px", fontSize: 14 }}>Event Stream</h3>

      {error && (
        <div style={{ padding: 8, fontSize: 11, borderRadius: 4, marginBottom: 12,
                      background: "var(--bg-input)", color: "var(--status-failed)" }}>
          {error}
        </div>
      )}

      {sourced === false && (
        <div style={{ padding: 10, fontSize: 11, borderRadius: 4, lineHeight: 1.6,
                      background: "var(--bg-input)", color: "var(--text-muted)" }}>
          This case predates the event-sourcing cutover, so it has no stream — and
          one is deliberately never reconstructed. Its history exists in the audit
          trail and step completions, but those were not recorded as an append-only
          stream, so a rebuilt history would be a guess presented as a fact.
          Cases created after the cutover are fully replayable.
        </div>
      )}

      {verify?.verifiable && (
        <div style={{
          padding: 10, fontSize: 11, borderRadius: 4, marginBottom: 12, lineHeight: 1.6,
          background: "var(--bg-input)",
          color: verify.consistent ? "var(--accent)" : "var(--status-failed)",
        }}>
          {verify.consistent ? (
            <>✓ Consistent — replaying {verify.events} event(s) reproduces the case exactly.</>
          ) : (
            <>
              ⚠ Drift detected. The recorded history does not reproduce the current
              case, which means state was written outside the lifecycle chokepoints.
              {verify.mismatches?.map(m => (
                <div key={m.field} style={{ marginTop: 4 }}>
                  <code>{m.field}</code>: stream says <code>{String(m.projected)}</code>,
                  row says <code>{String(m.actual)}</code>
                </div>
              ))}
              {!!verify.sequence_gaps?.length && (
                <div style={{ marginTop: 4 }}>
                  Missing sequence numbers: {verify.sequence_gaps.join(", ")} — events are
                  append-only, so a gap means one was deleted.
                </div>
              )}
            </>
          )}
        </div>
      )}

      {events.length === 0 && sourced && (
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>No events yet.</div>
      )}

      {events.map(e => (
        <div key={e.seq} style={{ display: "flex", gap: 10, padding: "6px 0",
                                  borderBottom: "1px solid var(--border-subtle)", fontSize: 11 }}>
          <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)", minWidth: 32 }}>
            #{e.seq}
          </span>
          <span style={{ minWidth: 130 }}>{e.event_type}</span>
          <span style={{ color: "var(--text-muted)", flex: 1 }}>
            {Object.entries(e.data ?? {}).map(([k, v]) => `${k}=${String(v)}`).join("  ")}
          </span>
          <span style={{ color: "var(--text-muted)" }}>
            {e.occurred_at ? new Date(e.occurred_at).toLocaleString() : ""}
          </span>
        </div>
      ))}
    </div>
  );
}
