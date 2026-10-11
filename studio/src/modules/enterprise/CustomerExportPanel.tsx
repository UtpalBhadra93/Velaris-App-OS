/**
 * Oct item 8f — a CUSTOMER's subject-access export (GDPR Art. 15).
 *
 * The panel above this one answers for staff users. This one answers for the
 * people the platform serves: find what this tenant holds about them by email,
 * phone, portal account or rep-desk profile; check first (counts only, nothing
 * recorded), then download. Internal staff notes stay out unless the operator
 * ticks the box — after reviewing them for other people's data.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useState } from "react";

import { customerExport, type CustomerExportRequest } from "@shared/api/client";
import { Button, Card, ErrorNote, FIELD_STYLE } from "@shared/components";

const muted: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)" };
const row: React.CSSProperties = {
  display: "flex", gap: "var(--space-sm)", alignItems: "center", flexWrap: "wrap",
};
const split = (s: string) => s.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);

export default function CustomerExportPanel() {
  const [emails, setEmails] = useState("");
  const [phones, setPhones] = useState("");
  const [reason, setReason] = useState("");
  const [internal, setInternal] = useState(false);
  const [summary, setSummary] = useState<{ exists: boolean; counts: Record<string, number>;
                                           internal_notes_withheld: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const body = (extra: Partial<CustomerExportRequest>): CustomerExportRequest => ({
    emails: split(emails), phones: split(phones), include_internal: internal,
    reason: reason.trim(), ...extra,
  });
  const ready = (split(emails).length + split(phones).length) > 0 && reason.trim().length >= 5;

  const run = async (download: boolean) => {
    setBusy(true); setError(null); setDone(null);
    try {
      const out = await customerExport(body({ summary: !download }));
      if (!download) { setSummary(out as any); return; }
      const blob = new Blob([out as string], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = "customer-export.json"; a.click();
      URL.revokeObjectURL(url);
      setDone("Downloaded. The request is recorded as completed.");
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };

  return (
    <Card style={{ marginBottom: "var(--space-lg)" }}>
      <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 6 }}>
        Customer data request (Art. 15)
      </div>
      <p style={{ ...muted, margin: "0 0 8px" }}>
        Everything this tenant holds about one customer — portal account, rep-desk profile,
        cases, messages, documents (listed), orders, calls, conversations and consent. Only
        this tenant is searched. Customers can also download their own from the portal.
      </p>
      <div style={{ ...row, alignItems: "flex-start" }}>
        <textarea aria-label="Customer emails" rows={2} value={emails}
                  onChange={(e) => setEmails(e.target.value)} placeholder="email(s)"
                  style={{ ...FIELD_STYLE, flex: 1, minWidth: 200 }} />
        <textarea aria-label="Customer phones" rows={2} value={phones}
                  onChange={(e) => setPhones(e.target.value)} placeholder="phone(s)"
                  style={{ ...FIELD_STYLE, flex: 1, minWidth: 200 }} />
      </div>
      <div style={{ ...row, marginTop: 8 }}>
        <input aria-label="Request reason" value={reason} onChange={(e) => setReason(e.target.value)}
               placeholder="request reference / reason" style={{ ...FIELD_STYLE, flex: 1, minWidth: 220 }} />
        <label style={muted}>
          <input type="checkbox" checked={internal} onChange={(e) => setInternal(e.target.checked)} />{" "}
          include internal staff notes (reviewed)
        </label>
      </div>
      <div style={{ ...row, marginTop: 8 }}>
        <Button size="sm" variant="secondary" disabled={!ready || busy} onClick={() => run(false)}>
          Check
        </Button>
        <Button size="sm" disabled={!ready || busy} onClick={() => run(true)}>Download export</Button>
      </div>
      {summary && (
        <div style={{ ...muted, marginTop: 8 }}>
          {summary.exists ? "Found: " : "Nothing held for these identifiers. "}
          {Object.entries(summary.counts).filter(([, n]) => n > 0)
            .map(([k, n]) => `${n} ${k.replace(/_/g, " ")}`).join(" · ")}
          {summary.internal_notes_withheld > 0 &&
            <> · {summary.internal_notes_withheld} internal note(s) withheld</>}
        </div>
      )}
      {done && <div style={{ ...muted, marginTop: 8 }}>{done}</div>}
      {error && <ErrorNote style={{ marginTop: 8 }}>{error}</ErrorNote>}
    </Card>
  );
}
