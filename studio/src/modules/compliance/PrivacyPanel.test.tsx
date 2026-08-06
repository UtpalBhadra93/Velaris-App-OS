/**
 * HxPrivacy panel.
 *
 * Detection is heuristic, so the screen's job is to make a weak guess look
 * like a question. A 0.4 confidence rendered like a 0.95 fact is how reviewers
 * start rubber-stamping the queue.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import PrivacyPanel from "./PrivacyPanel";

function finding(over: Record<string, unknown> = {}) {
  return {
    id: "f1", resource_type: "form", resource_name: "Intake",
    field_path: "customer_email", field_label: "Email",
    category: "email", confidence: 0.95, reason: "matched 'email'",
    special_category: false, status: "detected",
    reviewed_by: null, last_seen_at: "2026-08-04T08:00:00Z",
    ...over,
  };
}

function stub(findings: unknown[], summary: Record<string, unknown> = {}) {
  vi.stubGlobal("fetch", vi.fn(() =>
    Promise.resolve({
      ok: true, status: 200,
      json: () => Promise.resolve({
        findings,
        summary: {
          total: findings.length, by_status: {}, by_category: {},
          special_category: 0, awaiting_review: findings.length, ...summary,
        },
      }),
    } as Response),
  ));
}

describe("PrivacyPanel — confidence", () => {
  it("marks a low-confidence guess as needing a human", async () => {
    stub([finding({ confidence: 0.4, category: "name", reason: "contains 'name'" })]);
    render(<PrivacyPanel />);

    await waitFor(() =>
      expect(screen.getByText(/needs a human/i)).toBeInTheDocument(),
    );
  });

  it("does not caveat a high-confidence match", async () => {
    stub([finding({ confidence: 0.95 })]);
    render(<PrivacyPanel />);

    await waitFor(() => expect(screen.getByText("95%")).toBeInTheDocument());
    expect(screen.queryByText(/needs a human/i)).not.toBeInTheDocument();
  });
});

describe("PrivacyPanel — Article 9", () => {
  it("surfaces special-category findings separately", async () => {
    stub([finding({ category: "health", special_category: true })],
         { special_category: 1 });
    render(<PrivacyPanel />);

    await waitFor(() =>
      expect(screen.getByText(/Article 9 special category/i)).toBeInTheDocument(),
    );
  });
});

describe("PrivacyPanel — review actions", () => {
  it("offers Confirm and Dismiss only while a finding is undecided", async () => {
    stub([finding({ status: "detected" })]);
    render(<PrivacyPanel />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /confirm/i })).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /dismiss/i })).toBeInTheDocument();
  });

  it("offers no buttons once a human has decided", async () => {
    stub([finding({ status: "confirmed", reviewed_by: "alex" })]);
    render(<PrivacyPanel />);

    await waitFor(() =>
      expect(screen.getByText(/customer_email/)).toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: /^confirm$/i })).not.toBeInTheDocument();
  });
});

describe("PrivacyPanel — empty state", () => {
  it("says nothing has looked, rather than implying no PII exists", async () => {
    stub([]);
    render(<PrivacyPanel />);

    await waitFor(() =>
      expect(screen.getByText(/because nothing has looked/i)).toBeInTheDocument(),
    );
  });
});
