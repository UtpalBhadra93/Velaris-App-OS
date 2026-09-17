// Shared API + presentation helpers for the Compliance module.
//
// Extracted when HxConsent / HxPrivacy / HxLedger / HxDR gained panels: all
// five surfaces sit behind the same `/api/v1/compliance` router and were
// duplicating the auth header, the error unwrapping and the table styles.
//
// Copyright (c) 2024-2026 Velaris Contributors
// SPDX-License-Identifier: BSL-1.1
import React from "react";
import { getAccessToken } from "@/auth/tokenManager";

/** Always read the token through this — a hand-rolled header is how 21 modules
 *  ended up unauthenticated. */
export function authHdr(): Record<string, string> {
  const t = getAccessToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

export async function apiJSON<T>(url: string, opts: RequestInit = {}): Promise<T> {
  const r = await fetch(url, { ...opts, headers: { ...authHdr(), ...opts.headers } });
  if (!r.ok) {
    let detail = `${url} → ${r.status}`;
    try {
      const j = await r.json();
      if (j?.detail) detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch { /* a non-JSON error body is still an error */ }
    throw new Error(detail);
  }
  return r.json();
}

export function postJSON<T>(url: string, body: unknown): Promise<T> {
  return apiJSON<T>(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

/** Money is integer cents everywhere in HxLedger; never render it as a float. */
export function cents(v: number | null | undefined, currency = "USD"): string {
  if (v == null) return "—";
  const sign = v < 0 ? "-" : "";
  const abs = Math.abs(v);
  return `${sign}${currency.toUpperCase()} ${(abs / 100).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
}

export function when(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

export function Kpi({ label, value, warn = false, good = false }: {
  label: string; value: React.ReactNode; warn?: boolean; good?: boolean;
}) {
  const border = warn ? "var(--status-failed)" : good ? "var(--status-completed)" : "var(--border-default)";
  const colour = warn ? "var(--status-failed)" : good ? "var(--status-completed)" : "var(--text-primary)";
  return (
    <div style={{ padding: "10px 12px", border: `1px solid ${border}`, borderRadius: 6, background: "var(--bg-elevated)" }}>
      <div style={{ fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: 0.4 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 600, color: colour, marginTop: 2 }}>{value}</div>
    </div>
  );
}

export function Banner({ tone, children }: { tone: "ok" | "warn" | "bad" | "info"; children: React.ReactNode }) {
  const palette = {
    ok:   { bg: "rgba(13,148,136,0.15)", fg: "var(--status-completed)" },
    warn: { bg: "rgba(247,183,49,0.15)", fg: "var(--status-running)" },
    bad:  { bg: "rgba(220,38,38,0.15)", fg: "var(--status-failed)" },
    info: { bg: "var(--accent-dim)", fg: "var(--accent-text)" },
  }[tone];
  return (
    <div style={{ marginTop: 12, padding: 10, borderRadius: 4, fontSize: 13,
                  background: palette.bg, color: palette.fg }}>
      {children}
    </div>
  );
}

/** Shown wherever a module is wired but has no producer yet — better than an
 *  empty table that reads as "nothing is wrong". */
export function EmptyHint({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "10px 2px" }}>{children}</div>;
}

export const card: React.CSSProperties = { background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: 8, padding: 16, marginBottom: 18 };
export const h2: React.CSSProperties = { margin: "0 0 10px", fontSize: 15, color: "var(--text-primary)" };
export const th: React.CSSProperties = { textAlign: "left", padding: "6px 8px", borderBottom: "1px solid var(--border-subtle)", color: "var(--text-secondary)", fontWeight: 500, fontSize: 11 };
export const td: React.CSSProperties = { padding: "6px 8px", borderBottom: "1px solid var(--border-subtle)", fontSize: 12 };
export const inp: React.CSSProperties = { padding: "5px 8px", fontSize: 13, border: "1px solid var(--border-default)", borderRadius: 3, background: "var(--bg-input)", color: "var(--text-primary)" };
export const table: React.CSSProperties = { width: "100%", fontSize: 12, borderCollapse: "collapse" };
export const mono: React.CSSProperties = { fontFamily: "ui-monospace, monospace" };

export function btn(primary = false): React.CSSProperties {
  return {
    padding: "5px 10px", border: `1px solid ${primary ? "var(--accent)" : "var(--border-default)"}`, borderRadius: 3,
    background: primary ? "var(--accent)" : "var(--bg-elevated)", color: primary ? "#fff" : "var(--text-primary)",
    fontSize: 12, cursor: "pointer", textDecoration: "none", display: "inline-block",
  };
}
