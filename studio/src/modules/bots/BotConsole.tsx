/* ═══════════════════════════════════════════════════════════════════
   Bot Console — the RPA registry, the job queue, and the review gate

   Three things the UI has to make obvious, because each is a place where a
   reasonable-looking action is the dangerous one:

   1. **A draft bot cannot run.** Approval is the review of a container image
      that will execute on this host, and it cannot be done by the author. The
      list says who wrote it and who approved it, side by side.
   2. **Credentials and egress are reviewed together.** A bot with a supplier
      password and permission to reach anywhere is an exfiltration tool; the
      backend refuses that combination, and the row shows both so the refusal is
      not a surprise.
   3. **An attended job proposes, it does not apply.** The diff is the commit
      point, and accepting carries the checksum the operator saw — a case that
      changed underneath answers 409 rather than being silently overwritten.

   Copyright (c) 2024-2026 Velaris Contributors
   SPDX-License-Identifier: BSL-1.1
   ═══════════════════════════════════════════════════════════════════ */
import React, { useCallback, useEffect, useState } from "react";

import {
  acceptBotProposal,
  approveBot,
  cancelBotJob,
  getBotProposal,
  listBotJobs,
  listBots,
  type BotDefinition,
  type BotJob,
  type BotProposal,
} from "@shared/api/client";
import { Button, Card, EmptyState, Spinner } from "@shared/components";

const cellStyle: React.CSSProperties = {
  padding: "var(--space-sm)", borderBottom: "1px solid var(--border)",
  textAlign: "left", verticalAlign: "top",
};

const LIVE_STATUSES = ["queued", "leased", "running"];

export default function BotConsole() {
  const [bots, setBots] = useState<BotDefinition[] | null>(null);
  const [jobs, setJobs] = useState<BotJob[]>([]);
  const [proposal, setProposal] = useState<BotProposal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [b, j] = await Promise.all([listBots(), listBotJobs()]);
      setBots(b); setJobs(j);
    } catch (e) { setError(String(e)); }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); await load(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  return (
    <div style={{ padding: "var(--space-xl)", overflow: "auto", height: "100%",
                  boxSizing: "border-box" }}>
      {error && <p role="alert" style={{ color: "var(--danger)" }}>{error}</p>}

      <Card style={{ marginBottom: "var(--space-xl)" }}>
        <h3 style={{ marginTop: 0 }}>Bots</h3>
        <p style={{ fontSize: "0.875rem", color: "var(--text-muted)" }}>
          A bot is a digest-pinned container that performs a task and returns a result.
          It runs on the same hardened substrate as a marketplace app — internal network
          with no route out, non-root, read-only root filesystem, no database credentials —
          and it cannot run until somebody other than its author approves it.
        </p>

        {bots === null ? <Spinner /> : bots.length === 0 ? (
          <EmptyState title="No bots registered"
                      description="Register one with its image digest, then have a second admin approve it." />
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.875rem" }}>
            <thead><tr>
              <th style={cellStyle}>Name</th>
              <th style={cellStyle}>Image</th>
              <th style={cellStyle}>Credentials</th>
              <th style={cellStyle}>Egress</th>
              <th style={cellStyle}>Status</th>
              <th style={cellStyle} />
            </tr></thead>
            <tbody>
              {bots.map(b => (
                <tr key={b.id}>
                  <td style={cellStyle}>
                    {b.name}
                    <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                      by {b.created_by}
                      {b.approved_by ? ` · approved by ${b.approved_by}` : ""}
                    </div>
                  </td>
                  <td style={{ ...cellStyle, wordBreak: "break-all", maxWidth: "16rem" }}>
                    {b.image}
                    <div style={{ fontSize: "0.6875rem", color: "var(--text-muted)" }}>
                      {b.digest}
                    </div>
                  </td>
                  <td style={cellStyle}>
                    {(b.credential_refs ?? []).length
                      ? `${b.credential_refs.length} leased per job` : "none"}
                  </td>
                  <td style={cellStyle}>
                    {(b.granted_domains ?? []).length
                      ? b.granted_domains.join(", ") : "internal only"}
                  </td>
                  <td style={cellStyle}>{b.status}</td>
                  <td style={cellStyle}>
                    {b.status === "draft" && (
                      <Button disabled={busy}
                              onClick={() => act(() => approveBot(b.id))}>
                        Approve
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Card style={{ marginBottom: "var(--space-xl)" }}>
        <h3 style={{ marginTop: 0 }}>Jobs</h3>
        {jobs.length === 0 ? (
          <EmptyState title="No jobs yet"
                      description="Jobs appear when a workflow reaches a bot task or an agent runs one from a case." />
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.875rem" }}>
            <thead><tr>
              <th style={cellStyle}>Job</th>
              <th style={cellStyle}>Trigger</th>
              <th style={cellStyle}>Status</th>
              <th style={cellStyle}>Error</th>
              <th style={cellStyle} />
            </tr></thead>
            <tbody>
              {jobs.map(j => (
                <tr key={j.id}>
                  <td style={cellStyle}>
                    {j.id.slice(0, 8)}
                    {j.case_id && (
                      <div style={{ fontSize: "0.6875rem", color: "var(--text-muted)" }}>
                        case {j.case_id.slice(0, 8)}
                      </div>
                    )}
                  </td>
                  <td style={cellStyle}>{j.trigger}</td>
                  <td style={cellStyle}>
                    {j.status}
                    {j.attempt > 1 && ` (attempt ${j.attempt})`}
                  </td>
                  <td style={{ ...cellStyle, maxWidth: "18rem" }}>{j.error ?? ""}</td>
                  <td style={cellStyle}>
                    {LIVE_STATUSES.includes(j.status) && (
                      <Button variant="danger" disabled={busy}
                              onClick={() => act(() => cancelBotJob(j.id))}>
                        Cancel
                      </Button>
                    )}
                    {j.status === "succeeded" && j.trigger === "agent" && j.case_id && (
                      <Button variant="secondary" disabled={busy}
                              onClick={async () => {
                                setError(null);
                                try { setProposal(await getBotProposal(j.id)); }
                                catch (e) { setError(String(e)); }
                              }}>
                        Review
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {proposal && (
        <Card>
          <h3 style={{ marginTop: 0 }}>Proposed changes</h3>
          <p style={{ fontSize: "0.875rem", color: "var(--text-muted)" }}>
            An attended bot proposes; you commit. Accepting carries the checksum you
            are looking at, so if the case changed while this was open you will be
            asked to review the new diff rather than overwriting it.
          </p>
          {proposal.changes.length === 0 ? (
            <p>Nothing to change — the case already matches the bot's output.</p>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.875rem" }}>
              <thead><tr>
                <th style={cellStyle}>Field</th>
                <th style={cellStyle}>Current</th>
                <th style={cellStyle}>Proposed</th>
              </tr></thead>
              <tbody>
                {proposal.changes.map(c => (
                  <tr key={c.field}>
                    <td style={cellStyle}>{c.field}</td>
                    <td style={cellStyle}>{JSON.stringify(c.current)}</td>
                    <td style={cellStyle}>{JSON.stringify(c.proposed)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <div style={{ marginTop: "var(--space-md)", display: "flex",
                        gap: "var(--space-sm)" }}>
            <Button disabled={busy || proposal.changes.length === 0}
                    onClick={() => act(async () => {
                      await acceptBotProposal(proposal.job_id, proposal.checksum);
                      setProposal(null);
                    })}>
              Accept
            </Button>
            <Button variant="secondary" onClick={() => setProposal(null)}>
              Dismiss
            </Button>
          </div>
        </Card>
      )}
    </div>
  );
}
