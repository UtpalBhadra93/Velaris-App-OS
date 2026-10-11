/**
 * Oct item 5 — the tenant's industry rails.
 *
 * Rails arrive from marketplace packs or are written here. Each can be switched
 * on and off without changing it (a pack rail that is only switched off still
 * takes its pack's upgrades), edited (an edited pack rail is KEPT across pack
 * upgrades), or removed (a tenant rail is deleted; a pack rail is switched off,
 * so the next upgrade cannot quietly bring it back).
 *
 * Editing is the rail's own JSON, checked by the server with the same closed
 * rules a pack is: order, hide, relabel, guide, preset — and raise a tier,
 * never lower one. The server's refusal is shown verbatim, because it says
 * exactly which rule was broken.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useState } from "react";

import {
  engageDeleteRail, engagePutRail, engageRails, engageToggleRail, type EngageRail,
} from "@shared/api/client";
import {
  Button, Card, EmptyState, ErrorNote, FIELD_STYLE, Pill, SectionTitle, TextInput,
} from "@shared/components";

const muted: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)" };
const row: React.CSSProperties = {
  display: "flex", gap: "var(--space-sm)", alignItems: "center", flexWrap: "wrap",
};
const STARTER = JSON.stringify({
  name: "My rail", industry: "general",
  guidance: "What a rep should know on this kind of call.",
  applies_to_case_types: [],
  actions: [{ key: "reply", label: "Message the customer" }],
}, null, 2);

function Editor({ railKey, initial, enabled, onSaved, onCancel }: {
  railKey?: string; initial: string; enabled: boolean;
  onSaved: () => void; onCancel: () => void;
}) {
  const [key, setKey] = useState(railKey ?? "");
  const [text, setText] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  return (
    <div style={{ marginTop: 8 }}>
      {!railKey && (
        <TextInput aria-label="Rail key" placeholder="rail-key (lowercase, hyphens)"
                   value={key} onChange={(e) => setKey(e.target.value)} style={{ marginBottom: 8 }} />
      )}
      <textarea aria-label="Rail JSON" value={text} onChange={(e) => setText(e.target.value)}
                rows={14} style={{ ...FIELD_STYLE, width: "100%", fontFamily: "var(--font-mono)",
                                   fontSize: 12 }} />
      <div style={{ ...row, marginTop: 8 }}>
        <Button size="sm" disabled={!key.trim()} onClick={async () => {
          setError(null);
          let parsed: Record<string, unknown>;
          try { parsed = JSON.parse(text); }
          catch { setError("That is not valid JSON."); return; }
          const { key: _ignored, ...config } = parsed as Record<string, unknown>;
          try { await engagePutRail(key.trim(), config, enabled); onSaved(); }
          catch (e) { setError(String(e)); }
        }}>Save</Button>
        <Button size="sm" variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
      {error && <ErrorNote style={{ marginTop: 8 }}>{error}</ErrorNote>}
    </div>
  );
}

export default function RailSettings() {
  const [rails, setRails] = useState<EngageRail[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);   // a key, or "new"

  const load = useCallback(async () => {
    try { setRails((await engageRails()).rails); setError(null); }
    catch (e) { setError(String(e)); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const act = async (fn: () => Promise<unknown>) => {
    setError(null);
    try { await fn(); await load(); }
    catch (e) {
      const msg = String(e);
      setError(/\b403\b|forbidden/i.test(msg) ? "Rails are managed by an administrator." : msg);
    }
  };

  return (
    <div>
      <Card style={{ marginBottom: "var(--space-md)" }}>
        <SectionTitle right={editing !== "new" && (
          <Button size="sm" onClick={() => setEditing("new")}>New rail</Button>)}>
          Industry rails
        </SectionTitle>
        <p style={{ ...muted, margin: 0 }}>
          A rail orders, hides and words the rep desk's actions for a kind of call, with
          guidance and preset text. It can require MORE verification for an action, never less,
          and it cannot add an action. Install one from a marketplace pack, or write your own.
        </p>
        {editing === "new" && (
          <Editor initial={STARTER} enabled onCancel={() => setEditing(null)}
                  onSaved={() => { setEditing(null); void load(); }} />
        )}
      </Card>
      {error && <ErrorNote style={{ marginBottom: 8 }}>{error}</ErrorNote>}
      {rails.length === 0 && !error && (
        <EmptyState title="No rails" description="The rep desk uses its standard rail." />
      )}
      {rails.map((r) => (
        <Card key={r.key} style={{ marginBottom: "var(--space-sm)" }}>
          <div style={row}>
            <strong style={{ fontSize: 14 }}>{r.name}</strong>
            <Pill tone="muted">{r.industry}</Pill>
            <Pill tone={r.source === "pack" ? "info" : "muted"}>
              {r.source === "pack" ? `pack ${r.package_id ?? ""} ${r.pack_version ?? ""}` : "yours"}
            </Pill>
            {r.edited && r.source === "pack" && <Pill tone="warn">edited — kept on upgrade</Pill>}
            <Pill tone={r.enabled ? "ok" : "muted"}>{r.enabled ? "on" : "off"}</Pill>
            <span style={{ flex: 1 }} />
            <Button size="sm" variant="secondary"
                    onClick={() => act(() => engageToggleRail(r.key, !r.enabled))}>
              {r.enabled ? "Switch off" : "Switch on"}
            </Button>
            <Button size="sm" variant="secondary" onClick={() => setEditing(r.key)}>Edit</Button>
            <Button size="sm" variant="ghost" onClick={() => act(() => engageDeleteRail(r.key))}>
              {r.source === "pack" ? "Remove (switch off)" : "Delete"}
            </Button>
          </div>
          {editing === r.key && (
            <Editor railKey={r.key} initial={JSON.stringify(r.config, null, 2)} enabled={r.enabled}
                    onCancel={() => setEditing(null)}
                    onSaved={() => { setEditing(null); void load(); }} />
          )}
        </Card>
      ))}
    </div>
  );
}
