/* ═══════════════════════════════════════════════════════════════════
   VoiceFillPanel — voice input P3: speech becomes candidate field values.

   Speak (or pick a recorded session) and the local model proposes values
   for this form's fields. Every proposal is a DRAFT: it shows the exact
   words it came from, who said them, and how sure the model is, and it
   only reaches the form when a human clicks Accept. Nothing is written
   server-side by the extraction itself — accepting fills the form in
   front of you, and the form saves the way it always did.

   Renders NOTHING when the feature is off or the tenant has not opted
   into case AI — an absent capability is absent, never a dead control.
   ═══════════════════════════════════════════════════════════════════ */

import { useCallback, useEffect, useRef, useState } from "react";

import { apiFetch } from "@shared/api/client";

import VoiceInput from "./VoiceInput";

export interface VoiceFillCandidate {
  field_key: string;
  label: string;
  value: unknown;
  kind: string;
  quote: string;
  speaker: string | null;
  confidence: number;
}

interface VoiceFillStatus {
  available: boolean;
  egress: string;
}

interface SealedSession {
  id: string;
  title: string | null;
  transcript_status: string | null;
}

/** Accept-all only fires on proposals this confident. */
const HIGH_CONFIDENCE = 0.8;

let statusProbe: Promise<VoiceFillStatus | null> | null = null;

function getVoiceFillStatus(): Promise<VoiceFillStatus | null> {
  if (!statusProbe) {
    statusProbe = apiFetch("/voicefill/status")
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null);
  }
  return statusProbe;
}

function formatValue(value: unknown): string {
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) return value.join(", ");
  return String(value ?? "");
}

export interface VoiceFillPanelProps {
  caseId: string;
  /** Scopes the proposals to this form's fields. */
  formId?: string;
  /** Fill one field. Called once per accepted proposal. */
  onAccept: (fieldKey: string, value: unknown) => void;
  disabled?: boolean;
}

