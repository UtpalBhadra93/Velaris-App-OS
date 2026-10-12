/**
 * PUO Phase 1 — platform update banner for the admin dashboard.
 *
 * Shows when the channel manifest pins a newer platform version than this
 * environment runs. Admin can view release notes and approve the update for
 * THIS environment (executed by the local update agent — never by the web
 * tier). No version is stored on approval: the agent resolves the channel
 * pin live at execution time.
 *
 * Renders nothing for non-admins (the status endpoint returns 403) or when
 * the environment is up to date.
 */
import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@shared/api/client";

const POLL_MS = 5 * 60 * 1000;

interface UpdateStatus {
  current_version: string;
  channel: string;
  target_version: string | null;
  update_available: boolean;
  release: {
    notes_url: string | null;
    security: boolean;
    min_upgrade_from: string | null;
  } | null;
  manifest_reachable: boolean;
  update_window: string;
  pending_request: { mode: string; requested_at: string } | null;
  last_update_status: { result: string; message: string; timestamp: string } | null;
  /** false = no update agent on this server; null/absent = cannot be told. */
  agent_installed?: boolean | null;
}

// An update that began and did not finish. After the code pull the installed
// version already equals the channel's, so `update_available` is false and the
// banner used to disappear at exactly the moment it had something to say.
const STOPPED = new Set(["failed", "unhealthy", "restart_required"]);

export function PlatformUpdateBanner() {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await apiFetch("/platform/update/status");
      if (!res.ok) return; // non-admin or service issue — stay silent
      setStatus((await res.json()) as UpdateStatus);
    } catch {
      /* silent — banner is best-effort */
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [load]);

  const request = async (mode: "window" | "now") => {
    setBusy(true);
    setError(null);
    try {
      const res = await apiFetch("/platform/update/request", {
        method: "POST",
        body: JSON.stringify({ mode }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body.detail ?? "Request failed");
      }
      await load();
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  const cancel = async () => {
    setBusy(true);
    try {
      await apiFetch("/platform/update/request", { method: "DELETE" });
      await load();
    } finally {
      setBusy(false);
    }
  };

  if (!status) return null;

  const stopped = STOPPED.has(status.last_update_status?.result ?? "");
  if (!status.update_available) {
    if (!stopped) return null;
    return (
      <div
        style={{
          padding: "10px 16px",
          marginBottom: 16,
          borderRadius: 8,
          border: "1px solid #fca5a5",
          background: "#fef2f2",
          color: "#7f1d1d",
          fontSize: 14,
        }}
      >
        <span style={{ fontWeight: 600 }}>⚠ The last platform update did not finish. </span>
        {status.last_update_status?.message}
        {" — on the server, run "}
        <code>./update-velaris.sh</code>
        {" to finish it."}
      </div>
    );
  }

  const security = status.release?.security ?? false;
  const noAgent = status.agent_installed === false;

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        flexWrap: "wrap",
        gap: 12,
        padding: "10px 16px",
        marginBottom: 16,
        borderRadius: 8,
        border: `1px solid ${security ? "#fca5a5" : "#93c5fd"}`,
        background: security ? "#fef2f2" : "#eff6ff",
        fontSize: 14,
      }}
    >
      <span style={{ fontWeight: 600 }}>
        {security ? "🔒 Security update" : "⬆ Platform update"} available:
        v{status.current_version} → v{status.target_version}
        <span style={{ fontWeight: 400, opacity: 0.7 }}> ({status.channel} channel)</span>
      </span>

      {status.release?.notes_url && (
        <a href={status.release.notes_url} target="_blank" rel="noreferrer">
          Release notes
        </a>
      )}

      {status.pending_request ? (
        <>
          {noAgent && (
            <span style={{ color: "#b91c1c" }}>
              Approved, but this server has no update agent, so nothing will apply it. On the
              server run <code>./update-velaris.sh --auto</code> now, or{" "}
              <code>./update-velaris.sh --install-timer</code> once so approvals apply themselves.
            </span>
          )}
          <span style={{ opacity: 0.8, display: noAgent ? "none" : undefined }}>
            Update approved ({status.pending_request.mode === "now"
              ? "applies at the next agent check"
              : `applies in the next maintenance window ${status.update_window || ""}`})
          </span>
          <button onClick={cancel} disabled={busy}>Cancel</button>
        </>
      ) : confirming ? (
        <>
          <span>
            {security
              ? "This release contains security fixes. Apply it for this environment?"
              : "Apply this update for this environment?"}
          </span>
          <button onClick={() => request("window")} disabled={busy}>
            Schedule (maintenance window)
          </button>
          <button onClick={() => request("now")} disabled={busy}>
            Update now
          </button>
          <button onClick={() => setConfirming(false)} disabled={busy}>Back</button>
        </>
      ) : (
        <button onClick={() => setConfirming(true)} disabled={busy}>
          Update this environment
        </button>
      )}

      {error && <span style={{ color: "#b91c1c" }}>{error}</span>}
      {stopped && (
        <span style={{ color: "#b91c1c" }}>
          Last update needs attention: {status.last_update_status?.message}
        </span>
      )}
    </div>
  );
}
