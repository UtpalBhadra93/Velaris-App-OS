import React from "react";
import type { ProcessStatus, InstanceStatus } from "@shared/types";

/* ═══════════════════════════════════════════════════════════════════
   StatusBadge — colored pill for process/instance status
   ═══════════════════════════════════════════════════════════════════ */

const STATUS_COLORS: Record<string, string> = {
  active: "var(--status-active)",
  running: "var(--status-running)",
  completed: "var(--status-completed)",
  failed: "var(--status-failed)",
  cancelled: "var(--status-cancelled)",
  suspended: "var(--status-cancelled)",
  deprecated: "var(--status-deprecated)",
};

export function StatusBadge({ status }: { status: ProcessStatus | InstanceStatus }) {
  const color = STATUS_COLORS[status] || "var(--text-muted)";
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "3px 10px",
        borderRadius: 100,
        fontSize: 12,
        fontWeight: 500,
        fontFamily: "var(--font-mono)",
        letterSpacing: "0.02em",
        color,
        background: `color-mix(in srgb, ${color} 12%, transparent)`,
        border: `1px solid color-mix(in srgb, ${color} 25%, transparent)`,
        textTransform: "uppercase",
      }}
    >
      <span
        style={{
          width: 6,
          height: 6,
          borderRadius: "50%",
          background: color,
          animation: status === "running" ? "pulse 2s infinite" : "none",
        }}
      />
      {status}
      <style>{`@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }`}</style>
    </span>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   Card — elevated surface container
   ═══════════════════════════════════════════════════════════════════ */

export function Card({
  children,
  onClick,
  style,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  style?: React.CSSProperties;
}) {
  return (
    <div
      onClick={onClick}
      style={{
        background: "var(--bg-card)",
        border: "1px solid var(--border-subtle)",
        borderRadius: "var(--radius-md)",
        padding: "var(--space-lg)",
        cursor: onClick ? "pointer" : "default",
        transition: "all 0.15s ease",
        ...(onClick && {
          ":hover": { background: "var(--bg-card-hover)" },
        }),
        ...style,
      }}
      onMouseEnter={(e) => onClick && (e.currentTarget.style.background = "var(--bg-card-hover)")}
      onMouseLeave={(e) => onClick && (e.currentTarget.style.background = "var(--bg-card)")}
    >
      {children}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   Button
   ═══════════════════════════════════════════════════════════════════ */

export function Button({
  children,
  onClick,
  variant = "primary",
  size = "md",
  disabled = false,
  style,
}: {
  children: React.ReactNode;
  onClick?: ((e?: React.MouseEvent<HTMLButtonElement>) => void);
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
  disabled?: boolean;
  style?: React.CSSProperties;
}) {
  const base: React.CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: 8,
    fontFamily: "var(--font-body)",
    fontWeight: 500,
    fontSize: size === "sm" ? 12 : 13,
    padding: size === "sm" ? "6px 12px" : "8px 16px",
    borderRadius: "var(--radius-sm)",
    border: "1px solid transparent",
    cursor: disabled ? "not-allowed" : "pointer",
    opacity: disabled ? 0.5 : 1,
    transition: "all 0.15s ease",
    letterSpacing: "0.01em",
  };

  const variants: Record<string, React.CSSProperties> = {
    primary: {
      background: "var(--accent)",
      color: "var(--text-inverse)",
      borderColor: "var(--accent)",
    },
    secondary: {
      background: "transparent",
      color: "var(--text-primary)",
      borderColor: "var(--border-default)",
    },
    ghost: {
      background: "transparent",
      color: "var(--text-secondary)",
      borderColor: "transparent",
    },
    danger: {
      background: "transparent",
      color: "var(--status-failed)",
      borderColor: "var(--status-failed)",
    },
  };

  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{ ...base, ...variants[variant], ...style }}
    >
      {children}
    </button>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   Spinner
   ═══════════════════════════════════════════════════════════════════ */

export function Spinner({ size = 20 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      style={{ animation: "spin 1s linear infinite" }}
    >
      <circle
        cx="12"
        cy="12"
        r="10"
        stroke="var(--border-default)"
        strokeWidth="2.5"
        fill="none"
      />
      <path
        d="M12 2a10 10 0 0 1 10 10"
        stroke="var(--accent)"
        strokeWidth="2.5"
        strokeLinecap="round"
        fill="none"
      />
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </svg>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   EmptyState
   ═══════════════════════════════════════════════════════════════════ */

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: React.ReactNode;
}) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: "var(--space-md)",
        padding: "var(--space-2xl)",
        color: "var(--text-muted)",
      }}
    >
      <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
        <rect
          x="8"
          y="8"
          width="32"
          height="32"
          rx="4"
          stroke="var(--border-default)"
          strokeWidth="2"
          strokeDasharray="4 4"
        />
        <path d="M20 24h8M24 20v8" stroke="var(--border-strong)" strokeWidth="2" strokeLinecap="round" />
      </svg>
      <div style={{ fontSize: 15, fontWeight: 500, color: "var(--text-secondary)" }}>{title}</div>
      {description && (
        <div style={{ fontSize: 13, maxWidth: 320, textAlign: "center" }}>{description}</div>
      )}
      {action && <div style={{ marginTop: "var(--space-sm)" }}>{action}</div>}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   TimeAgo — relative time display
   ═══════════════════════════════════════════════════════════════════ */

