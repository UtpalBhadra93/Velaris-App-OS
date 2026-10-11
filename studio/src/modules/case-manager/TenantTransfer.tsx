/**
 * Moving a case between tenants — the Studio screen (Oct item 6; API = mig 205).
 *
 * Two pieces:
 *
 *   - `MoveTenantTab` sits on a case. It asks the server's **preflight** for the
 *     blockers BEFORE the button is live, so an administrator reads "a payment
 *     request exists" as a list, not as a 409 after pressing it.
 *   - `TenantTransfers` (`/case-transfers`) lists **Outgoing** (the source's
 *     requests and, once accepted, the tombstone manifest) and **Incoming**
 *     (requests the source has approved, awaiting this tenant's decision).
 *
 * The server decides everything: who may approve (never the requester), which
 * side may act, what the destination may see. The screen only hides a button
 * the server would refuse anyway — Approve is not shown to the requester — and
 * shows the server's refusal verbatim if it refuses regardless.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useState } from "react";

import {
  decideTenantTransfer,
  listTenantTransfers,
  preflightTenantTransfer,
  requestTenantTransfer,
  type TenantTransfer,
  type TenantTransferPreflight,
} from "@shared/api/client";
import {
  Banner, Button, Card, EmptyState, ErrorNote, FIELD_STYLE, MONO, Pill,
  SectionTitle, Spinner, Tabs, TextInput, type PillTone,
} from "@shared/components";
import { useAuth } from "@/auth/AuthContext";

const TONE: Record<string, PillTone> = {
  requested: "warn", approved: "info", accepted: "ok", refused: "bad", cancelled: "muted",
};
const muted: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)" };
const label: React.CSSProperties = {
  ...muted, fontFamily: "var(--font-mono)", textTransform: "uppercase",
  letterSpacing: "0.06em", fontSize: 11, marginBottom: 6,
};

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

// ── Who may see the tab ──────────────────────────────────────────────────────

let _mayTransfer: Promise<boolean> | null = null;

/**
 * Whether the caller holds `case.actions.transfer_tenant` — asked of the SERVER.
 *
 * `is_admin` is display-only and a role name is not the privilege: since Phase 6
 * an operator may grant it to another role in data, and a tab keyed on "admin"
 * would hide a feature its holder is entitled to. The list route is gated by
 * exactly that privilege and reads nothing sensitive, so a 200 is the answer.
 * Asked once per page load.
 */
export function useMayTransferTenant(): boolean {
  const [may, setMay] = useState(false);
  useEffect(() => {
    let live = true;
    _mayTransfer ??= listTenantTransfers("outgoing").then(() => true, () => false);
    _mayTransfer.then((v) => { if (live) setMay(v); });
    return () => { live = false; };
  }, []);
  return may;
}

// ── On a case ────────────────────────────────────────────────────────────────

export function MoveTenantTab({ caseId }: { caseId: string }) {
  const [destination, setDestination] = useState("");
  const [reason, setReason] = useState("");
  const [check, setCheck] = useState<TenantTransferPreflight | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<TenantTransfer | null>(null);

  // Ask the server what the button would meet, whenever the destination settles.
  useEffect(() => {
    const dest = destination.trim();
    setCheck(null); setCheckError(null);
    if (!dest) return;
    const t = window.setTimeout(async () => {
      setChecking(true);
      try { setCheck(await preflightTenantTransfer(caseId, dest)); }
      catch (e) { setCheckError(errText(e)); }
      finally { setChecking(false); }
    }, 400);
    return () => window.clearTimeout(t);
  }, [caseId, destination]);

  const submit = async () => {
    setBusy(true); setError(null);
    try { setDone(await requestTenantTransfer(caseId, destination.trim(), reason.trim())); }
    catch (e) { setError(errText(e)); }
    finally { setBusy(false); }
  };

  if (done) {
    return (
      <Banner tone="ok" title="Transfer requested">
        A <b>different</b> administrator of this tenant must approve it before the
        destination sees it. Track it under <a href="/case-transfers">Case transfers → Outgoing</a>.
      </Banner>
    );
  }

  const reasonOk = reason.trim().length >= 10;
  const canSubmit = !!check?.can_move && reasonOk && !busy;

  return (
    <div style={{ display: "grid", gap: "var(--space-md)", maxWidth: 640 }}>
      <Banner tone="muted" title="Move this case to another tenant">
        The case leaves this tenant's data. Three people are involved: you request,
        another administrator here approves, and an administrator of the destination
        accepts or refuses. Nothing moves until they accept.
      </Banner>

      <div>
        <div style={label}>Destination tenant (slug or id)</div>
        <TextInput value={destination} onChange={(e) => setDestination(e.target.value)}
                   placeholder="e.g. acme" style={{ width: "100%" }} />
      </div>

      {checking && <div style={muted}><Spinner size={12} /> Checking what would stop this move…</div>}
      {checkError && <ErrorNote>{checkError}</ErrorNote>}
      {check && (check.can_move ? (
        <Banner tone="ok" title="Nothing blocks this move" />
      ) : (
        <Banner tone="bad" title="This case cannot move yet">
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {check.blockers.map((b) => <li key={b}>{b}</li>)}
          </ul>
        </Banner>
      ))}

      <div>
        <div style={label}>Reason — read by both tenants</div>
        <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={3}
                  placeholder="At least 10 characters"
                  style={{ ...FIELD_STYLE, width: "100%", resize: "vertical" }} />
      </div>

      {error && <ErrorNote>{error}</ErrorNote>}
      <div>
        <Button onClick={submit} disabled={!canSubmit}>
          {busy ? "Requesting…" : "Request the move"}
        </Button>
        {!reasonOk && reason.length > 0 && (
          <span style={{ ...muted, marginLeft: 10 }}>The reason needs at least 10 characters.</span>
        )}
      </div>
    </div>
  );
}

