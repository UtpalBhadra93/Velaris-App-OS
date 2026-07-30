/* ═══════════════════════════════════════════════════════════════════
   VoiceInput — the platform-wide dictation mic.

   Click to speak, click to stop; the clip goes to case-service, local
   Whisper transcribes it, and the text is handed back to the caller.
   Audio never leaves the platform and nothing is stored server-side.

   The button renders NOTHING when the voice_input release flag is off or
   no ASR engine is installed on the box — an absent capability is absent,
   never a dead control. The status probe runs once per page load and is
   shared by every mic on screen.
   ═══════════════════════════════════════════════════════════════════ */

import { useCallback, useEffect, useRef, useState } from "react";

import { apiFetch } from "@shared/api/client";
import { PcmRecorder } from "@shared/audio/pcm";

interface SttStatus {
  available: boolean;
  engine: string | null;
  device: string | null;
  model: string | null;
  realtime: boolean | null;
  max_clip_seconds: number;
}

let statusProbe: Promise<SttStatus | null> | null = null;

/** One probe per page load, shared by every mic. */
function getSttStatus(): Promise<SttStatus | null> {
  if (!statusProbe) {
    statusProbe = apiFetch("/stt/status")
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null);
  }
  return statusProbe;
}

/**
 * Fold a transcript into whatever is already typed. Dictation adds to the
 * question, it never silently eats it — someone who typed half a sentence and
 * then reached for the mic meant both halves.
 */
export function appendTranscript(prev: string, text: string): string {
  const head = prev.trimEnd();
  return head ? `${head} ${text}` : text;
}

export interface VoiceInputProps {
  /** Receives the transcript. Append to the field rather than replacing it. */
  onText: (text: string) => void;
  /** Dictating into a case surface — sends the clip through the case-level PDP. */
  caseId?: string;
  disabled?: boolean;
  /** Match the host control's height (px). */
  size?: number;
  title?: string;
}

export default function VoiceInput({
  onText, caseId, disabled, size = 30, title = "Dictate",
}: VoiceInputProps) {
  const [status, setStatus] = useState<SttStatus | null>(null);
  const [recording, setRecording] = useState(false);
  const [busy, setBusy] = useState(false);
  const [level, setLevel] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const recRef = useRef<PcmRecorder | null>(null);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    getSttStatus().then((s) => { if (aliveRef.current) setStatus(s); });
    return () => {
      aliveRef.current = false;
      recRef.current?.cancel();      // navigating away must release the mic
      recRef.current = null;
    };
  }, []);

  const send = useCallback(async (clip: ArrayBuffer) => {
    if (clip.byteLength === 0) return;
    setBusy(true);
    setError(null);
    try {
      const res = await apiFetch(`/stt/transcribe${caseId ? `?case_id=${caseId}` : ""}`, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: clip,
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Transcription failed (${res.status})`);
      }
      const { text } = await res.json();
      if (text) onText(text);
      else setError("Nothing was picked up — try again closer to the mic");
    } catch (e: any) {
      setError(e?.message || "Transcription failed");
    } finally {
      if (aliveRef.current) setBusy(false);
    }
  }, [caseId, onText]);

  const stop = useCallback(() => {
    const rec = recRef.current;
    recRef.current = null;
    setRecording(false);
    setLevel(0);
    if (rec) void send(rec.stop());
  }, [send]);

  const start = useCallback(async () => {
    setError(null);
    try {
      recRef.current = await PcmRecorder.start({
        onLevel: (l) => { if (aliveRef.current) setLevel(l); },
        maxSeconds: status?.max_clip_seconds ?? 60,
        onLimit: () => { if (aliveRef.current) stop(); },
      });
      setRecording(true);
    } catch (e: any) {
      setError(e?.name === "NotAllowedError"
        ? "Microphone permission was denied"
        : `Microphone unavailable: ${e?.message || e}`);
    }
  }, [status, stop]);

  // Absent capability = absent control, never a dead one.
  if (!status?.available) return null;

  const lagMode = status.realtime === false;
  const tip = error
    || (recording ? "Listening — click to transcribe" : busy ? "Transcribing…" : title)
    + (!recording && !busy && lagMode ? " (no GPU on this server — expect a wait)" : "");

  return (
    <button
      type="button"
      onClick={() => (recording ? stop() : void start())}
      disabled={disabled || busy}
      title={tip}
      aria-label={tip}
      aria-pressed={recording}
      style={{
        width: size,
        height: size,
        flex: `0 0 ${size}px`,
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        borderRadius: 6,
        cursor: disabled || busy ? "default" : "pointer",
        background: recording
          ? "color-mix(in srgb, var(--danger, #e5484d) 18%, var(--bg-panel))"
          : "var(--bg-panel)",
        border: `1px solid ${recording
          ? "var(--danger, #e5484d)"
          : error ? "var(--danger, #e5484d)" : "var(--border-default)"}`,
        color: recording ? "var(--danger, #e5484d)" : "var(--text-muted)",
        opacity: busy ? 0.6 : 1,
        padding: 0,
        transition: "background 120ms, border-color 120ms",
        // While recording the ring tracks the mic level, so a dead mic is visible.
        boxShadow: recording
          ? `0 0 0 ${1 + Math.round(level / 25)}px color-mix(in srgb, var(--danger, #e5484d) 22%, transparent)`
          : "none",
      }}
    >
      <svg width={Math.round(size * 0.55)} height={Math.round(size * 0.55)}
           viewBox="0 0 24 24" fill="none" stroke="currentColor"
           strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
        {busy ? (
          <>
            <path d="M12 3a9 9 0 1 0 9 9" />
          </>
        ) : (
          <>
            <rect x="9" y="2" width="6" height="11" rx="3" />
            <path d="M5 10v1a7 7 0 0 0 14 0v-1" />
            <line x1="12" y1="18" x2="12" y2="22" />
          </>
        )}
      </svg>
    </button>
  );
}
