/**
 * HxLedger panel.
 *
 * A reconciliation screen has one job it must not get wrong: never show
 * "reconciled" when it is not, and never present an empty ledger as a clean
 * one. Both failures look identical to a healthy screen.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import LedgerPanel from "./LedgerPanel";

function stub(trialBalance: unknown, reconcile: unknown) {
  vi.stubGlobal("fetch", vi.fn((url: string) =>
    Promise.resolve({
      ok: true, status: 200,
      json: () => Promise.resolve(url.includes("reconcile") ? reconcile : trialBalance),
    } as Response),
  ));
}

const EMPTY_TB = {
  total_debits_cents: 0, total_credits_cents: 0, balanced: true, accounts: [],
};

describe("LedgerPanel — reconciliation", () => {
  it("reports a discrepancy when money moved with no book entry", async () => {
    stub(EMPTY_TB, {
      payments_captured_cents: 9_000, ledger_debits_cents: 0,
      ledger_refunds_cents: 0, ledger_net_cents: 0,
      difference_cents: 9_000, reconciled: false,
    });
    render(<LedgerPanel />);

    await waitFor(() =>
      expect(screen.getByText(/unaccounted for/i)).toBeInTheDocument(),
    );
  });

  it("points at the flag when nothing has been posted at all", async () => {
    stub(EMPTY_TB, {
      payments_captured_cents: 9_000, ledger_debits_cents: 0,
      ledger_refunds_cents: 0, ledger_net_cents: 0,
      difference_cents: 9_000, reconciled: false,
    });
    render(<LedgerPanel />);

    await waitFor(() =>
      expect(screen.getByText(/LEDGER_POSTING_ENABLED/)).toBeInTheDocument(),
    );
  });

  it("confirms reconciliation when captures and cash agree", async () => {
    stub(EMPTY_TB, {
      payments_captured_cents: 12_345, ledger_debits_cents: 12_345,
      ledger_refunds_cents: 0, ledger_net_cents: 12_345,
      difference_cents: 0, reconciled: true,
    });
    render(<LedgerPanel />);

    await waitFor(() =>
      expect(screen.getByText(/Reconciled/i)).toBeInTheDocument(),
    );
  });

  it("shows a refunded payment as reconciled, not as a discrepancy", async () => {
    // The trap the backend fix addressed — gross debits would report this broken.
    stub(EMPTY_TB, {
      payments_captured_cents: 0, ledger_debits_cents: 6_000,
      ledger_refunds_cents: 6_000, ledger_net_cents: 0,
      difference_cents: 0, reconciled: true,
    });
    render(<LedgerPanel />);

    await waitFor(() =>
      expect(screen.getByText(/Reconciled/i)).toBeInTheDocument(),
    );
    // Debits and refunds are both 6000 here — that is the point of the case,
    // so the amount legitimately appears twice.
    expect(screen.getAllByText("USD 60.00")).toHaveLength(2);
  });
});

describe("LedgerPanel — trial balance", () => {
  it("warns loudly when debits and credits disagree", async () => {
    stub({
      total_debits_cents: 10_000, total_credits_cents: 9_000, balanced: false,
      accounts: [{
        account_id: "a1", code: "1000", account_type: "asset", currency: "usd",
        debits_cents: 10_000, credits_cents: 9_000, balance_cents: 1_000,
      }],
    }, {
      payments_captured_cents: 0, ledger_debits_cents: 0, ledger_refunds_cents: 0,
      ledger_net_cents: 0, difference_cents: 0, reconciled: true,
    });
    render(<LedgerPanel />);

    await waitFor(() =>
      expect(screen.getByText(/outside/i)).toBeInTheDocument(),
    );
  });

  it("explains an empty chart of accounts rather than implying zero balances", async () => {
    stub(EMPTY_TB, {
      payments_captured_cents: 0, ledger_debits_cents: 0, ledger_refunds_cents: 0,
      ledger_net_cents: 0, difference_cents: 0, reconciled: true,
    });
    render(<LedgerPanel />);

    await waitFor(() =>
      expect(screen.getByText(/created on the first posting/i)).toBeInTheDocument(),
    );
  });
});
