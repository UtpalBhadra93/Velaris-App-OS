/* ═══════════════════════════════════════════════════════════════════
   Channel Inbox — inbound SMS, WhatsApp and voice

   Mirrors the email inbox on purpose. An agent should not have to learn a
   second mental model because the message arrived over a different transport:
   one list of threads, one unread count, one "attach to case" action.

   Two refusals get first-class treatment in the UI because they are the two an
   agent will actually hit and cannot diagnose from a generic error:

   * **opted out** — the customer texted STOP. That is a legal instruction, not
     a preference, so the composer is disabled rather than failing on send.
   * **WhatsApp window closed** — more than 24 hours since the customer's last
     message, so only a pre-approved template will be delivered. Meta accepts a
     free-form message and silently never delivers it, which is why this is
     surfaced before the send rather than after.

   Copyright (c) 2024-2026 Velaris Contributors
   SPDX-License-Identifier: BSL-1.1
   ═══════════════════════════════════════════════════════════════════ */
import React, { useCallback, useEffect, useState } from "react";

import {
  getChannelConversation,
  listChannelConversations,
  listChannelTemplates,
  replyToConversation,
  updateChannelConversation,
  type ChannelConversation,
  type ChannelMessage,
  type WhatsAppTemplate,
} from "@shared/api/client";
import { Button, Card, EmptyState, Spinner } from "@shared/components";

const CHANNEL_LABELS: Record<string, string> = {
  sms: "SMS", whatsapp: "WhatsApp", voice: "Voice",
};

function windowIsClosed(conversation: ChannelConversation | null): boolean {
  if (!conversation?.window_expires_at) return false;
  return new Date(conversation.window_expires_at).getTime() <= Date.now();
}

export default function ChannelInbox() {
  const [conversations, setConversations] = useState<ChannelConversation[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selected, setSelected] = useState<ChannelConversation | null>(null);
  const [messages, setMessages] = useState<ChannelMessage[]>([]);
  const [templates, setTemplates] = useState<WhatsAppTemplate[]>([]);
  const [draft, setDraft] = useState("");
  const [templateName, setTemplateName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadList = useCallback(async () => {
    try { setConversations(await listChannelConversations()); }
    catch (e) { setError(String(e)); }
  }, []);

  useEffect(() => { void loadList(); }, [loadList]);

  const openConversation = useCallback(async (id: string) => {
    setSelectedId(id);
    setError(null);
    try {
      const detail = await getChannelConversation(id);
      setSelected(detail.conversation);
      setMessages(detail.messages);
      setTemplates(
        detail.conversation.endpoint_id
          ? await listChannelTemplates(detail.conversation.endpoint_id)
          : [],
      );
      await loadList();
    } catch (e) { setError(String(e)); }
  }, [loadList]);

  const send = async () => {
    if (!selected) return;
    setBusy(true); setError(null);
    try {
      await replyToConversation(selected.id, {
        body: draft,
        template_name: templateName || undefined,
      });
      setDraft(""); setTemplateName("");
      await openConversation(selected.id);
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  const closeThread = async () => {
    if (!selected) return;
    setBusy(true);
    try {
      await updateChannelConversation(selected.id, { status: "closed" });
      await openConversation(selected.id);
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  const closedWindow = windowIsClosed(selected);
  const approved = templates.filter(t => t.approval_status === "approved");

  return (
    <div style={{ padding: "var(--space-xl)", display: "grid",
                  gridTemplateColumns: "minmax(18rem, 22rem) 1fr",
                  gap: "var(--space-lg)", height: "100%", boxSizing: "border-box" }}>
      <Card style={{ overflow: "auto" }}>
        <h3 style={{ marginTop: 0 }}>Conversations</h3>
        {error && <p role="alert" style={{ color: "var(--danger)" }}>{error}</p>}
        {conversations === null ? <Spinner /> : conversations.length === 0 ? (
          <EmptyState
            title="Nothing inbound yet"
            description="Messages appear here once a channel endpoint is configured and a customer texts it." />
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {conversations.map(c => (
              <li key={c.id}>
                <button
                  type="button"
                  onClick={() => openConversation(c.id)}
                  style={{
                    width: "100%", textAlign: "left", cursor: "pointer",
                    padding: "var(--space-sm)", border: "none",
                    borderBottom: "1px solid var(--border-subtle)",
                    background: c.id === selectedId ? "var(--bg-elevated)" : "transparent",
                  }}>
                  <strong>{c.peer_address}</strong>
                  {c.unread_count > 0 && (
                    <span style={{ marginLeft: "var(--space-xs)", color: "var(--accent)" }}>
                      ({c.unread_count} new)
                    </span>
                  )}
                  <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                    {c.endpoint_address ?? ""} · {c.status}
                    {c.case_id ? " · linked" : " · unlinked"}
                  </div>
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card style={{ display: "flex", flexDirection: "column", overflow: "hidden" }}>
        {!selected ? (
          <EmptyState title="Pick a conversation"
                      description="The whole thread, both directions, is on the case record." />
        ) : (
          <>
            <div style={{ display: "flex", justifyContent: "space-between",
                          alignItems: "baseline" }}>
              <h3 style={{ margin: 0 }}>
                {selected.peer_address}{" "}
                <span style={{ fontSize: "0.8125rem", color: "var(--text-muted)" }}>
                  {CHANNEL_LABELS[messages[0]?.channel ?? ""] ?? ""}
                </span>
              </h3>
              <Button variant="secondary" onClick={closeThread} disabled={busy}>
                Close thread
              </Button>
            </div>

            <div style={{ flex: 1, overflow: "auto", margin: "var(--space-md) 0" }}>
              {messages.map(m => (
                <div key={m.id} style={{
                  marginBottom: "var(--space-sm)",
                  textAlign: m.direction === "outbound" ? "right" : "left",
                }}>
                  <div style={{
                    display: "inline-block", maxWidth: "70%",
                    padding: "var(--space-sm)", borderRadius: "var(--radius)",
                    background: m.direction === "outbound"
                      ? "var(--accent-subtle, var(--bg-elevated))" : "var(--bg-elevated)",
                  }}>
                    {m.template_name && (
                      <div style={{ fontSize: "0.6875rem", color: "var(--text-muted)" }}>
                        template: {m.template_name}
                      </div>
                    )}
                    <div>{m.body}</div>
                    <div style={{ fontSize: "0.6875rem", color: "var(--text-muted)" }}>
                      {m.status}
                      {m.error ? ` — ${m.error}` : ""}
                    </div>
                  </div>
                </div>
              ))}
            </div>

            {closedWindow && (
              <p style={{ fontSize: "0.8125rem", color: "var(--warning, #c90)" }}>
                More than 24 hours have passed since this customer's last message, so
                WhatsApp will only deliver a pre-approved template. Choose one below —
                a free-form message would be accepted by Meta and never delivered.
              </p>
            )}

            <div style={{ display: "flex", gap: "var(--space-sm)", flexWrap: "wrap" }}>
              {closedWindow && (
                <select value={templateName} aria-label="Template"
                        onChange={e => setTemplateName(e.target.value)}>
                  <option value="">Template…</option>
                  {approved.map(t => (
                    <option key={t.id} value={t.name}>{t.name} ({t.language})</option>
                  ))}
                </select>
              )}
              <input value={draft} onChange={e => setDraft(e.target.value)}
                     placeholder="Reply…" aria-label="Reply"
                     style={{ flex: "1 1 20rem" }} />
              <Button onClick={send} disabled={busy}>Send</Button>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
