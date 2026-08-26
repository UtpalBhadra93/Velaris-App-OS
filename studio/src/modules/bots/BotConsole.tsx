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
  disableBot,
  getBotJob,
  getBotProposal,
  listBotCredentials,
  listBotJobs,
  listBots,
  upsertBotCredential,
  type BotCredential,
  type BotDefinition,
  type BotJob,
  type BotJobAudit,
  type BotProposal,
} from "@shared/api/client";
import { Button, Card, EmptyState, Spinner } from "@shared/components";

import BotEditor from "./BotEditor";

const cellStyle: React.CSSProperties = {
  padding: "var(--space-sm)", borderBottom: "1px solid var(--border)",
  textAlign: "left", verticalAlign: "top",
};

const LIVE_STATUSES = ["queued", "leased", "running"];

export default function BotConsole() {
  const [bots, setBots] = useState<BotDefinition[] | null>(null);
  const [jobs, setJobs] = useState<BotJob[]>([]);
  const [credentials, setCredentials] = useState<BotCredential[]>([]);
  const [proposal, setProposal] = useState<BotProposal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  /** null = closed, "new" = a fresh draft, otherwise the bot being edited. */
  const [editing, setEditing] = useState<BotDefinition | "new" | null>(null);
  const [credRef, setCredRef] = useState("");
  const [credSecret, setCredSecret] = useState("");
  const [audit, setAudit] = useState<BotJobAudit | null>(null);

  const load = useCallback(async () => {
    try {
      const [b, j, c] = await Promise.all([
        listBots(), listBotJobs(), listBotCredentials(),
      ]);
      setBots(b); setJobs(j); setCredentials(c);
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

      {editing !== null && (
        <BotEditor
          bot={editing === "new" ? null : editing}
          onCancel={() => setEditing(null)}
          onDone={async () => { setEditing(null); await load(); }}
        />
      )}

      <Card style={{ marginBottom: "var(--space-xl)" }}>
        <div style={{ display: "flex", alignItems: "center",
                      justifyContent: "space-between" }}>
          <h3 style={{ marginTop: 0 }}>Bots</h3>
          <Button size="sm" disabled={busy} onClick={() => setEditing("new")}>
            Register a bot
          </Button>
        </div>
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
                  <td style={{ ...cellStyle, whiteSpace: "nowrap" }}>
                    {b.status === "draft" && (
                      <Button size="sm" disabled={busy}
                              onClick={() => act(() => approveBot(b.id))}>
                        Approve
                      </Button>
                    )}{" "}
                    <Button size="sm" variant="secondary" disabled={busy}
                            onClick={() => setEditing(b)}>
                      Edit
                    </Button>{" "}
                    {b.status !== "disabled" && (
                      <Button size="sm" variant="danger" disabled={busy}
                              onClick={() => act(() => disableBot(b.id))}>
                        Disable
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
        <h3 style={{ marginTop: 0 }}>Credentials</h3>
        <p style={{ fontSize: "0.875rem", color: "var(--text-muted)" }}>
          What a bot signs in to a supplier's site with. The value is encrypted
          under this tenant's key, leased to one job at a time, and revoked when
          that job ends — <strong>it is never shown again, to anyone, including
          you</strong>. Storing a ref that already exists rotates it, which makes
          any outstanding lease dead rather than merely stale.
        </p>
        <div style={{ display: "flex", gap: "var(--space-sm)", flexWrap: "wrap",
                      marginBottom: "var(--space-md)" }}>
          <input aria-label="Credential reference" placeholder="supplier/portal-login"
                 value={credRef} onChange={e => setCredRef(e.target.value)}
                 style={{ flex: "1 1 16rem", padding: "var(--space-sm)",
                          background: "var(--surface)", color: "var(--text-primary)",
                          border: "1px solid var(--border)",
                          borderRadius: "var(--radius-sm)" }} />
          <input aria-label="Secret" type="password" placeholder="secret"
                 value={credSecret} onChange={e => setCredSecret(e.target.value)}
                 style={{ flex: "1 1 12rem", padding: "var(--space-sm)",
                          background: "var(--surface)", color: "var(--text-primary)",
                          border: "1px solid var(--border)",
                          borderRadius: "var(--radius-sm)" }} />
          <Button disabled={busy || !credRef.trim() || !credSecret}
                  onClick={() => act(async () => {
                    await upsertBotCredential({
                      ref: credRef.trim(), secret: credSecret });
                    setCredRef(""); setCredSecret("");
                  })}>
            Store
          </Button>
        </div>
        {credentials.length === 0 ? (
          <EmptyState title="No credentials stored"
                      description="A bot that needs to sign in somewhere names a ref here." />
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.875rem" }}>
            <thead><tr>
              <th style={cellStyle}>Ref</th>
              <th style={cellStyle}>Environment variable</th>
              <th style={cellStyle}>Version</th>
              <th style={cellStyle}>Stored by</th>
            </tr></thead>
            <tbody>
              {credentials.map(c => (
                <tr key={c.id}>
                  <td style={cellStyle}>{c.ref}</td>
                  <td style={cellStyle}><code>${c.env_name}</code></td>
                  <td style={cellStyle}>{c.version}</td>
                  <td style={cellStyle}>{c.created_by}</td>
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
                    {j.batch_id && (
                      <div style={{ fontSize: "0.6875rem", color: "var(--text-muted)" }}>
                        batch {j.batch_id.slice(0, 8)} ·{" "}
                        {(j.batch_index ?? 0) + 1} of {j.batch_size}
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
                    <Button size="sm" variant="secondary" disabled={busy}
                            onClick={async () => {
                              setError(null);
                              try { setAudit(await getBotJob(j.id)); }
                              catch (e) { setError(String(e)); }
                            }}>
                      Audit
                    </Button>{" "}
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

      {audit && (
        <Card style={{ marginBottom: "var(--space-xl)" }}>
          <div style={{ display: "flex", alignItems: "center",
                        justifyContent: "space-between" }}>
            <h3 style={{ marginTop: 0 }}>
              Run {audit.job.id.slice(0, 8)} — the whole account
            </h3>
            <Button size="sm" variant="secondary" onClick={() => setAudit(null)}>
              Close
            </Button>
          </div>
          <p style={{ fontSize: "0.875rem", color: "var(--text-muted)" }}>
            What this run did, which credentials it held while doing it, and every
            host it tried to reach. Three subsystems write these; answering
            "which bot did what" should not require knowing that.
          </p>

          <h4 style={{ marginBottom: 4 }}>Timeline</h4>
          {audit.events.length === 0 ? (
            <p style={{ fontSize: "0.875rem" }}>Nothing recorded yet.</p>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.8125rem" }}>
              <tbody>
                {audit.events.map((e, i) => (
                  <tr key={i}>
                    <td style={{ ...cellStyle, width: "12rem" }}>
                      {e.at ? new Date(e.at).toLocaleString() : "—"}
                    </td>
                    <td style={{ ...cellStyle, width: "7rem" }}>{e.kind}</td>
                    <td style={cellStyle}>
                      <code>{JSON.stringify(e.detail)}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h4 style={{ marginBottom: 4, marginTop: "var(--space-md)" }}>
            Credentials held
          </h4>
          {audit.credentials.length === 0 ? (
            <p style={{ fontSize: "0.875rem" }}>None — this run signed in nowhere.</p>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.8125rem" }}>
              <thead><tr>
                <th style={cellStyle}>Reference</th>
                <th style={cellStyle}>Leased</th>
                <th style={cellStyle}>Revoked</th>
              </tr></thead>
              <tbody>
                {audit.credentials.map((c, i) => (
                  <tr key={i}>
                    <td style={cellStyle}>{c.ref}</td>
                    <td style={cellStyle}>
                      {c.leased_at ? new Date(c.leased_at).toLocaleString() : "—"}
                    </td>
                    <td style={{ ...cellStyle,
                                 color: c.revoked_at ? undefined : "var(--danger)" }}>
                      {c.revoked_at
                        ? new Date(c.revoked_at).toLocaleString()
                        : "still held"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h4 style={{ marginBottom: 4, marginTop: "var(--space-md)" }}>
            Hosts reached
          </h4>
          {audit.network.length === 0 ? (
            <p style={{ fontSize: "0.875rem", color: "var(--text-muted)" }}>
              No egress recorded. Either this bot was granted no outbound hosts,
              or the egress gateway is not enabled on this deployment.
            </p>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.8125rem" }}>
              <thead><tr>
                <th style={cellStyle}>When</th>
                <th style={cellStyle}>Destination</th>
                <th style={cellStyle}>Result</th>
                <th style={cellStyle}>Bytes</th>
              </tr></thead>
              <tbody>
                {audit.network.map((n, i) => (
                  <tr key={i}>
                    <td style={cellStyle}>
                      {n.at ? new Date(n.at).toLocaleString() : "—"}
                    </td>
                    <td style={{ ...cellStyle, wordBreak: "break-all" }}>
                      {n.destination}
                    </td>
                    <td style={{ ...cellStyle,
                                 color: n.status === "allowed"
                                   ? undefined : "var(--danger)" }}>
                      {n.status}
                    </td>
                    <td style={cellStyle}>
                      {(n.bytes_out ?? 0)} out / {(n.bytes_in ?? 0)} in
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      )}

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
