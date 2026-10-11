/**
 * Legal holds — the Studio screens (Oct item 13; API = mig 215).
 *
 * Three pieces:
 *
 *   - `LegalHoldBanner` sits above a case's tabs while it is held, so a case
 *     worker whose Delete is refused has already read why.
 *   - `LegalHoldTab` sits on a case: what the hold freezes, the holds on it,
 *     and — for whoever the server says may — placing and releasing.
 *   - `LegalHoldsPanel` (Compliance → Legal Holds) lists every hold in the
 *     tenant by matter, and releases a whole matter at once.
 *
 * The server decides everything. `can_read` / `can_place` / `can_release` come
 * from the status route and are never inferred from a role name: since Phase 6
 * an operator may grant a privilege to another role in data. Anyone who can
 * read the case is told THAT it is held; the matter and the reason are sent
 * only to holders of `legal.holds.read`, so there is nothing here to hide.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";

import {
  getCaseLegalHold,
  listLegalHolds,
  placeLegalHold,
  releaseLegalHold,
  releaseLegalMatter,
  type CaseLegalHoldStatus,
  type LegalHold,
} from "@shared/api/client";
import {
  Banner, Button, Card, EmptyState, ErrorNote, FIELD_STYLE, MONO, Pill,
  SectionTitle, Spinner, TextInput,
} from "@shared/components";

const REASON_MIN = 10;
const muted: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)" };
const label: React.CSSProperties = {
  ...muted, fontFamily: "var(--font-mono)", textTransform: "uppercase",
  letterSpacing: "0.06em", fontSize: 11, marginBottom: 6,
};

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

// ── The case's status ────────────────────────────────────────────────────────

/**
 * Whether this case is held, and what the caller may do about it.
 *
 * `null` while loading or when the route cannot be read (an older server, a
 * case the caller may not read) — callers treat that as "show nothing", never
 * as "not held".
 */
export function useCaseLegalHold(caseId: string | null | undefined) {
  const [status, setStatus] = useState<CaseLegalHoldStatus | null>(null);
  const reload = useCallback(async () => {
    if (!caseId) { setStatus(null); return; }
    try { setStatus(await getCaseLegalHold(caseId)); }
    catch { setStatus(null); }
  }, [caseId]);
  useEffect(() => { setStatus(null); reload(); }, [reload]);
  return { status, reload };
}

export function LegalHoldBanner({ status }: { status: CaseLegalHoldStatus | null }) {
  if (!status?.held) return null;
  return (
    <Banner tone="warn" title="This case is under a legal hold">
      It can still be worked. It cannot be deleted, anonymized, erased or moved to
      another tenant until the hold is released.
    </Banner>
  );
}

// ── Releasing one hold ───────────────────────────────────────────────────────

