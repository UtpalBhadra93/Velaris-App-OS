/**
 * Voice input P3b — the customer-portal mic and form-fill panel.
 *
 * Not a reuse of Studio's `VoiceInput`/`VoiceFillPanel`: those authenticate as
 * a worker (`apiFetch` + `/stt/status`) and inherit Studio's visual system.
 * This one speaks the portal's API (customer JWT), wears the portal's card
 * vocabulary, and is built for a phone — because the portal's real second home
 * is an installed PWA on someone's home screen.
 *
 * What that costs, concretely:
 *  - the capability probe has a browser half as well as a server half, and an
 *    insecure origin says so out loud instead of vanishing;
 *  - getUserMedia failures are recoverable prompts, never a dead button;
 *  - offline hides the mic rather than queueing audio — a queued clip would be
 *    the customer's voice at rest on their device, which is the one thing
 *    transcribe-and-discard promises never happens;
 *  - touch targets are >=44px and the cards stack on a narrow screen.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useEffect, useRef, useState } from "react";
import { PcmRecorder, micErrorMessage, micSupport } from "@shared/audio/pcm";
import {
  getVoiceStatus, transcribeVoice, voiceFillAction,
  type CaseAction, type PortalVoiceStatus, type VoiceCandidate,
} from "./portalApi";
import { C } from "./portalUi";

const consentKey = (slug: string, caseId: string) => `velaris_voice_ok_${slug}_${caseId}`;

/** Fold a transcript into whatever is already typed — dictation adds, never eats. */
export function appendSpoken(prev: string, text: string): string {
  const spoken = (text || "").trim();
  if (!spoken) return prev;
  if (!prev.trim()) return spoken;
  return /[\s]$/.test(prev) ? prev + spoken : `${prev} ${spoken}`;
}

/** One shared status probe per page load, however many cards are on screen. */
let _statusPromise: Promise<PortalVoiceStatus | null> | null = null;
function probeStatus(slug: string): Promise<PortalVoiceStatus | null> {
  if (!_statusPromise) _statusPromise = getVoiceStatus(slug).catch(() => null);
  return _statusPromise;
}

function useOnline(): boolean {
  const [online, setOnline] = useState(() =>
    typeof navigator === "undefined" ? true : navigator.onLine);
  useEffect(() => {
    const up = () => setOnline(true);
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => { window.removeEventListener("online", up); window.removeEventListener("offline", down); };
  }, []);
  return online;
}

