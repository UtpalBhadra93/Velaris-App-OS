/**
 * Oct item 4 — who else is connected to this customer.
 *
 * Two kinds, drawn differently on purpose:
 *   - relations a person RECORDED (with `may_act_for` where it was granted);
 *   - connections HxGraph SUGGESTS from evidence — a shared line, the same
 *     claim, the same small firm — each labelled "suggested", with its reasons,
 *     masked to an initial, and never able to act for the customer.
 *
 * A rep who agrees with a suggestion records it with one click; that writes an
 * ordinary `related_to` relation, never `may_act_for`.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useState } from "react";

import { engageRelate, type ThreeSixty } from "@shared/api/client";
import { Button, Card, ErrorNote, Pill, SectionTitle } from "@shared/components";

const muted: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)" };
const row: React.CSSProperties = {
  display: "flex", gap: "var(--space-sm)", alignItems: "center", flexWrap: "wrap",
};

export default function RelatedPeople({ view, customerId, onChanged }: {
  view: ThreeSixty; customerId: string; onChanged: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const recorded = view.panels.relations ?? [];
  const derived = view.panels.suggested_relations;
  if (!view.panels.relations && !derived) return null;
  const skipped = Object.entries(derived?.skipped_as_too_common ?? {}).filter(([, n]) => n > 0);

  return (
    <Card>
      <SectionTitle>Related people</SectionTitle>
      {recorded.length === 0 && !derived?.suggestions.length && (
        <p style={{ ...muted, margin: 0 }}>No known connections.</p>
      )}
      <div style={{ display: "grid", gap: 8 }}>
        {recorded.map((r) => (
          <div key={`rec-${r.to}`} style={row}>
            <Pill tone="ok">{r.relation.replace("_", " ")}</Pill>
            {r.may_act_for && <Pill tone="warn">may act for them</Pill>}
            <span style={muted}>{r.note ?? "recorded by a rep"}</span>
          </div>
        ))}
        {derived?.suggestions.map((s) => (
          <div key={`sug-${s.customer_id}`}
               style={{ paddingTop: 6, borderTop: "1px solid var(--border-subtle)" }}>
            <div style={row}>
              <Pill tone="info">suggested</Pill>
              <strong style={{ fontSize: 13 }}>{s.name_initial} · {s.display_name_masked}</strong>
              <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>{s.suggested}</span>
              {s.hops > 1 && <Pill tone="muted">2 steps away</Pill>}
              {s.already_related ? <Pill tone="ok">recorded</Pill> : (
                <Button size="sm" variant="secondary" onClick={async () => {
                  setError(null);
                  try {
                    await engageRelate(customerId, {
                      to_customer_id: s.customer_id, relation: "related_to",
                      note: `confirmed from suggestion: ${s.because[0] ?? s.suggested}` });
                    onChanged();
                  } catch (e) { setError(String(e)); }
                }}>They are connected</Button>
              )}
            </div>
            <div style={{ ...muted, marginTop: 4 }}>{s.because.join(" · ")}</div>
          </div>
        ))}
      </div>
      {skipped.length > 0 && (
        <p style={{ ...muted, margin: "8px 0 0" }}>
          Not counted as relationships: {skipped.map(([k, n]) => `${n} ${k === "number" ? "shared switchboard number(s)" : k === "case" ? "very large case(s)" : "large organisation(s)"}`).join(", ")}.
        </p>
      )}
      {error && <ErrorNote style={{ marginTop: 8 }}>{error}</ErrorNote>}
    </Card>
  );
}
