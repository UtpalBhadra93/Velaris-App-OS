/**
 * Oct item 1 — bring the next rep onto a live contact.
 *
 * Rep A names a colleague (or a queue), picks warm or cold, and gets a link to
 * share however the floor shares things. Rep B opens it and lands on the same
 * contact and 360, at the tier already reached.
 *
 * The link is a credential and the screen treats it as one: it is shown ONCE
 * (the server keeps only a hash), it says when it expires, and it can be
 * revoked until it is used. The list below shows what happened to each link —
 * never the link itself.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useState } from "react";

import {
  engageCreateJoinLink,
  engageListJoinLinks,
  engageRevokeJoinLink,
  type JoinLink,
} from "@shared/api/client";
import { Button, ErrorNote, Pill, Select, SubPanel, TextInput, type PillTone } from "@shared/components";

const STATE_TONE: Record<JoinLink["state"], PillTone> = {
  open: "info", used: "ok", revoked: "muted", expired: "muted",
};
const muted: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)" };
const row: React.CSSProperties = {
  display: "flex", gap: "var(--space-sm)", alignItems: "center", flexWrap: "wrap",
};

export default function JoinLinks({ interactionId }: { interactionId: string }) {
  const [target, setTarget] = useState<"rep" | "queue">("rep");
  const [who, setWho] = useState("");
  const [mode, setMode] = useState<"warm" | "cold">("warm");
  const [fresh, setFresh] = useState<{ url: string; expires_at: string | null } | null>(null);
  const [copied, setCopied] = useState(false);
  const [links, setLinks] = useState<JoinLink[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setLinks((await engageListJoinLinks(interactionId)).links); }
    catch (e) { setError(String(e)); }
  }, [interactionId]);
  useEffect(() => { void load(); }, [load]);

  const create = async () => {
    setError(null); setCopied(false);
    try {
      const out = await engageCreateJoinLink(interactionId, {
        mode,
        for_user_id: target === "rep" ? who.trim() : null,
        for_queue: target === "queue" ? who.trim() : null,
      });
      setFresh({ url: `${window.location.origin}${out.path}`, expires_at: out.expires_at });
      setWho("");
      void load();
    } catch (e) { setError(String(e)); }
  };

  return (
    <div>
      <div style={row}>
        <Select aria-label="Join for" value={target}
                onChange={(e) => setTarget(e.target.value as "rep" | "queue")}>
          <option value="rep">a colleague</option>
          <option value="queue">a queue</option>
        </Select>
        <TextInput aria-label={target === "rep" ? "Colleague" : "Queue"}
                   placeholder={target === "rep" ? "Their user id or email" : "Queue name"}
                   value={who} onChange={(e) => setWho(e.target.value)} style={{ width: 220 }} />
        <Select aria-label="Handover" value={mode}
                onChange={(e) => setMode(e.target.value as "warm" | "cold")}>
          <option value="warm">Warm — I stay on</option>
          <option value="cold">Cold — I leave when they join</option>
        </Select>
        <Button size="sm" disabled={!who.trim()} onClick={create}>Create link</Button>
      </div>

      {fresh && (
        <SubPanel tone="info">
          <div style={{ fontSize: 13, wordBreak: "break-all" }}>{fresh.url}</div>
          <div style={{ ...row, marginTop: 8 }}>
            <Button size="sm" variant="secondary" onClick={async () => {
              try { await navigator.clipboard.writeText(fresh.url); setCopied(true); }
              catch { setCopied(false); }
            }}>{copied ? "Copied" : "Copy"}</Button>
            <span style={muted}>
              Shown once. Works once, for them only
              {fresh.expires_at ? `, until ${new Date(fresh.expires_at).toLocaleTimeString()}` : ""}.
            </span>
          </div>
        </SubPanel>
      )}

      {links.length > 0 && (
        <div style={{ display: "grid", gap: 6, marginTop: "var(--space-sm)" }}>
          {links.map((l) => (
            <div key={l.id} style={row}>
              <Pill tone={STATE_TONE[l.state]}>{l.state}</Pill>
              <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>
                {l.mode} · {l.for_user_id ?? `queue ${l.for_queue}`}
                {l.redeemed_by ? ` — joined by ${l.redeemed_by}` : ""}
              </span>
              {l.state === "open" && (
                <Button size="sm" variant="ghost" onClick={async () => {
                  try { await engageRevokeJoinLink(l.id); void load(); }
                  catch (e) { setError(String(e)); }
                }}>Revoke</Button>
              )}
            </div>
          ))}
        </div>
      )}
      {error && <ErrorNote style={{ marginTop: 8 }}>{error}</ErrorNote>}
    </div>
  );
}