export function TimeAgo({ date }: { date: string }) {
  const seconds = Math.floor((Date.now() - new Date(date).getTime()) / 1000);

  if (seconds < 60) return <span title={date}>just now</span>;
  if (seconds < 3600) return <span title={date}>{Math.floor(seconds / 60)}m ago</span>;
  if (seconds < 86400) return <span title={date}>{Math.floor(seconds / 3600)}h ago</span>;
  return <span title={date}>{Math.floor(seconds / 86400)}d ago</span>;
}

/* ═══════════════════════════════════════════════════════════════════
   Stat — key-value display for dashboards
   ═══════════════════════════════════════════════════════════════════ */

export function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div>
      <div style={{ fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "var(--font-mono)" }}>
        {label}
      </div>
      <div style={{ fontSize: 24, fontWeight: 600, color: "var(--text-primary)", marginTop: 4, fontFamily: "var(--font-display)" }}>
        {value}
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   Pill — small status token

   **Why this exists.** Five modules built in one week (rep desk, self-healing,
   intent, joint contracts, field ops) each hand-rolled the same `Pill` against
   `--success-bg`, `--success-fg`, `--danger-bg`, `--danger-fg` and
   `--warning-fg`. **None of those variables exist.** Every one of them fell
   through to its hardcoded fallback — `#10331f`, `#4ade80`, `#f87171` — which
   are dark-theme colours painted onto a white card in light mode. The one that
   does resolve, `--warning-bg`, is `--accent-dim`: teal, under amber text.

   So the tone lives here, on the tokens the platform actually defines, and a
   copy cannot drift again.
   ═══════════════════════════════════════════════════════════════════ */

export type PillTone = "ok" | "warn" | "bad" | "info" | "muted";

const PILL_COLOR: Record<PillTone, string> = {
  ok:    "var(--status-completed)",
  warn:  "var(--status-running)",
  bad:   "var(--status-failed)",
  info:  "var(--accent-text)",
  muted: "var(--text-muted)",
};

export function Pill({
  tone = "muted",
  children,
  title,
}: {
  tone?: PillTone;
  children: React.ReactNode;
  title?: string;
}) {
  const c = PILL_COLOR[tone];
  return (
    <span
      title={title}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "2px 9px",
        borderRadius: 100,
        fontSize: 11,
        fontWeight: 600,
        fontFamily: "var(--font-mono)",
        letterSpacing: "0.02em",
        whiteSpace: "nowrap",
        color: c,
        background: `color-mix(in srgb, ${c} 12%, transparent)`,
        border: `1px solid color-mix(in srgb, ${c} 25%, transparent)`,
      }}
    >
      {children}
    </span>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   OwnershipPill — default, a tenant's copy, and a copy whose default moved

   Oct 8i/8e: case types, forms, rules, data models and calendars are one
   platform DEFAULT (tenant_id null) that every tenant reads, until a tenant
   edits it — then that tenant has its own copy (forked_from_id), and the
   default's later changes are OFFERED (update_available), never applied.
   A tenant's own row, and a server too old to send these fields, show nothing.
   ═══════════════════════════════════════════════════════════════════ */