// ── The two lists ────────────────────────────────────────────────────────────

function Manifest({ value }: { value: Record<string, unknown> }) {
  const rows = Object.entries(value);
  if (!rows.length) return null;
  return (
    <div style={{ marginTop: "var(--space-md)" }}>
      <div style={label}>What happened to everything attached</div>
      <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "4px 14px", fontSize: 13 }}>
        {rows.map(([k, v]) => (
          <React.Fragment key={k}>
            <span style={{ ...MONO, color: "var(--text-muted)" }}>{k}</span>
            <span style={{ ...MONO, color: "var(--text-primary)", wordBreak: "break-word" }}>
              {typeof v === "object" ? JSON.stringify(v) : String(v)}
            </span>
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}

function TransferCard({ t, me, onChanged }: {
  t: TenantTransfer; me: string | undefined; onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const source = t.side === "source";

  const act = async (action: "approve" | "cancel" | "accept" | "refuse") => {
    if (action === "accept" && !window.confirm(
      `Move case ${t.case_number ?? t.case_id} into this tenant now? This cannot be undone from here.`)) return;
    if (action === "approve" && !window.confirm(
      `Approve moving case ${t.case_number ?? t.case_id} out of this tenant? The destination will then be able to accept it.`)) return;
    setBusy(true); setError(null);
    try { await decideTenantTransfer(t.id, action, note.trim()); onChanged(); }
    catch (e) { setError(errText(e)); }
    finally { setBusy(false); }
  };

  const open = t.state === "requested" || t.state === "approved";
  // The server refuses a requester approving their own request; the screen does
  // not offer it. Everything else is the server's call.
  const canApprove = source && t.state === "requested" && t.requested_by !== me;
  const canCancel = source && open;
  const canDecide = !source && t.state === "approved";

  return (
    <Card style={{ marginBottom: "var(--space-md)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <Pill tone={TONE[t.state] ?? "muted"}>{t.state}</Pill>
        <span style={{ ...MONO, fontWeight: 600 }}>{t.case_number ?? t.case_id}</span>
        {t.case_type_name && <span style={muted}>{t.case_type_name}</span>}
        <span style={{ ...muted, marginLeft: "auto" }}>
          {t.requested_at ? new Date(t.requested_at).toLocaleString() : ""}
        </span>
      </div>
      <div style={{ marginTop: 8, fontSize: 13, color: "var(--text-primary)" }}>{t.reason}</div>
      {source && t.state === "requested" && t.requested_by === me && (
        <div style={{ ...muted, marginTop: 6 }}>
          Waiting for a different administrator of this tenant to approve.
        </div>
      )}
      {t.decision_note && (
        <div style={{ ...muted, marginTop: 6 }}>Destination's note: {t.decision_note}</div>
      )}
      {t.state === "accepted" && t.manifest && <Manifest value={t.manifest} />}

      {canDecide && (
        <TextInput value={note} onChange={(e) => setNote(e.target.value)}
                   placeholder="Note to the source (optional)"
                   style={{ width: "100%", marginTop: "var(--space-md)" }} />
      )}
      {error && <ErrorNote style={{ marginTop: 8 }}>{error}</ErrorNote>}
      {(canApprove || canCancel || canDecide) && (
        <div style={{ display: "flex", gap: 8, marginTop: "var(--space-md)" }}>
          {canApprove && <Button size="sm" disabled={busy} onClick={() => act("approve")}>Approve</Button>}
          {canCancel && <Button size="sm" variant="secondary" disabled={busy} onClick={() => act("cancel")}>Cancel request</Button>}
          {canDecide && <Button size="sm" disabled={busy} onClick={() => act("accept")}>Accept — move the case here</Button>}
          {canDecide && <Button size="sm" variant="danger" disabled={busy} onClick={() => act("refuse")}>Refuse</Button>}
        </div>
      )}
    </Card>
  );
}

type Dir = "outgoing" | "incoming";

export default function TenantTransfers() {
  const { user } = useAuth();
  const [dir, setDir] = useState<Dir>("outgoing");
  const [items, setItems] = useState<TenantTransfer[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try { setItems((await listTenantTransfers(dir)).items); }
    catch (e) { setItems([]); setError(errText(e)); }
  }, [dir]);

  useEffect(() => { setItems(null); load(); }, [load]);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <Tabs ariaLabel="Case transfers"
            tabs={[{ key: "outgoing" as Dir, label: "Outgoing" }, { key: "incoming" as Dir, label: "Incoming" }]}
            value={dir} onChange={setDir}
            right={<Button size="sm" variant="ghost" onClick={load}>Refresh</Button>} />
      <div style={{ flex: 1, overflow: "auto", padding: "var(--space-lg)" }}>
        <SectionTitle>
          {dir === "outgoing" ? "Cases this tenant asked to move out" : "Cases another tenant wants to move here"}
        </SectionTitle>
        {error && <ErrorNote>{error}</ErrorNote>}
        {items === null ? <Spinner /> : items.length === 0 ? (
          <EmptyState
            title={dir === "outgoing" ? "No outgoing transfers" : "Nothing waiting for this tenant"}
            description={dir === "outgoing"
              ? "Start one from a case: open it and use the Move tenant tab."
              : "A request appears here only after the source tenant's second administrator approves it."} />
        ) : items.map((t) => (
          <TransferCard key={t.id} t={{ ...t, side: t.side ?? (dir === "outgoing" ? "source" : "destination") }}
                        me={user?.user_id} onChanged={load} />
        ))}
      </div>
    </div>
  );
}
