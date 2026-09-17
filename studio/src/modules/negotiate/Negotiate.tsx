/**
 * Process-as-negotiation (F-12) — where a contract is read and agreed to.
 *
 * A person on each side approves a joint workflow. That is only meaningful if
 * they can read what they are agreeing to, so this screen shows every clause
 * the matcher resolved — including the ones that passed.
 *
 * Four things it is careful about:
 *
 *   1. **`seam_ceiling` and `effective_ceiling` are different things and are
 *      shown as different things.** The first is what the seam carries right
 *      now; the second is what was negotiated. A term agreed is not a term in
 *      force, and a screen showing one where the other belongs would be the
 *      single most misleading thing here.
 *   2. **The ceiling clause shows all three numbers** — what we would send, what
 *      they accept, and what this platform permits — because the third is what
 *      makes deny-overrides survive negotiation, and it is invisible otherwise.
 *   3. **Verify is a button.** "No trusted third party" means either side can
 *      recompute the other's answer, and that is worth being able to press.
 *   4. **Suspend is always available and always reachable.** It needs nothing
 *      from the counterparty, and a kill switch buried behind a state machine
 *      is one nobody finds at 3 a.m.
 *
 * Layout follows the platform shell (`/help`, `/work-center`): `AppLayout`
 * owns the title, the status filter chips are `/work-center`'s, and the body
 * is the only thing that scrolls.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";

import {
  approveContract,
  contractAnchors,
  listContracts,
  promoteContract,
  suspendContract,
  verifyContract,
  type JointContract,
} from "@shared/api/client";
import {
  Banner, Button, Card, EmptyState, ErrorNote, MONO, Pill, Spinner,
  SubPanel, TextInput, type PillTone,
} from "@shared/components";

const NEXT: Record<string, string> = { approved: "shadow", shadow: "enforced" };

const muted: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)" };

const STATUS_TONE: Record<string, PillTone> = {
  enforced: "ok", shadow: "info", approved: "info",
  suspended: "bad", unmatchable: "muted", matched: "warn",
};
const TONE_COLOR: Record<PillTone, string> = {
  ok: "var(--status-completed)", warn: "var(--status-running)",
  bad: "var(--status-failed)", info: "var(--accent)", muted: "var(--border-default)",
};

/** The three ceilings, as labelled figures rather than a run of prose. */
function Ceiling({ label, value, dim }: { label: string; value: string; dim?: boolean }) {
  return (
    <div>
      <div style={{ ...muted, fontFamily: "var(--font-mono)", fontSize: 10,
                    textTransform: "uppercase", letterSpacing: "0.06em" }}>
        {label}
      </div>
      <div style={{ ...MONO, fontSize: 15, marginTop: 3,
                    color: dim ? "var(--text-secondary)" : "var(--text-primary)" }}>
        {value}
      </div>
    </div>
  );
}