export type Ownership = {
  tenant_id?: string | null;
  forked_from_id?: string | null;
  update_available?: boolean;
};

export function OwnershipPill({ row }: { row: Ownership }) {
  if (row.forked_from_id) {
    return (
      <span style={{ display: "inline-flex", gap: 4 }}>
        <Pill tone="info" title="Your tenant's own copy of a platform default. Your edits stay here.">
          Your copy
        </Pill>
        {row.update_available && (
          <Pill tone="warn" title="The default this was copied from has changed since. Your copy has not been changed.">
            Update available
          </Pill>
        )}
      </span>
    );
  }
  if (row.tenant_id === null) {
    return (
      <Pill title="A platform default shared by every tenant. A tenant that edits it gets its own copy.">
        Default
      </Pill>
    );
  }
  return null;
}

/* ═══════════════════════════════════════════════════════════════════
   Banner — a standing fact above the tab bar

   For the one sentence that changes how every tab below it reads: the feature
   is off, N changes are still being watched, N nodes are locked out. It sits
   between the app header and the tabs, full width, so it cannot be scrolled
   past.
   ═══════════════════════════════════════════════════════════════════ */

export function Banner({
  tone = "muted",
  title,
  children,
}: {
  tone?: PillTone;
  title?: React.ReactNode;
  children?: React.ReactNode;
}) {
  const c = PILL_COLOR[tone];
  return (
    <div
      style={{
        flexShrink: 0,
        display: "flex",
        alignItems: "baseline",
        gap: 8,
        flexWrap: "wrap",
        padding: "10px var(--space-2xl)",
        background: `color-mix(in srgb, ${c} 7%, var(--bg-panel))`,
        borderBottom: "1px solid var(--border-subtle)",
        borderLeft: `3px solid ${c}`,
        fontSize: 12,
        color: "var(--text-secondary)",
        lineHeight: 1.6,
      }}
    >
      {title && (
        <strong style={{ color: "var(--text-primary)", fontWeight: 600 }}>{title}</strong>
      )}
      {children && <span>{children}</span>}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   Form fields

   A bare `<input>` inherits the browser's own chrome, which in dark mode is a
   white box with black text sitting on a #18181c card. Every text entry goes
   through these.
   ═══════════════════════════════════════════════════════════════════ */

export const FIELD_STYLE: React.CSSProperties = {
  padding: "8px 12px",
  border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)",
  fontSize: 13,
  fontFamily: "var(--font-body)",
  background: "var(--bg-input)",
  color: "var(--text-primary)",
  outline: "none",
  boxSizing: "border-box",
};

export function TextInput(
  props: React.InputHTMLAttributes<HTMLInputElement>,
) {
  const { style, ...rest } = props;
  return (
    <input
      {...rest}
      style={{ ...FIELD_STYLE, ...style }}
      onFocus={(e) => { e.currentTarget.style.borderColor = "var(--border-focus)"; props.onFocus?.(e); }}
      onBlur={(e) => { e.currentTarget.style.borderColor = "var(--border-default)"; props.onBlur?.(e); }}
    />
  );
}

export function Select(
  props: React.SelectHTMLAttributes<HTMLSelectElement>,
) {
  const { style, children, ...rest } = props;
  return (
    <select {...rest} style={{ ...FIELD_STYLE, cursor: "pointer", ...style }}>
      {children}
    </select>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   SubPanel — an inset block INSIDE a Card

   Modules were nesting `<Card>` in `<Card>`, which draws the same border and
   background twice and reads as a rendering fault rather than a hierarchy.
   ═══════════════════════════════════════════════════════════════════ */

export function SubPanel({
  tone,
  children,
  style,
}: {
  tone?: PillTone;
  children: React.ReactNode;
  style?: React.CSSProperties;
}) {
  const c = tone ? PILL_COLOR[tone] : "var(--border-subtle)";
  return (
    <div
      style={{
        marginTop: "var(--space-md)",
        padding: "var(--space-md)",
        borderRadius: "var(--radius-sm)",
        background: tone
          ? `color-mix(in srgb, ${c} 5%, var(--bg-elevated))`
          : "var(--bg-elevated)",
        borderLeft: `3px solid ${c}`,
        ...style,
      }}
    >
      {children}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   SectionTitle — the heading inside a card or a tab body
   ═══════════════════════════════════════════════════════════════════ */

export function SectionTitle({
  children,
  right,
  style,
}: {
  children: React.ReactNode;
  right?: React.ReactNode;
  style?: React.CSSProperties;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: "var(--space-md)",
        margin: "0 0 var(--space-md)",
        ...style,
      }}
    >
      <h3
        style={{
          margin: 0,
          fontSize: 14,
          fontWeight: 600,
          color: "var(--text-primary)",
          fontFamily: "var(--font-display)",
        }}
      >
        {children}
      </h3>
      {right}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   Table styles — one geometry for every module's data table
   ═══════════════════════════════════════════════════════════════════ */

export const TABLE: Record<string, React.CSSProperties> = {
  table: { width: "100%", borderCollapse: "collapse", fontSize: 13 },
  th: {
    textAlign: "left",
    padding: "8px 10px",
    fontSize: 11,
    fontWeight: 600,
    textTransform: "uppercase",
    letterSpacing: "0.06em",
    color: "var(--text-muted)",
    fontFamily: "var(--font-mono)",
    borderBottom: "1px solid var(--border-default)",
    whiteSpace: "nowrap",
  },
  tr: { borderBottom: "1px solid var(--border-subtle)" },
  td: { padding: "10px", color: "var(--text-primary)", verticalAlign: "top" },
};

/* ═══════════════════════════════════════════════════════════════════
   ErrorNote — a failed call, shown the same way everywhere
   ═══════════════════════════════════════════════════════════════════ */

export function ErrorNote({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  if (!children) return null;
  return (
    <div
      style={{
        display: "flex",
        gap: 8,
        padding: "10px 14px",
        borderRadius: "var(--radius-sm)",
        background: "color-mix(in srgb, var(--status-failed) 10%, transparent)",
        border: "1px solid color-mix(in srgb, var(--status-failed) 30%, transparent)",
        color: "var(--status-failed)",
        fontSize: 13,
        lineHeight: 1.5,
        ...style,
      }}
    >
      {children}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   MONO — the one monospace run every module was redeclaring
   ═══════════════════════════════════════════════════════════════════ */

export const MONO: React.CSSProperties = {
  fontFamily: "var(--font-mono)",
  fontSize: 12,
};

export { CommitModal } from "./CommitModal";

export { CommitHistory } from "./CommitHistory";

export { BranchModeBanner } from "./BranchModeBanner";

export { ReviewerPicker } from "./ReviewerPicker";

export { Tabs, default as TabBar } from "./Tabs";
export type { TabItem } from "./Tabs";

/**
 * MeetRoom is `lazy` HERE, in the barrel, on purpose.
 *
 * Every module in Studio imports something from `@shared/components`, and this
 * one line pulled `livekit-client` — a WebRTC stack — into whatever chunk the
 * barrel landed in, which was the single entry chunk. A user who never opens a
 * meeting downloaded the whole conferencing client to see their case list.
 *
 * It renders inside the router's Suspense boundary, so the only visible
 * difference is that the video component arrives when a meeting is opened.
 */
export const MeetRoom = React.lazy(() => import("./MeetRoom"));

export { default as VoiceInput, appendTranscript } from "./VoiceInput";

export { default as VoiceFillPanel } from "./VoiceFillPanel";
export type { VoiceFillCandidate } from "./VoiceFillPanel";
