/**
 * Portal v2 P2 — offline submission queue + PWA registration.
 *
 * Submissions made while offline land in IndexedDB with a client-generated
 * UUID (client_ref). The queue flushes automatically: on page load, on the
 * `online` event, and — where the browser supports Background Sync — from
 * the service worker even after the tab closes. The server dedupes on
 * client_ref, so any number of replays resolve to one case.
 *
 * Honesty rules: drafts are device-local only, and OTP login cannot happen
 * offline — the queue serves anonymous and already-logged-in submits.
 */

const DB_NAME = "velaris-portal";
const STORE = "pending_submissions";
const EDIT_STORE = "pending_edits";

// ── CRDT clock (#38) ────────────────────────────────────────────────
// Offline devices cannot agree on wall time, so edits are ordered by a
// Lamport counter plus a stable per-device origin. The server merges on
// (lamport, origin) — never on a timestamp — which is what makes two devices
// converge on the same value instead of the loser's edit vanishing.
const ORIGIN_KEY = "velaris-crdt-origin";
const LAMPORT_KEY = "velaris-crdt-lamport";

/** Stable per-device id. Generated once; ties break deterministically on it. */
export function deviceOrigin(): string {
  let o = localStorage.getItem(ORIGIN_KEY);
  if (!o) {
    o = crypto.randomUUID().slice(0, 16);
    localStorage.setItem(ORIGIN_KEY, o);
  }
  return o;
}

/** Lamport's rule: max(local, received) + 1. Survives a week offline. */
export function tickLamport(received = 0): number {
  const local = Number(localStorage.getItem(LAMPORT_KEY) ?? 0);
  const next = Math.max(local, received) + 1;
  localStorage.setItem(LAMPORT_KEY, String(next));
  return next;
}

export type PendingSubmission = {
  client_ref: string;
  slug: string;
  payload: {
    case_type_id: string; submitter_name: string; submitter_email: string;
    subject: string; description: string; priority: string;
  };
  saved_at: string;
};

