// Index queue — the operator's window on background document indexing (mig 203).
//
// A queue with no window on it is indistinguishable from documents that were
// silently dropped. Three things this panel exists to say, in order:
//
//  1. **Whether anything drains the queue at all.** Jobs sit `pending` forever
//     without a `document_index` schedule, and forty pending jobs with no
//     warning reads as a slow queue rather than a missing one.
//  2. **Why a job failed**, on the row — `last_error`, not "failed".
//  3. **What can be done about it**: cancel a job that has not started, retry
//     one that failed. A retry is a new job; the failure stays on the record.
//
// Admin-only (`ai.index_queue.read`), because it names documents across every
// case in the tenant. The per-case listing stays with case access.
import React, { useCallback, useEffect, useState } from "react";
import { apiFetch, errorFromResponse } from "@shared/api/client";
import ErrorCodeLink from "@shared/errors/ErrorCodeLink";

export interface IndexJob {
  id: string;
  document_id: string;
  case_id: string | null;
  status: "pending" | "running" | "done" | "failed" | "cancelled";
  attempts: number;
  max_attempts: number;
  reindex: boolean;
  chunks_indexed: number | null;
  last_error: string | null;
  requested_by: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  filename: string | null;
  case_number: string | null;
}

interface Queue {
  count: number;
  counts: Record<string, number>;
  drain_scheduled: boolean;
  jobs: IndexJob[];
}

const STATUSES = ["", "pending", "running", "failed", "done", "cancelled"] as const;

const cell: React.CSSProperties = { padding: "6px 4px", verticalAlign: "top" };

function when(iso: string | null) {
  return iso ? new Date(iso).toLocaleString() : "—";
}

export default function IndexQueuePanel() {
  const [data, setData] = useState<Queue | null>(null);
  const [status, setStatus] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [errObj, setErrObj] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await apiFetch(`/hxnexus/documents/index-jobs${status ? `?status=${status}` : ""}`);
      if (r.status === 403 || r.status === 401) {
        setError("Admin access required — this lists documents across every case in the tenant.");
        setErrObj(null);
        setData(null);
        return;
      }
      if (!r.ok) throw await errorFromResponse(r);
      setData(await r.json());
      setError(null);
      setErrObj(null);
    } catch (e: any) {
      setError(e?.message ?? "Could not load the queue");
      setErrObj(e);
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => { void load(); }, [load]);

  async function act(job: IndexJob, verb: "cancel" | "retry") {
    if (!job.case_id) return;
    setBusy(job.id);
    try {
      const r = await apiFetch(
        `/hxnexus/cases/${job.case_id}/documents/index-jobs/${job.id}/${verb}`,
        { method: "POST" });
      if (!r.ok) throw await errorFromResponse(r);
      await load();
    } catch (e: any) {
      setError(e?.message ?? `Could not ${verb} the job`);
      setErrObj(e);
    } finally {
      setBusy(null);
    }
  }

  const pending = data?.counts?.pending ?? 0;

  return (
    <div style={{ padding: 16 }}>
      <h3 style={{ margin: "0 0 4px", fontSize: 14 }}>Document index queue</h3>
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 12 }}>
        Documents queued with <code>background: true</code>, indexed by a
        <code> document_index</code> schedule rather than inside the request.
      </div>

      {data && !data.drain_scheduled && (
        <div role="alert" style={{ padding: 8, fontSize: 11, borderRadius: 4, marginBottom: 12,
                                   background: "var(--bg-input)", color: "var(--status-failed)" }}>
          No active <code>document_index</code> schedule for this tenant — nothing drains
          this queue{pending > 0 ? `, and ${pending} job${pending === 1 ? " is" : "s are"} waiting` : ""}.
          Create one in Scheduler.
        </div>
      )}

      <div style={{ display: "flex", gap: 4, marginBottom: 12, flexWrap: "wrap" }}>
        {STATUSES.map((s) => (
          <button key={s || "all"} onClick={() => setStatus(s)} style={{
            padding: "4px 10px", fontSize: 11, borderRadius: 4, cursor: "pointer",
            border: "1px solid var(--border-default)",
            background: status === s ? "var(--accent)" : "transparent",
            color: status === s ? "var(--bg-base)" : "var(--text-muted)",
          }}>
            {s || "all"}
            {s && data?.counts?.[s] ? ` (${data.counts[s]})` : ""}
          </button>
        ))}
        <button onClick={() => void load()} style={{
          padding: "4px 10px", fontSize: 11, borderRadius: 4, cursor: "pointer",
          border: "1px solid var(--border-default)", background: "transparent",
          color: "var(--text-muted)", marginLeft: "auto",
        }}>Refresh</button>
      </div>

      {error && (
        <div style={{ padding: 8, fontSize: 11, borderRadius: 4, marginBottom: 12,
                      background: "var(--bg-input)", color: "var(--status-failed)" }}>
          {error}<ErrorCodeLink error={errObj} componentKey="hxnexus" />
        </div>
      )}

      {loading && !data && (
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>Loading…</div>
      )}

      {data && data.jobs.length === 0 && (
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
          {status ? `No ${status} jobs.` : "The queue is empty."}
        </div>
      )}

      {data && data.jobs.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
                <th style={cell}>Document</th>
                <th style={cell}>Case</th>
                <th style={cell}>Status</th>
                <th style={cell}>Attempts</th>
                <th style={cell}>Queued</th>
                <th style={cell}>Reason</th>
                <th style={cell}></th>
              </tr>
            </thead>
            <tbody>
              {data.jobs.map((j) => (
                <tr key={j.id} style={{ borderTop: "1px solid var(--border-subtle)" }}>
                  <td style={cell}>{j.filename || j.document_id.slice(0, 8)}{j.reindex ? " · reindex" : ""}</td>
                  <td style={cell}>{j.case_number || (j.case_id ? j.case_id.slice(0, 8) : "—")}</td>
                  <td style={{ ...cell, color: j.status === "failed" ? "var(--status-failed)"
                                             : j.status === "done" ? "var(--status-completed)"
                                             : undefined }}>
                    {j.status}{j.status === "done" && j.chunks_indexed != null ? ` · ${j.chunks_indexed} chunks` : ""}
                  </td>
                  <td style={cell}>{j.attempts} / {j.max_attempts}</td>
                  <td style={cell}>{when(j.created_at)}</td>
                  <td style={{ ...cell, color: "var(--text-muted)", maxWidth: 320, wordBreak: "break-word" }}>
                    {j.last_error || ""}
                  </td>
                  <td style={cell}>
                    {j.status === "pending" && (
                      <button disabled={busy === j.id} onClick={() => void act(j, "cancel")}>Cancel</button>
                    )}
                    {(j.status === "failed" || j.status === "cancelled") && (
                      <button disabled={busy === j.id} onClick={() => void act(j, "retry")}>Retry</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