function ContractCard({ contract, onChanged }: {
  contract: JointContract; onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [verified, setVerified] = useState<string | null>(null);
  const [anchors, setAnchors] = useState<string | null>(null);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); onChanged(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  const tone = STATUS_TONE[contract.status] ?? "warn";
  const ceilingClause = contract.resolution?.clauses
    .find((c) => c.clause === "data_class_ceiling");

  return (
    <Card style={{ borderLeft: `3px solid ${TONE_COLOR[tone]}` }}>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <Pill tone={tone}>{contract.status}</Pill>
        <strong style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>
          {contract.name}
        </strong>
        <span style={muted}>v{contract.version}</span>
        <Pill tone={contract.signed_by_us ? "ok" : "muted"}>
          {contract.signed_by_us ? "we signed" : "unsigned by us"}
        </Pill>
        <Pill tone={contract.signed_by_them ? "ok" : "muted"}>
          {contract.signed_by_them ? "they signed" : "unsigned by them"}
        </Pill>
      </div>

      {contract.status === "unmatchable" && (
        <SubPanel tone="warn">
          <div style={{ fontSize: 13, color: "var(--text-primary)", lineHeight: 1.55 }}>
            No joint process satisfies both sides — {contract.unmatched_reason}
          </div>
        </SubPanel>
      )}

      {/* The two ceilings are different facts and are shown as different
          facts. Confusing them would be the most misleading thing on the page. */}
      <div style={{ display: "flex", gap: "var(--space-xl)", flexWrap: "wrap",
                    margin: "var(--space-md) 0 0", padding: "var(--space-md)",
                    borderRadius: "var(--radius-sm)", background: "var(--bg-elevated)" }}>
        <Ceiling label="carried right now" value={contract.seam_ceiling} />
        <Ceiling label="negotiated" value={contract.effective_ceiling ?? "—"} dim />
        {ceilingClause?.platform && (
          <Ceiling label="this platform permits" value={ceilingClause.platform} dim />
        )}
      </div>
      {contract.status !== "shadow" && contract.status !== "enforced" && (
        <p style={{ ...muted, margin: "8px 0 0", lineHeight: 1.55 }}>
          A term agreed is not a term in force — the seam carries nothing until
          this contract is running.
        </p>
      )}

      {contract.resolution?.clauses?.length ? (
        <div style={{ marginTop: "var(--space-md)" }}>
          <div style={{ ...muted, fontFamily: "var(--font-mono)", fontSize: 11,
                        textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 8 }}>
            Every clause
          </div>
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: 8 }}>
            {contract.resolution.clauses.map((c) => (
              <li key={c.clause} style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
                <Pill tone={c.ok ? "ok" : "bad"}>{c.ok ? "agreed" : "blocked"}</Pill>
                <div style={{ minWidth: 0 }}>
                  <div style={{ ...MONO, color: "var(--text-primary)" }}>{c.clause}</div>
                  <div style={{ ...muted, lineHeight: 1.5 }}>{c.detail}</div>
                </div>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {contract.composition?.steps?.length ? (
        <div style={{ marginTop: "var(--space-md)" }}>
          <div style={{ ...muted, fontFamily: "var(--font-mono)", fontSize: 11,
                        textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 8 }}>
            The joint process — {contract.composition.seams} boundary crossing
            {contract.composition.seams === 1 ? "" : "s"}
          </div>
          <ol style={{ margin: 0, paddingLeft: 20, fontSize: 13, display: "grid", gap: 6,
                       color: "var(--text-primary)" }}>
            {contract.composition.steps.map((s, i) => (
              <li key={`${s.fragment}-${i}`}>
                <span style={{ ...MONO, marginRight: 8 }}>{s.fragment}</span>
                <Pill tone={s.side === "ours" ? "ok" : "muted"}>
                  {s.side === "ours" ? "we run this" : "they run this"}
                </Pill>
                {s.crosses_seam && (
                  <span style={{ ...muted, color: "var(--status-running)", marginLeft: 8 }}>
                    → hands over
                  </span>
                )}
              </li>
            ))}
          </ol>
        </div>
      ) : null}

      <div style={{ display: "flex", gap: "var(--space-sm)", marginTop: "var(--space-md)",
                    paddingTop: "var(--space-md)", flexWrap: "wrap",
                    borderTop: "1px solid var(--border-subtle)" }}>
        {/* "No trusted third party" is worth being able to press. */}
        <Button variant="secondary" disabled={busy} onClick={async () => {
          setBusy(true);
          try {
            const out = await verifyContract(contract.id);
            setVerified(out.detail);
          } catch (e) { setError(String(e)); }
          finally { setBusy(false); }
        }}>Recompute the contract</Button>

        <Button variant="secondary" disabled={busy} onClick={async () => {
          setBusy(true);
          try {
            const out = await contractAnchors(contract.id);
            setAnchors(`${out.exchanges} exchange(s) — ${out.note}`);
          } catch (e) { setError(String(e)); }
          finally { setBusy(false); }
        }}>Check the shared history</Button>

        {contract.status === "matched" && (
          <Button disabled={busy || !contract.signed_by_them}
                  onClick={() => act(() => approveContract(contract.id))}>
            Approve
          </Button>
        )}
        {NEXT[contract.status] && (
          <Button disabled={busy}
                  onClick={() => act(() => promoteContract(contract.id,
                                                           NEXT[contract.status]))}>
            Move to {NEXT[contract.status]}
          </Button>
        )}
      </div>

      {/* Always reachable. A kill switch behind a state machine is one nobody
          finds at 3 a.m. */}
      {contract.status !== "suspended" && (
        <div style={{ display: "flex", gap: "var(--space-sm)", alignItems: "center",
                      marginTop: "var(--space-sm)", flexWrap: "wrap" }}>
          <TextInput value={reason} onChange={(e) => setReason(e.target.value)}
                     placeholder="Why you are closing the seam"
                     style={{ flex: 1, minWidth: 240 }} />
          <Button variant="danger" disabled={busy || reason.trim().length < 5}
                  onClick={() => act(() => suspendContract(contract.id, reason))}>
            Close the seam
          </Button>
        </div>
      )}

      {contract.status === "matched" && !contract.signed_by_them && (
        <p style={{ ...muted, margin: "8px 0 0", lineHeight: 1.55 }}>
          Both sides sign before either approves — a contract only we have signed
          is a proposal.
        </p>
      )}
      {contract.status === "suspended" && (
        <SubPanel tone="bad">
          <p style={{ fontSize: 13, color: "var(--text-primary)", margin: 0, lineHeight: 1.55 }}>
            Closed by {contract.suspended_by} — {contract.suspended_reason}. Proposals
            are still queued; nothing in flight was stranded.
          </p>
        </SubPanel>
      )}

      {verified && <SubPanel tone="ok"><span style={{ fontSize: 13 }}>{verified}</span></SubPanel>}
      {anchors && <SubPanel><span style={{ fontSize: 13 }}>{anchors}</span></SubPanel>}
      {error && <ErrorNote style={{ marginTop: "var(--space-md)" }}>{error}</ErrorNote>}
    </Card>
  );
}

export default function Negotiate() {
  const [contracts, setContracts] = useState<JointContract[]>([]);
  const [filter, setFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try { setContracts((await listContracts()).contracts); }
    catch (e) { setError(String(e)); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const statuses = useMemo(
    () => [...new Set(contracts.map(c => c.status))].sort(),
    [contracts],
  );
  const shown = filter ? contracts.filter(c => c.status === filter) : contracts;
  const suspended = contracts.filter(c => c.status === "suspended").length;

  return (
    <div style={{ display: "flex", flexDirection: "column", width: "100%", height: "100%",
                  overflow: "hidden", boxSizing: "border-box" }}>
      {/* No page title here: `AppLayout` already renders one from `nav-data`. */}

      {/* A closed seam is a standing fact about a live counterparty, and it is
          invisible from a card further down a long list. */}
      {!loading && suspended > 0 && (
        <Banner
          tone="bad"
          title={`${suspended} seam${suspended === 1 ? " is" : "s are"} closed.`}
        >
          Nothing crosses on a suspended contract. Proposals are still queued;
          nothing in flight was stranded.
        </Banner>
      )}

      <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-lg) var(--space-2xl)",
                    boxSizing: "border-box" }}>
        {/* Status chips, the /work-center pattern. */}
        {statuses.length > 1 && (
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center",
                        marginBottom: "var(--space-md)" }}>
            {["", ...statuses].map((s) => (
              <button key={s || "all"} onClick={() => setFilter(s)} style={{
                padding: "4px 12px", border: "1px solid var(--border-default)", borderRadius: 20,
                cursor: "pointer", fontSize: 12, fontFamily: "var(--font-mono)",
                background: filter === s ? "var(--accent)" : "var(--bg-elevated)",
                color: filter === s ? "var(--text-inverse)" : "var(--text-secondary)",
                fontWeight: filter === s ? 600 : 400,
              }}>{s || "All"}</button>
            ))}
            <span style={{ ...muted, marginLeft: "auto" }}>
              {shown.length} contract{shown.length === 1 ? "" : "s"}
            </span>
          </div>
        )}

        {error && <ErrorNote style={{ marginBottom: "var(--space-md)" }}>{error}</ErrorNote>}

        {loading ? (
          <div style={{ display: "flex", justifyContent: "center", padding: "var(--space-2xl)" }}>
            <Spinner size={24} />
          </div>
        ) : shown.length === 0 ? (
          <EmptyState
            title={filter ? `No ${filter} contracts` : "No contracts"}
            description={filter
              ? "Clear the filter to see the rest."
              : "A contract is proposed against a counterparty enrolled with a pinned key."} />
        ) : (
          <div style={{ display: "grid", gap: "var(--space-md)" }}>
            {shown.map((c) => (
              <ContractCard key={c.id} contract={c} onChanged={load} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