export type PendingEdit = {
  id: string;            // `${case_id}:${key}` — one pending edit per variable
  case_id: string;
  key: string;
  value: unknown;
  lamport: number;
  origin: string;
  saved_at: string;
};

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    // v2 adds the pending_edits store (#38). The version bump is what makes
    // onupgradeneeded fire for devices that already hold a v1 database — a
    // new store on the old version would never be created for them.
    const req = indexedDB.open(DB_NAME, 2);
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains(STORE)) {
        req.result.createObjectStore(STORE, { keyPath: "client_ref" });
      }
      if (!req.result.objectStoreNames.contains(EDIT_STORE)) {
        req.result.createObjectStore(EDIT_STORE, { keyPath: "id" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function editTx<T>(
  mode: IDBTransactionMode, fn: (store: IDBObjectStore) => IDBRequest,
): Promise<T> {
  const db = await openDb();
  return new Promise<T>((resolve, reject) => {
    const r = fn(db.transaction(EDIT_STORE, mode).objectStore(EDIT_STORE));
    r.onsuccess = () => resolve(r.result as T);
    r.onerror = () => reject(r.error);
  });
}

async function tx<T>(mode: IDBTransactionMode, fn: (store: IDBObjectStore) => IDBRequest): Promise<T> {
  const db = await openDb();
  return new Promise<T>((resolve, reject) => {
    const r = fn(db.transaction(STORE, mode).objectStore(STORE));
    r.onsuccess = () => resolve(r.result as T);
    r.onerror = () => reject(r.error);
  });
}

export const savePending = (row: PendingSubmission) => tx<void>("readwrite", s => s.put(row));
export const deletePending = (ref: string) => tx<void>("readwrite", s => s.delete(ref));
export async function listPending(slug: string): Promise<PendingSubmission[]> {
  const all = await tx<PendingSubmission[]>("readonly", s => s.getAll());
  return (all || []).filter(r => r.slug === slug);
}

// ── Change notification (pending count badges) ─────────────────────
const listeners = new Set<() => void>();
export function onQueueChange(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
const notify = () => listeners.forEach(fn => fn());

/** Flush every queued submission for this slug. Safe to call repeatedly. */
export async function flushPending(slug: string): Promise<number> {
  const rows = await listPending(slug);
  let flushed = 0;
  for (const row of rows) {
    try {
      const res = await fetch(`/api/v1/portal/${row.slug}/submit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...row.payload, client_ref: row.client_ref }),
      });
      // Success or permanent rejection: either way, stop retrying this row.
      if (res.ok || (res.status >= 400 && res.status < 500)) {
        await deletePending(row.client_ref);
        flushed++;
      }
    } catch { /* still offline — a later flush retries */ }
  }
  if (flushed) notify();
  return flushed;
}

/** Queue an offline variable edit, stamped for CRDT convergence. */
export async function queueEdit(
  caseId: string, key: string, value: unknown,
): Promise<PendingEdit> {
  const row: PendingEdit = {
    id: `${caseId}:${key}`,
    case_id: caseId,
    key,
    value,
    lamport: tickLamport(),
    origin: deviceOrigin(),
    saved_at: new Date().toISOString(),
  };
  // Keyed on (case, variable): a newer local edit supersedes an unsent older
  // one, so the queue never grows a chain of edits to the same field.
  await editTx<void>("readwrite", st => st.put(row));
  notify();
  return row;
}

export async function listPendingEdits(caseId: string): Promise<PendingEdit[]> {
  const all = await editTx<PendingEdit[]>("readonly", st => st.getAll());
  return all.filter(r => r.case_id === caseId);
}

/**
 * Push queued edits and reconcile with what the server decided.
 *
 * The response reports each edit as applied / ignored / duplicate / rejected.
 * All four are terminal for the client: `ignored` means a newer edit already
 * won, so resending forever would be pointless. Only a network failure keeps
 * a row queued.
 */
export async function flushEdits(caseId: string): Promise<{
  applied: number; ignored: number; rejected: number;
}> {
  const rows = await listPendingEdits(caseId);
  if (rows.length === 0) return { applied: 0, ignored: 0, rejected: 0 };

  const tally = { applied: 0, ignored: 0, rejected: 0 };
  try {
    const res = await fetch(`/api/v1/cases/${caseId}/variables/sync`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        edits: rows.map(r => ({
          key: r.key, value: r.value, lamport: r.lamport, origin: r.origin,
        })),
      }),
    });
    if (!res.ok) return tally;             // retry on the next flush

    const body = await res.json();
    let highest = 0;
    for (const result of body.results ?? []) {
      const row = rows.find(r => r.key === result.key);
      if (!row) continue;
      if (result.outcome === "applied") tally.applied++;
      else if (result.outcome === "ignored" || result.outcome === "duplicate") tally.ignored++;
      else tally.rejected++;
      highest = Math.max(highest, Number(result.lamport ?? 0));
      // Every outcome is terminal — the server has spoken about this edit.
      await editTx<void>("readwrite", st => st.delete(row.id));
    }
    // Adopt the server's clock so this device sorts correctly against edits
    // made elsewhere while it was offline.
    if (highest) tickLamport(highest);
    notify();
  } catch { /* still offline — a later flush retries */ }
  return tally;
}

export async function queueSubmission(slug: string, payload: PendingSubmission["payload"]): Promise<string> {
  const client_ref = crypto.randomUUID();
  await savePending({ client_ref, slug, payload, saved_at: new Date().toISOString() });
  notify();
  // Ask the SW for Background Sync so a closed tab still submits.
  try {
    const reg: any = await navigator.serviceWorker?.ready;
    await reg?.sync?.register("portal-flush");
  } catch { /* no Background Sync — load/online flush covers it */ }
  return client_ref;
}

// ── PWA registration (portal pages only) ───────────────────────────
let _registered = false;
export function registerPortalPwa(slug: string): void {
  if (_registered || typeof window === "undefined") return;
  _registered = true;

  // Manifest is injected at runtime so only portal routes become installable.
  if (!document.querySelector('link[rel="manifest"]')) {
    const link = document.createElement("link");
    link.rel = "manifest";
    link.href = "/portal-manifest.webmanifest";
    document.head.appendChild(link);
  }

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/portal-sw.js").catch(() => {});
    navigator.serviceWorker.addEventListener("message", (e: MessageEvent) => {
      if (e.data?.type === "portal-flushed") notify();
    });
  }

  window.addEventListener("online", () => { flushPending(slug); flushAllEdits(); });
  // Flush anything left over from a previous offline visit.
  flushPending(slug);
  flushAllEdits();
}

/**
 * Flush queued edits for every case this device has pending.
 *
 * Edits are queued per case, but the reconnect trigger is global — a device
 * coming back online should converge everything it holds, not only whichever
 * case happens to be on screen.
 */
export async function flushAllEdits(): Promise<void> {
  try {
    const all = await editTx<PendingEdit[]>("readonly", st => st.getAll());
    const caseIds = [...new Set(all.map(r => r.case_id))];
    for (const id of caseIds) await flushEdits(id);
  } catch { /* storage unavailable — a later trigger retries */ }
}
