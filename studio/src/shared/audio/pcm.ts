/* ═══════════════════════════════════════════════════════════════════
   Shared PCM16 mic capture — the browser half of Velaris speech input.

   One format across the platform: little-endian int16 mono 16 kHz PCM.
   HxMeet live captions stream it over a WebSocket; voice input POSTs one
   clip of it to /api/v1/stt/transcribe. The server side (meet/asr.py)
   auto-selects whisper.cpp on AMD/Intel GPU boxes, and that engine takes
   raw samples only — so PCM, never a MediaRecorder container.
   ═══════════════════════════════════════════════════════════════════ */

/** Worklet that forwards each render quantum of mic samples to the main thread. */
export const WORKLET_SRC = `class VxPcm extends AudioWorkletProcessor {
  process(inputs){ const c = inputs[0] && inputs[0][0]; if (c) this.port.postMessage(c.slice(0)); return true; }
}
registerProcessor("vx-pcm", VxPcm);`;

export const SAMPLE_RATE = 16000;

/** Linear-interpolation downsample to 16 kHz int16 — safe on any context rate. */
export function toPcm16k(samples: Float32Array, fromRate: number): Int16Array {
  const ratio = fromRate / SAMPLE_RATE;
  const out = new Int16Array(Math.floor(samples.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const pos = i * ratio;
    const i0 = Math.floor(pos);
    const frac = pos - i0;
    const s = samples[i0] * (1 - frac) + (samples[Math.min(i0 + 1, samples.length - 1)] || 0) * frac;
    out[i] = Math.max(-32768, Math.min(32767, Math.round(s * 32767)));
  }
  return out;
}

export interface PcmRecorderOptions {
  /** 0-100 mic level, ~4x/second — for the "we're listening" affordance. */
  onLevel?: (level: number) => void;
  /** Hard cap; the recorder stops itself and calls onLimit. Server caps too. */
  maxSeconds?: number;
  onLimit?: () => void;
  /**
   * The tab/app went to the background mid-clip. On a phone that suspends the
   * audio graph, so what continues is silence with the mic indicator still
   * lit — the recorder stops itself and hands the caller what it had.
   */
  onInterrupted?: () => void;
}

/** Why the browser cannot capture — the mic UI decides what to show from this. */
export type MicSupport = "ok" | "insecure" | "unsupported";

/**
 * Can this browser capture at all, before we ever ask for permission?
 *
 * `navigator.mediaDevices` is **undefined** on a non-secure origin, so a portal
 * served over plain http works on desktop-localhost and fails on every phone.
 * That is a deployment problem worth naming, not a missing feature — hence a
 * distinct `insecure` answer rather than a blanket "unsupported".
 */
export function micSupport(): MicSupport {
  if (typeof window === "undefined") return "unsupported";
  const secure = window.isSecureContext ?? location.protocol === "https:";
  if (!navigator.mediaDevices?.getUserMedia) return secure ? "unsupported" : "insecure";
  // AudioWorklet lands in iOS 14.5; older phones and some WebView shells lack it.
  if (typeof AudioContext === "undefined" || !AudioContext.prototype.audioWorklet) {
    return "unsupported";
  }
  return "ok";
}

/** A getUserMedia rejection turned into something a person can act on. */
export function micErrorMessage(e: unknown): string {
  const name = (e as { name?: string })?.name || "";
  if (name === "NotAllowedError" || name === "SecurityError") {
    return "Microphone access was blocked — allow it in your browser settings and tap again.";
  }
  if (name === "NotFoundError" || name === "OverconstrainedError") {
    return "No microphone was found on this device.";
  }
  if (name === "NotReadableError" || name === "AbortError") {
    return "Your microphone is in use by another app.";
  }
  return "Could not start the microphone.";
}

/**
 * Push-to-talk recorder: owns its own getUserMedia stream, accumulates PCM16
 * until `stop()`, and releases the mic on every exit path (stop OR cancel).
 * A dictation clip is short-lived — it exists only until the POST returns.
 */
export class PcmRecorder {
  private ctx: AudioContext | null = null;
  private node: AudioWorkletNode | null = null;
  private src: MediaStreamAudioSourceNode | null = null;
  private stream: MediaStream | null = null;
  private workletUrl: string | null = null;
  private chunks: Int16Array[] = [];
  private total = 0;
  private stopped = false;
  private onHide: (() => void) | null = null;

  private constructor(private opts: PcmRecorderOptions) {}

  static async start(opts: PcmRecorderOptions = {}): Promise<PcmRecorder> {
    const rec = new PcmRecorder(opts);
    try {
      await rec.open();
    } catch (e) {
      rec.release();
      throw e;
    }
    return rec;
  }

  private async open() {
    // Constructed BEFORE the await: on iOS an AudioContext created after one
    // can land outside the user gesture and come up permanently suspended.
    this.ctx = new AudioContext();
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1, echoCancellation: true, noiseSuppression: true,
        // A phone held at arm's length produces a quiet clip, and Whisper's
        // accuracy drops materially on low amplitude.
        autoGainControl: true,
      },
    });
    this.workletUrl = URL.createObjectURL(new Blob([WORKLET_SRC], { type: "application/javascript" }));
    await this.ctx.audioWorklet.addModule(this.workletUrl);
    if (this.ctx.state === "suspended") await this.ctx.resume();  // autoplay policy
    this.src = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, "vx-pcm");
    this.src.connect(this.node);
    // The worklet only runs while it is reachable from the destination; its
    // outputs stay silent (process() writes nothing), so this is inaudible.
    this.node.connect(this.ctx.destination);

    // Backgrounding the tab (a call, a notification, the home button) suspends
    // the graph, so anything "recorded" from here on is silence. Stop instead —
    // honest for the caller, and we never hold the mic open out of view.
    this.onHide = () => {
      if (document.visibilityState === "hidden" && !this.stopped) {
        this.stopped = true;
        this.opts.onInterrupted?.();
      }
    };
    document.addEventListener("visibilitychange", this.onHide);

    const rate = this.ctx.sampleRate;
    const maxSamples = this.opts.maxSeconds ? this.opts.maxSeconds * SAMPLE_RATE : Infinity;
    let pending: Float32Array[] = [];
    let pendingLen = 0;

    this.node.port.onmessage = (e: MessageEvent<Float32Array>) => {
      if (this.stopped) return;
      pending.push(e.data);
      pendingLen += e.data.length;
      if (pendingLen < rate / 4) return;                 // ~250 ms batches

      const all = new Float32Array(pendingLen);
      let off = 0;
      for (const c of pending) { all.set(c, off); off += c.length; }
      pending = []; pendingLen = 0;

      if (this.opts.onLevel) {
        let sum = 0;
        for (let i = 0; i < all.length; i += 4) sum += all[i] * all[i];
        this.opts.onLevel(Math.min(100, Math.round(Math.sqrt(sum / (all.length / 4)) * 300)));
      }

      const pcm = toPcm16k(all, rate);
      this.chunks.push(pcm);
      this.total += pcm.length;
      if (this.total >= maxSamples) {
        this.stopped = true;
        this.opts.onLimit?.();
      }
    };
  }

  /** True once the clip hit `maxSeconds` — the caller should send it. */
  get atLimit(): boolean {
    return this.stopped;
  }

  get seconds(): number {
    return this.total / SAMPLE_RATE;
  }

  /** Stop capture, release the mic, and return the clip as raw bytes. */
  stop(): ArrayBuffer {
    this.stopped = true;
    this.release();
    const out = new Int16Array(this.total);
    let off = 0;
    for (const c of this.chunks) { out.set(c, off); off += c.length; }
    this.chunks = [];
    return out.buffer;
  }

  /** Abandon the clip and release the mic. */
  cancel(): void {
    this.stopped = true;
    this.chunks = [];
    this.total = 0;
    this.release();
  }

  private release() {
    if (this.onHide) {
      document.removeEventListener("visibilitychange", this.onHide);
      this.onHide = null;
    }
    try { this.node?.port.close(); this.node?.disconnect(); } catch { /* already gone */ }
    try { this.src?.disconnect(); } catch { /* already gone */ }
    try { this.ctx?.close(); } catch { /* already gone */ }
    this.stream?.getTracks().forEach((t) => t.stop());
    if (this.workletUrl) URL.revokeObjectURL(this.workletUrl);
    this.node = null; this.src = null; this.ctx = null;
    this.stream = null; this.workletUrl = null;
  }
}