function ReleaseForm({ what, onRelease }: {
  what: string; onRelease: (reason: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ok = reason.trim().length >= REASON_MIN;

  const submit = async () => {
    if (!window.confirm(
      `Release ${what}? Retention and erasure will apply to it again. This is recorded.`)) return;
    setBusy(true); setError(null);
    try { await onRelease(reason.trim()); setOpen(false); setReason(""); }
    catch (e) { setError(errText(e)); }
    finally { setBusy(false); }
  };

  if (!open) {
    return <Button variant="secondary" size="sm" onClick={() => setOpen(true)}>Release…</Button>;
  }
  return (
    <div style={{ display: "grid", gap: 8, marginTop: 8 }}>
      <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2}
                aria-label="Reason for release"
                placeholder="Why the hold is being lifted — at least 10 characters"
                style={{ ...FIELD_STYLE, width: "100%", resize: "vertical" }} />
      {error && <ErrorNote>{error}</ErrorNote>}
      {!ok && (
        <span style={muted}>
          The reason needs {REASON_MIN - reason.trim().length} more character(s).
        </span>
      )}
      <div style={{ display: "flex", gap: 8 }}>
        <Button variant="danger" size="sm" onClick={submit} disabled={!ok || busy}>
          {busy ? "Releasing…" : "Release hold"}
        </Button>
        <Button variant="ghost" size="sm" onClick={() => { setOpen(false); setError(null); }}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

function HoldCard({ hold, canRelease, onChanged, showCase }: {
  hold: LegalHold; canRelease: boolean; onChanged: () => void; showCase?: boolean;
}) {
  const active = hold.status === "active";
  return (
    <Card style={{ marginBottom: "var(--space-md)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <Pill tone={active ? "warn" : "muted"}>{active ? "active" : "released"}</Pill>
        <b style={{ color: "var(--text-primary)" }}>{hold.matter_ref}</b>
        {showCase && (
          <span style={{ ...MONO, color: "var(--text-muted)" }}>
            {hold.case_number ?? hold.case_id}
          </span>
        )}
      </div>
      <div style={{ marginTop: 8, fontSize: 13, color: "var(--text-primary)", whiteSpace: "pre-wrap" }}>
        {hold.reason}
      </div>
      <div style={{ ...muted, marginTop: 8 }}>
        Placed by <span style={MONO}>{hold.placed_by}</span> · {when(hold.placed_at)}
      </div>
      {!active && (
        <div style={{ ...muted, marginTop: 4 }}>
          Released by <span style={MONO}>{hold.released_by}</span> · {when(hold.released_at)}
          {hold.release_reason ? ` — ${hold.release_reason}` : ""}
        </div>
      )}
      {active && canRelease && (
        <div style={{ marginTop: 10 }}>
          <ReleaseForm what={`the hold for "${hold.matter_ref}"`} onRelease={async (reason) => {
            await releaseLegalHold(hold.id, reason);
            onChanged();
          }} />
        </div>
      )}
    </Card>
  );
}

// ── On a case ────────────────────────────────────────────────────────────────

export function LegalHoldTab({ caseId, status, onChanged }: {
  caseId: string; status: CaseLegalHoldStatus | null; onChanged: () => void;
}) {
  const [matter, setMatter] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!status) return <div style={muted}><Spinner size={12} /> Loading…</div>;

  const canSubmit = matter.trim().length > 0 && reason.trim().length >= REASON_MIN && !busy;
  const submit = async () => {
    setBusy(true); setError(null);
    try {
      const out = await placeLegalHold([caseId], matter.trim(), reason.trim());
      const first = out.results[0];
      if (!first?.ok) { setError(first?.error ?? "The hold was not placed."); return; }
      setMatter(""); setReason(""); onChanged();
    } catch (e) { setError(errText(e)); }
    finally { setBusy(false); }
  };

  const holds = status.holds ?? [];
  return (
    <div style={{ display: "grid", gap: "var(--space-md)", maxWidth: 720 }}>
      {status.held ? (
        <Banner tone="warn" title="Under a legal hold">
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {status.frozen.map((line) => <li key={line}>{line}</li>)}
          </ul>
        </Banner>
      ) : (
        <Banner tone="muted" title="Not under a legal hold">
          A hold freezes this case's records while a dispute or inquiry is open. The
          case can still be worked; it cannot be deleted, anonymized, erased or moved.
        </Banner>
      )}

      {!!status.other_active_holds && (
        <div style={muted}>
          {status.other_active_holds} more active hold(s) on this case belong to another
          tenant and are not shown.
        </div>
      )}

      {status.can_read && holds.length > 0 && (
        <div>
          <SectionTitle>Holds on this case</SectionTitle>
          {holds.map((h) => (
            <HoldCard key={h.id} hold={h} canRelease={status.can_release} onChanged={onChanged} />
          ))}
        </div>
      )}

      {status.can_place && status.held && (
        <div style={muted}>
          A case takes one legal hold at a time. Release this one before placing another.
        </div>
      )}

      {status.can_place && !status.held && (
        <div style={{ display: "grid", gap: 10 }}>
          <SectionTitle>Place a hold</SectionTitle>
          <div>
            <div style={label}>Matter — the lawsuit, inquiry or reference</div>
            <TextInput value={matter} onChange={(e) => setMatter(e.target.value)}
                       aria-label="Matter" maxLength={120}
                       placeholder="e.g. Claim 2026/114" style={{ width: "100%" }} />
          </div>
          <div>
            <div style={label}>Reason</div>
            <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={3}
                      aria-label="Reason" placeholder="At least 10 characters"
                      style={{ ...FIELD_STYLE, width: "100%", resize: "vertical" }} />
          </div>
          {error && <ErrorNote>{error}</ErrorNote>}
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <Button onClick={submit} disabled={!canSubmit}>
              {busy ? "Placing…" : "Place legal hold"}
            </Button>
            {!canSubmit && !busy && (
              <span style={muted}>
                {matter.trim().length === 0
                  ? "Enter the matter first."
                  : `The reason needs ${REASON_MIN - reason.trim().length} more character(s).`}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Compliance → Legal Holds ─────────────────────────────────────────────────

export default function LegalHoldsPanel() {
  const [holds, setHolds] = useState<LegalHold[] | null>(null);
  const [frozen, setFrozen] = useState<string[]>([]);
  const [canRelease, setCanRelease] = useState(false);
  const [show, setShow] = useState<"active" | "released">("active");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const out = await listLegalHolds({ status: show });
      setHolds(out.holds); setFrozen(out.frozen); setCanRelease(!!out.can_release);
    } catch (e) { setHolds([]); setError(errText(e)); }
  }, [show]);
  useEffect(() => { setHolds(null); load(); }, [load]);

  const matters = useMemo(() => {
    const by = new Map<string, LegalHold[]>();
    for (const h of holds ?? []) by.set(h.matter_ref, [...(by.get(h.matter_ref) ?? []), h]);
    return [...by.entries()];
  }, [holds]);

  return (
    <div style={{ maxWidth: 860 }}>
      <Banner tone="muted" title="Legal holds">
        A hold freezes a case's records while a dispute or inquiry is open. Place one
        from the case's <b>Legal hold</b> tab.
        {frozen.length > 0 && (
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {frozen.map((line) => <li key={line}>{line}</li>)}
          </ul>
        )}
      </Banner>

      <div style={{ display: "flex", gap: 8, margin: "var(--space-md) 0" }}>
        {(["active", "released"] as const).map((s) => (
          <Button key={s} size="sm" variant={show === s ? "primary" : "secondary"}
                  onClick={() => setShow(s)}>
            {s === "active" ? "Active" : "Released"}
          </Button>
        ))}
      </div>

      {error && <ErrorNote>{error}</ErrorNote>}
      {holds === null && <div style={muted}><Spinner size={12} /> Loading…</div>}
      {holds !== null && !error && holds.length === 0 && (
        <EmptyState title={show === "active" ? "No case is under a legal hold" : "No released holds"}
                    description={show === "active"
                      ? "Nothing in this tenant is frozen."
                      : "A released hold stays here as the record that it existed."} />
      )}

      {matters.map(([matter, rows]) => (
        <div key={matter} style={{ marginBottom: "var(--space-lg)" }}>
          <SectionTitle right={show === "active" && canRelease && rows.length > 1 ? (
            <ReleaseForm what={`all ${rows.length} holds for "${matter}"`}
                         onRelease={async (reason) => { await releaseLegalMatter(matter, reason); load(); }} />
          ) : undefined}>
            {matter} <span style={muted}>· {rows.length} case{rows.length === 1 ? "" : "s"}</span>
          </SectionTitle>
          {rows.map((h) => (
            <HoldCard key={h.id} hold={h} canRelease={canRelease} onChanged={load} showCase />
          ))}
        </div>
      ))}
    </div>
  );
}