export default function VoiceFillPanel({
  caseId, formId, onAccept, disabled,
}: VoiceFillPanelProps) {
  const [status, setStatus] = useState<VoiceFillStatus | null>(null);
  const [sessions, setSessions] = useState<SealedSession[]>([]);
  const [candidates, setCandidates] = useState<VoiceFillCandidate[]>([]);
  const [accepted, setAccepted] = useState<Record<string, true>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ran, setRan] = useState(false);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    getVoiceFillStatus().then((s) => { if (aliveRef.current) setStatus(s); });
    return () => { aliveRef.current = false; };
  }, []);

  // Sealed transcripts are the other input — offer them only if any exist.
  useEffect(() => {
    if (!status?.available || !caseId) return;
    apiFetch(`/meet/cases/${caseId}/sessions`)
      .then((r) => (r.ok ? r.json() : []))
      .then((rows: SealedSession[]) => {
        if (!aliveRef.current) return;
        setSessions((Array.isArray(rows) ? rows : [])
          .filter((s) => s.transcript_status === "sealed"));
      })
      .catch(() => undefined);
  }, [status, caseId]);

  const run = useCallback(async (body: { text?: string; session_id?: string }) => {
    setBusy(true);
    setError(null);
    try {
      const res = await apiFetch(`/voicefill/cases/${caseId}/extract`, {
        method: "POST",
        body: JSON.stringify({ ...body, form_id: formId }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(payload.detail || `Extraction failed (${res.status})`);
      if (!aliveRef.current) return;
      setCandidates(payload.candidates || []);
      setAccepted({});
      setRan(true);
    } catch (e: any) {
      if (aliveRef.current) setError(e?.message || "Extraction failed");
    } finally {
      if (aliveRef.current) setBusy(false);
    }
  }, [caseId, formId]);

  const accept = useCallback((c: VoiceFillCandidate) => {
    onAccept(c.field_key, c.value);
    setAccepted((prev) => ({ ...prev, [c.field_key]: true }));
  }, [onAccept]);

  const dismiss = useCallback((fieldKey: string) => {
    setCandidates((prev) => prev.filter((c) => c.field_key !== fieldKey));
  }, []);

  if (!status?.available) return null;

  const pending = candidates.filter((c) => !accepted[c.field_key]);
  const confident = pending.filter((c) => c.confidence >= HIGH_CONFIDENCE);

  return (
    <div style={{
      border: "1px solid var(--border-subtle)",
      borderRadius: "var(--radius-md, 8px)",
      background: "var(--bg-elevated, var(--bg-panel))",
      padding: 12,
      marginBottom: 16,
      display: "flex",
      flexDirection: "column",
      gap: 10,
    }}>
      {/* Controls */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)" }}>
          ✦ Fill by voice
        </span>
        <VoiceInput
          caseId={caseId}
          disabled={disabled || busy}
          size={30}
          title="Dictate the details to fill this form"
          onText={(text) => void run({ text })}
        />
        {sessions.length > 0 && (
          <select
            value=""
            disabled={disabled || busy}
            onChange={(e) => { if (e.target.value) void run({ session_id: e.target.value }); }}
            style={{
              fontSize: 11, padding: "5px 8px", borderRadius: "var(--radius-sm, 6px)",
              border: "1px solid var(--border-default)", background: "var(--bg-input)",
              color: "var(--text-primary)", maxWidth: 240,
            }}
          >
            <option value="">…or from a recorded session</option>
            {sessions.map((s) => (
              <option key={s.id} value={s.id}>{s.title || `Session ${s.id.slice(0, 8)}`}</option>
            ))}
          </select>
        )}
        {busy && <span style={{ fontSize: 11, color: "var(--text-muted)" }}>Reading…</span>}
        {pending.length > 1 && (
          <button
            type="button"
            disabled={confident.length === 0}
            onClick={() => confident.forEach(accept)}
            style={{
              marginLeft: "auto", fontSize: 11, padding: "5px 10px",
              borderRadius: "var(--radius-sm, 6px)", cursor: confident.length ? "pointer" : "default",
              border: "1px solid var(--border-default)", background: "var(--bg-panel)",
              color: "var(--text-secondary)", opacity: confident.length ? 1 : 0.5,
            }}
          >
            Accept {confident.length} confident
          </button>
        )}
      </div>

      <div style={{ fontSize: 11, color: "var(--text-muted)", lineHeight: 1.5 }}>
        Suggestions only — nothing is saved until you accept a value and submit the form.
        {status.egress === "external_allowed"
          ? " Your tenant allows external AI processing for case content."
          : " Processed locally on your server."}
      </div>

      {error && <div style={{ fontSize: 12, color: "var(--status-failed)" }}>{error}</div>}

      {ran && !busy && candidates.length === 0 && !error && (
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
          Nothing in that recording matched a field on this form.
        </div>
      )}

      {/* Proposals — each shows the words it came from */}
      {candidates.map((c) => {
        const isAccepted = !!accepted[c.field_key];
        return (
          <div key={c.field_key} style={{
            display: "flex", gap: 10, alignItems: "flex-start",
            padding: 10, borderRadius: "var(--radius-sm, 6px)",
            border: "1px solid var(--border-subtle)",
            background: isAccepted ? "var(--accent-dim, var(--bg-panel))" : "var(--bg-panel)",
            opacity: isAccepted ? 0.65 : 1,
          }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: "flex", gap: 6, alignItems: "baseline", flexWrap: "wrap" }}>
                <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{c.label}</span>
                <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>
                  {formatValue(c.value)}
                </span>
                <span title={`Model confidence ${Math.round(c.confidence * 100)}%`} style={{
                  fontSize: 10, padding: "1px 6px", borderRadius: 8,
                  border: "1px solid var(--border-subtle)",
                  color: c.confidence >= HIGH_CONFIDENCE ? "var(--accent)" : "var(--text-muted)",
                  fontFamily: "var(--font-mono)",
                }}>
                  {Math.round(c.confidence * 100)}%
                </span>
                {c.speaker && (
                  <span style={{
                    fontSize: 10, padding: "1px 6px", borderRadius: 8,
                    background: "var(--bg-secondary, transparent)",
                    border: "1px solid var(--border-subtle)", color: "var(--text-muted)",
                  }}>
                    {c.speaker}
                  </span>
                )}
              </div>
              <div style={{
                fontSize: 11, color: "var(--text-muted)", marginTop: 3,
                fontStyle: "italic", overflowWrap: "anywhere",
              }}>
                “{c.quote}”
              </div>
            </div>
            {isAccepted ? (
              <span style={{ fontSize: 11, color: "var(--accent)", whiteSpace: "nowrap" }}>
                ✓ filled
              </span>
            ) : (
              <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
                <button type="button" onClick={() => accept(c)} disabled={disabled}
                  style={{
                    fontSize: 11, padding: "4px 10px", borderRadius: "var(--radius-sm, 6px)",
                    border: "none", background: "var(--accent)", color: "#fff",
                    cursor: "pointer", fontWeight: 600,
                  }}>
                  Accept
                </button>
                <button type="button" onClick={() => dismiss(c.field_key)}
                  style={{
                    fontSize: 11, padding: "4px 8px", borderRadius: "var(--radius-sm, 6px)",
                    border: "1px solid var(--border-default)", background: "transparent",
                    color: "var(--text-muted)", cursor: "pointer",
                  }}>
                  Dismiss
                </button>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
