/**
 * Oct item 3 — profile backfill: an operator scan that PROPOSES.
 *
 * The scan reads the tenant's history (portal accounts, case submitters, and
 * chat numbers only where they sit on a known person's case) and writes
 * proposals. Nothing is created or merged until a person accepts, one proposal
 * at a time — and the accept is re-checked against the data as it is then.
 *
 * What the screen is careful about:
 *   - the run button says plainly that it applies nothing;
 *   - each proposal shows its EVIDENCE (where every identifier came from, and
 *     whether it is verified) so a person decides on facts, not a label;
 *   - a rejection needs a reason, because it stops the scan proposing it again;
 *   - a merge needs a reason too — it moves one person's history onto another's
 *     screen, and the reason is the only record of why.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useState } from "react";

import {
  engageAcceptProposal,
  engageBackfillProposals,
  engageBackfillRuns,
  engageRejectProposal,
  engageStartBackfill,
  type BackfillIdentifier,
  type BackfillProposal,
  type BackfillRun,
  type BackfillStatus,
} from "@shared/api/client";
import {
  Banner, Button, Card, EmptyState, ErrorNote, Pill, SectionTitle, Select, SubPanel, TextInput,
} from "@shared/components";

const KIND_LABEL: Record<BackfillProposal["kind"], string> = {
  create_profile: "New profile",
  enrich_profile: "Add to a profile",
  merge_profiles: "Same person, two profiles",
};
const COUNT_LABEL: Record<string, string> = {
  people_found: "people found",
  proposed_new: "new proposals",
  proposed_refreshed: "refreshed",
  rejected_not_reproposed: "rejected earlier, not proposed again",
  superseded: "no longer supported",
  numbers_skipped_no_other_evidence: "numbers with nothing else behind them (skipped)",
  truncated: "truncated — run again to continue",
};
const muted: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)" };
const row: React.CSSProperties = {
  display: "flex", gap: "var(--space-sm)", alignItems: "center", flexWrap: "wrap",
};

function Identifiers({ items }: { items?: BackfillIdentifier[] }) {
  if (!items?.length) return null;
  return (
    <div style={{ display: "grid", gap: 4, marginTop: 6 }}>
      {items.map((i) => (
        <div key={`${i.kind}:${i.value}`} style={row}>
          <span style={{ ...muted, width: 110, fontFamily: "var(--font-mono)" }}>{i.kind}</span>
          <span style={{ fontSize: 13 }}>{i.display}</span>
          <Pill tone={i.verified ? "ok" : "muted"}>{i.verified ? "verified" : "unverified"}</Pill>
          <span style={muted}>from {i.via}</span>
        </div>
      ))}
    </div>
  );
}

function Proposal({ p, onDone }: { p: BackfillProposal; onDone: () => void }) {
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const isMerge = p.kind === "merge_profiles";
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); onDone(); } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  return (
    <Card style={{ marginBottom: "var(--space-sm)" }}>
      <div style={row}>
        <Pill tone={isMerge ? "warn" : "info"}>{KIND_LABEL[p.kind]}</Pill>
        <strong style={{ fontSize: 14 }}>
          {isMerge ? p.payload.profiles?.map((x) => x.display_name).join(" + ")
                   : p.payload.display_name}
        </strong>
        {p.payload.case_ids?.length ? (
          <span style={muted}>{p.payload.case_ids.length} case(s) to link</span>) : null}
      </div>
      <Identifiers items={isMerge ? p.payload.shared : p.payload.identifiers} />
      {p.evidence.sources?.length ? (
        <p style={{ ...muted, margin: "6px 0 0" }}>Evidence: {p.evidence.sources.join(", ")}</p>
      ) : null}
      {p.status === "proposed" ? (
        <div style={{ ...row, marginTop: 10 }}>
          <TextInput aria-label="Reason" value={reason} onChange={(e) => setReason(e.target.value)}
                     placeholder={isMerge ? "Why these are the same person (required)"
                                          : "Reason (required to reject)"}
                     style={{ flex: 1, minWidth: 220 }} />
          <Button size="sm" disabled={busy || (isMerge && reason.trim().length < 5)}
                  onClick={() => act(() => engageAcceptProposal(p.id, reason.trim() || undefined))}>
            {isMerge ? "Merge" : "Accept"}
          </Button>
          <Button size="sm" variant="secondary" disabled={busy || reason.trim().length < 5}
                  onClick={() => act(() => engageRejectProposal(p.id, reason.trim()))}>
            Reject
          </Button>
        </div>
      ) : (
        <p style={{ ...muted, margin: "8px 0 0" }}>
          {p.status}{p.decided_by ? ` by ${p.decided_by}` : ""}{p.reason ? ` — ${p.reason}` : ""}
        </p>
      )}
      {error && <ErrorNote style={{ marginTop: 8 }}>{error}</ErrorNote>}
    </Card>
  );
}

export default function ProfileBackfill() {
  const [status, setStatus] = useState<BackfillStatus>("proposed");
  const [items, setItems] = useState<BackfillProposal[]>([]);
  const [total, setTotal] = useState(0);
  const [runs, setRuns] = useState<BackfillRun[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  const load = useCallback(async () => {
    try {
      const [p, r] = await Promise.all([engageBackfillProposals({ status, limit: 100 }),
                                        engageBackfillRuns()]);
      setItems(p.proposals); setTotal(p.total); setRuns(r.runs); setError(null);
    } catch (e) {
      const msg = String(e);
      setError(/\b403\b|forbidden/i.test(msg)
        ? "Backfill proposals are reviewed by an administrator or a supervisor."
        : msg);
    }
  }, [status]);
  useEffect(() => { void load(); }, [load]);

  const last = runs[0];
  return (
    <div>
      <Banner tone="info" title="A scan proposes; it applies nothing.">
        It reads this tenant's portal accounts and case submitters, and a chat number only
        when it sits on a known person's case. Every profile, link and merge waits for a
        person to accept it.
      </Banner>
      <Card style={{ margin: "var(--space-md) 0" }}>
        <SectionTitle right={
          <Button size="sm" disabled={running} onClick={async () => {
            setRunning(true); setError(null);
            try { await engageStartBackfill(); await load(); }
            catch (e) { setError(String(e)); } finally { setRunning(false); }
          }}>{running ? "Scanning…" : "Run a scan"}</Button>}>Last scan</SectionTitle>
        {last ? (
          <div style={row}>
            <span style={muted}>{last.started_at ? new Date(last.started_at).toLocaleString() : ""}
              {" "}by {last.started_by}</span>
            {Object.entries(last.counts).filter(([k, v]) => COUNT_LABEL[k] && v).map(([k, v]) => (
              <Pill key={k} tone={k === "truncated" ? "warn" : "muted"}>{v} {COUNT_LABEL[k]}</Pill>
            ))}
          </div>
        ) : <p style={{ ...muted, margin: 0 }}>No scan has been run in this tenant.</p>}
      </Card>
      <div style={{ ...row, marginBottom: "var(--space-sm)" }}>
        <Select aria-label="Show" value={status}
                onChange={(e) => setStatus(e.target.value as BackfillStatus)}>
          <option value="proposed">Waiting for a decision</option>
          <option value="accepted">Accepted</option>
          <option value="rejected">Rejected</option>
          <option value="superseded">No longer supported</option>
        </Select>
        <span style={muted}>{total} in total{total > items.length ? `, showing ${items.length}` : ""}</span>
      </div>
      {error && <ErrorNote style={{ marginBottom: 8 }}>{error}</ErrorNote>}
      {items.length === 0 && !error ? (
        <EmptyState title="Nothing here" description={status === "proposed"
          ? "Run a scan to propose profiles from this tenant's history." : "No proposals in this state."} />
      ) : items.map((p) => <Proposal key={p.id} p={p} onDone={load} />)}
      {status === "proposed" && items.length > 0 && (
        <SubPanel><span style={muted}>
          Accepting re-checks the proposal against the data as it is now. If something changed
          since the scan, it is refused and marked as no longer supported — run the scan again.
        </span></SubPanel>
      )}
    </div>
  );
}