function useNarrow(): boolean {
  const [narrow, setNarrow] = useState(() =>
    typeof window !== "undefined" && window.innerWidth <= 420);
  useEffect(() => {
    const onResize = () => setNarrow(window.innerWidth <= 420);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return narrow;
}

const note: React.CSSProperties = { fontSize: 12, color: "#6b7280", lineHeight: 1.5 };

type Props = {
  slug: string;
  caseId: string;
  action: CaseAction;
  brand: string;
  /** Called for every accepted candidate: the card's own local state. */
  onAccept: (fieldKey: string, value: string) => void;
  /** Approval cards have no fields — the transcript goes to the comment box. */
  onDictate?: (text: string) => void;
};

export default function PortalVoice({
  slug, caseId, action, brand, onAccept, onDictate,
}: Props) {
  const [status, setStatus] = useState<PortalVoiceStatus | null>(null);
  const [support] = useState(micSupport);
  const [asked, setAsked] = useState(false);
  // A ref, not state: `finish()` may run from a callback captured at the start
  // of the clip, and it must see the acknowledgement made a moment earlier.
  // localStorage only suppresses re-asking on this device — an installed PWA
  // has its own storage on iOS, so re-prompting after install is expected and
  // harmless. The server's audit row is the record that counts.
  const consentedRef = useRef(localStorage.getItem(consentKey(slug, caseId)) === "1");
  /** Set when the customer taps Allow; consumed by the next transcribe, which
   *  carries `consent=true` so the server writes the acknowledgement row. */
  const sendConsentRef = useRef(false);
  const [showConsent, setShowConsent] = useState(false);
  const [phase, setPhase] = useState<"idle" | "recording" | "uploading" | "thinking">("idle");
  const [level, setLevel] = useState(0);
  const [secs, setSecs] = useState(0);
  const [err, setErr] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<VoiceCandidate[]>([]);
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const recorder = useRef<PcmRecorder | null>(null);
  const online = useOnline();
  const narrow = useNarrow();

  useEffect(() => {
    let live = true;
    probeStatus(slug).then(s => { if (live) { setStatus(s); setAsked(true); } });
    return () => { live = false; };
  }, [slug]);

  // The mic must never outlive the card that owns it.
  useEffect(() => () => { recorder.current?.cancel(); recorder.current = null; }, []);

  // Dropping off the network mid-clip swaps this whole control for the offline
  // note, so nothing would ever call stop() — release the mic here instead of
  // leaving it live behind a message that says voice is unavailable.
  useEffect(() => {
    if (online) return;
    recorder.current?.cancel();
    recorder.current = null;
    setPhase("idle");
    setLevel(0);
  }, [online]);

  useEffect(() => {
    if (phase !== "recording") return;
    const t = setInterval(() => setSecs(recorder.current?.seconds ?? 0), 250);
    return () => clearInterval(t);
  }, [phase]);

  if (!asked || !status?.available) return null;
  // A browser that cannot capture at all: the capability is genuinely absent.
  if (support === "unsupported") return null;

  // ...but an insecure origin is a deployment fact, not a missing feature.
  // Saying nothing here just produces a support ticket.
  if (support === "insecure") {
    return (
      <div style={{ ...note, marginBottom: 12 }}>
        🎙 Voice input needs a secure (https) connection.
      </div>
    );
  }

  if (!online) {
    return (
      <div style={{ ...note, marginBottom: 12 }}>
        🎙 Voice input needs a connection — you can still type your answer.
      </div>
    );
  }

  const canFill = status.fill_available && action.type === "form"
    && (action.form_fields?.length ?? 0) > 0;

  const send = async (pcm: ArrayBuffer, firstTime: boolean) => {
    setPhase("uploading");
    try {
      const { text } = await transcribeVoice(slug, caseId, pcm, firstTime);
      const spoken = (text || "").trim();
      if (!spoken) { setErr("We didn't catch that — please try again."); return; }
      if (!canFill) { onDictate?.(spoken); return; }
      setPhase("thinking");
      const r = await voiceFillAction(slug, caseId, action.step_id, spoken);
      if (!r.candidates.length) {
        setErr("Nothing in that matched this form — try naming each answer, or type it in.");
        return;
      }
      setCandidates(r.candidates);
      setDismissed(new Set());
    } catch (e: any) {
      setErr(e?.message || "Something went wrong — please try again.");
    } finally {
      setPhase(p => (p === "uploading" || p === "thinking" ? "idle" : p));
      setSecs(0);
    }
  };

  const finish = async () => {
    const rec = recorder.current;
    recorder.current = null;
    if (!rec) { setPhase("idle"); return; }
    const pcm = rec.stop();
    setLevel(0);
    setPhase("idle");
    if (pcm.byteLength < 16000 * 2 * 0.2) {
      setErr("That was too short — hold on a moment longer.");
      setSecs(0);
      return;
    }
    const firstTime = sendConsentRef.current;
    sendConsentRef.current = false;
    await send(pcm, firstTime);
  };

  const startRecording = async () => {
    try {
      recorder.current = await PcmRecorder.start({
        maxSeconds: status.max_clip_seconds,
        onLevel: setLevel,
        onLimit: () => { void finish(); },
        onInterrupted: () => { void finish(); },
      });
      setSecs(0);
      setPhase("recording");
    } catch (e) {
      setErr(micErrorMessage(e));
    }
  };

  const begin = () => {
    setErr(null);
    setCandidates([]);
    if (!consentedRef.current) { setShowConsent(true); return; }
    void startRecording();
  };

  const toggle = () => (phase === "recording" ? void finish() : begin());

  const accept = (c: VoiceCandidate) => {
    onAccept(c.field_key, c.value == null ? "" : String(c.value));
    setDismissed(d => new Set(d).add(c.field_key));
  };

  const visible = candidates.filter(c => !dismissed.has(c.field_key));
  const confident = visible.filter(c => c.confidence >= 0.8);
  const busy = phase === "uploading" || phase === "thinking";

  return (
    <div style={{ marginBottom: 14 }}>
      {showConsent ? (
        <div style={{ background: "#f9fafb", border: "1px solid #e5e7eb", borderRadius: 10,
                      padding: 14, marginBottom: 10 }}>
          <div style={{ fontWeight: 700, fontSize: 13, color: "#111827", marginBottom: 6 }}>
            Answer by voice?
          </div>
          <div style={{ ...note, marginBottom: 12 }}>{status.consent_text}</div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button
              onClick={() => {
                setShowConsent(false);
                localStorage.setItem(consentKey(slug, caseId), "1");
                consentedRef.current = true;
                sendConsentRef.current = true;
                void startRecording();
              }}
              style={{ ...C.primary(brand), minHeight: 44, flex: narrow ? "1 1 100%" : undefined,
                       justifyContent: "center" }}
            >
              Allow microphone
            </button>
            <button onClick={() => setShowConsent(false)}
                    style={{ ...C.secondary, minHeight: 44,
                             flex: narrow ? "1 1 100%" : undefined, justifyContent: "center" }}>
              Not now
            </button>
          </div>
        </div>
      ) : (
        <button
          onClick={toggle}
          disabled={busy}
          aria-label={phase === "recording" ? "Stop recording" : "Answer by voice"}
          style={{
            display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 8,
            minHeight: 44, minWidth: 44, padding: "0 16px", borderRadius: 22,
            border: `1px solid ${phase === "recording" ? "#ef4444" : "#d1d5db"}`,
            background: phase === "recording" ? "#fef2f2" : "#fff",
            color: phase === "recording" ? "#b91c1c" : "#374151",
            fontWeight: 700, fontSize: 13, cursor: busy ? "default" : "pointer",
            opacity: busy ? 0.65 : 1,
            boxShadow: phase === "recording"
              ? `0 0 0 ${2 + Math.round(level / 12)}px #ef444422` : "none",
            transition: "box-shadow 0.12s linear",
          }}
        >
          <span style={{ fontSize: 16 }}>{phase === "recording" ? "⏹" : "🎙"}</span>
          {phase === "recording"
            ? `Stop (${Math.floor(secs)}s / ${status.max_clip_seconds}s)`
            : phase === "uploading" ? "Uploading…"
            : phase === "thinking" ? "Reading what you said…"
            : canFill ? "Answer by voice" : "Speak instead of typing"}
        </button>
      )}

      {phase === "recording" && status.realtime === false && (
        <div style={{ ...note, marginTop: 8 }}>Transcribing may take a few seconds.</div>
      )}
      {err && <div style={{ ...C.err, marginTop: 10, marginBottom: 0 }}>{err}</div>}

      {visible.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center",
                        gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
            <div style={C.sectionLabel}>From what you said</div>
            {confident.length > 1 && (
              <button
                onClick={() => confident.forEach(accept)}
                style={{ ...C.ghost, color: brand, fontWeight: 700, minHeight: 44 }}
              >
                Use all {confident.length}
              </button>
            )}
          </div>

          {visible.map(c => (
            <div key={c.field_key}
                 style={{ border: "1px solid #e5e7eb", borderRadius: 10, padding: 12,
                          marginBottom: 8, background: "#fff" }}>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "baseline",
                            marginBottom: 4 }}>
                <span style={{ fontSize: 11, fontWeight: 700, color: "#6b7280",
                               textTransform: "uppercase", letterSpacing: "0.05em" }}>
                  {c.label}
                </span>
                <span style={{ fontSize: 11, color: "#9ca3af" }}>
                  {Math.round(c.confidence * 100)}% sure
                </span>
              </div>
              <div style={{ fontSize: 15, fontWeight: 700, color: "#111827", marginBottom: 6,
                            wordBreak: "break-word" }}>
                {String(c.value)}
              </div>
              <div style={{ ...note, fontStyle: "italic", marginBottom: 10 }}>“{c.quote}”</div>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <button onClick={() => accept(c)}
                        style={{ ...C.primary(brand), minHeight: 44,
                                 flex: narrow ? "1 1 100%" : undefined, justifyContent: "center" }}>
                  Use this
                </button>
                <button onClick={() => setDismissed(d => new Set(d).add(c.field_key))}
                        style={{ ...C.secondary, minHeight: 44,
                                 flex: narrow ? "1 1 100%" : undefined, justifyContent: "center" }}>
                  Discard
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
