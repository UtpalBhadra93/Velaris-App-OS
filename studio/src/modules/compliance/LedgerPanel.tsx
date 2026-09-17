// HxLedger (§8.4) — trial balance and payment reconciliation.
//
// Read-only by design. Posting happens from the payment path, and a screen
// that let someone hand-key a transaction into an append-only ledger would
// invite exactly the corrections the ledger forbids.
//
// Copyright (c) 2024-2026 Velaris Contributors
// SPDX-License-Identifier: BSL-1.1
import React, { useEffect, useState } from "react";
import { apiJSON, Banner, EmptyHint, Kpi, btn, card, cents, h2, mono, table, td, th } from "./shared";

type Account = {
  account_id: string;
  code: string;
  account_type: string;
  currency: string;
  debits_cents: number;
  credits_cents: number;
  balance_cents: number;
};

type TrialBalance = {
  total_debits_cents: number;
  total_credits_cents: number;
  balanced: boolean;
  accounts: Account[];
};

type Reconcile = {
  payments_captured_cents: number;
  ledger_debits_cents: number;
  ledger_refunds_cents: number;
  ledger_net_cents: number;
  difference_cents: number;
  reconciled: boolean;
};

export default function LedgerPanel() {
  const [tb, setTb] = useState<TrialBalance | null>(null);
  const [rec, setRec] = useState<Reconcile | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    setBusy(true); setErr(null);
    try {
      const [t, r] = await Promise.all([
        apiJSON<TrialBalance>("/api/v1/compliance/ledger/trial-balance"),
        apiJSON<Reconcile>("/api/v1/compliance/ledger/reconcile"),
      ]);
      setTb(t); setRec(r);
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  }

  useEffect(() => { load(); }, []);

  return (
    <>
      <section style={card}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
          <h2 style={h2}>Payment reconciliation</h2>
          <button onClick={load} disabled={busy} style={btn()}>{busy ? "Loading…" : "Refresh"}</button>
        </div>

        {err && <Banner tone="bad">⚠ {err}</Banner>}

        {rec && (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
              <Kpi label="Payments captured" value={cents(rec.payments_captured_cents)} />
              <Kpi label="Ledger debits" value={cents(rec.ledger_debits_cents)} />
              <Kpi label="Ledger refunds" value={cents(rec.ledger_refunds_cents)} />
              <Kpi
                label="Difference"
                value={cents(rec.difference_cents)}
                warn={!rec.reconciled}
                good={rec.reconciled}
              />
            </div>

            {rec.reconciled ? (
              <Banner tone="ok">
                ✓ Reconciled — every captured payment has a matching book entry.
              </Banner>
            ) : (
              <Banner tone="bad">
                ✗ {cents(Math.abs(rec.difference_cents))} unaccounted for. Money moved without a
                matching entry, which is the exact failure this ledger exists to catch.
                {rec.ledger_net_cents === 0 && rec.payments_captured_cents > 0 && (
                  <> Nothing has been posted at all — check that
                    <code style={mono}> VELARIS_CASE_LEDGER_POSTING_ENABLED </code>
                    is on and that payments have completed since it was enabled.</>
                )}
              </Banner>
            )}
          </>
        )}
      </section>

      <section style={card}>
        <h2 style={h2}>Trial balance</h2>

        {tb && !tb.balanced && (
          <Banner tone="bad">
            ✗ Total debits do not equal total credits. Something wrote entries outside
            <code style={mono}> post() </code>— the one path that guarantees balance.
          </Banner>
        )}

        {!tb || tb.accounts.length === 0 ? (
          <EmptyHint>
            No accounts yet. The chart of accounts is created on the first posting, so this stays
            empty until a payment is captured.
          </EmptyHint>
        ) : (
          <>
            <table style={table}>
              <thead>
                <tr>
                  <th style={th}>Code</th>
                  <th style={th}>Type</th>
                  <th style={th}>Debits</th>
                  <th style={th}>Credits</th>
                  <th style={th}>Balance</th>
                </tr>
              </thead>
              <tbody>
                {tb.accounts.map(a => (
                  <tr key={a.account_id}>
                    <td style={{ ...td, ...mono }}>{a.code}</td>
                    <td style={td}>{a.account_type}</td>
                    <td style={td}>{cents(a.debits_cents, a.currency)}</td>
                    <td style={td}>{cents(a.credits_cents, a.currency)}</td>
                    <td style={{ ...td, fontWeight: 600 }}>{cents(a.balance_cents, a.currency)}</td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr>
                  <td style={{ ...td, fontWeight: 600 }} colSpan={2}>Total</td>
                  <td style={{ ...td, fontWeight: 600 }}>{cents(tb.total_debits_cents)}</td>
                  <td style={{ ...td, fontWeight: 600 }}>{cents(tb.total_credits_cents)}</td>
                  <td style={td}>
                    {tb.balanced
                      ? <span style={{ color: "var(--status-completed)" }}>✓ balanced</span>
                      : <span style={{ color: "var(--status-failed)" }}>✗ out by {cents(tb.total_debits_cents - tb.total_credits_cents)}</span>}
                  </td>
                </tr>
              </tfoot>
            </table>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 10 }}>
              Balance sign follows account type — a debit increases an asset or expense, a credit
              increases a liability, income or equity.
            </div>
          </>
        )}
      </section>
    </>
  );
}
