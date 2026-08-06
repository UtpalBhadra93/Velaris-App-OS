/**
 * HxConsent panel.
 *
 * The assertions worth having are the ones that stop the UI misstating a legal
 * position: a non-consent basis cannot be withdrawn, and "no record" must read
 * as "not lawful" rather than as an empty, reassuring table.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import ConsentPanel from "./ConsentPanel";

function mockJson(body: unknown, ok = true, status = 200) {
  return vi.fn(() =>
    Promise.resolve({ ok, status, json: () => Promise.resolve(body) } as Response),
  );
}

const EMPTY_SUBJECT = { subject_ref: "a@b.com", purposes: {}, ledger: [] };

describe("ConsentPanel — withdrawal guard", () => {
  it("blocks withdrawing a legal_obligation basis before the API is called", async () => {
    const fetchMock = mockJson(EMPTY_SUBJECT);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    render(<ConsentPanel />);
    await user.type(screen.getByPlaceholderText(/subject reference/i), "a@b.com");
    await user.selectOptions(screen.getByDisplayValue("granted"), "withdrawn");
    await user.selectOptions(screen.getByDisplayValue("consent"), "legal_obligation");

    expect(screen.getByRole("button", { name: /append to ledger/i })).toBeDisabled();
    expect(screen.getByText(/cannot be withdrawn by the subject/i)).toBeInTheDocument();
    // The point: the API is never asked to make a record that misstates the law.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("allows withdrawing a consent basis", async () => {
    vi.stubGlobal("fetch", mockJson(EMPTY_SUBJECT));
    const user = userEvent.setup();

    render(<ConsentPanel />);
    await user.type(screen.getByPlaceholderText(/subject reference/i), "a@b.com");
    await user.selectOptions(screen.getByDisplayValue("granted"), "withdrawn");

    expect(screen.getByRole("button", { name: /append to ledger/i })).toBeEnabled();
  });
});

describe("ConsentPanel — Article 9", () => {
  it("flags biometric as requiring explicit consent", async () => {
    vi.stubGlobal("fetch", mockJson(EMPTY_SUBJECT));
    const user = userEvent.setup();

    render(<ConsentPanel />);
    await user.selectOptions(screen.getByDisplayValue("case_processing"), "biometric");

    expect(screen.getByText(/Article 9 special category/i)).toBeInTheDocument();
  });
});

describe("ConsentPanel — absence is not permission", () => {
  it("says a subject with no records is not lawful, rather than showing an empty table", async () => {
    vi.stubGlobal("fetch", mockJson(EMPTY_SUBJECT));
    const user = userEvent.setup();

    render(<ConsentPanel />);
    await user.type(screen.getByPlaceholderText(/subject reference/i), "a@b.com");
    await user.click(screen.getByRole("button", { name: /look up/i }));

    await waitFor(() =>
      expect(screen.getByText(/not lawful/i)).toBeInTheDocument(),
    );
  });

  it("renders a recorded purpose with its basis", async () => {
    vi.stubGlobal("fetch", mockJson({
      subject_ref: "a@b.com",
      purposes: {
        marketing: {
          purpose: "marketing", granted: true, basis: "consent",
          source: "portal_submit", at: "2026-08-01T10:00:00Z",
        },
      },
      ledger: [{
        id: "1", purpose: "marketing", action: "granted",
        basis: "consent", source: "portal_submit",
        recorded_at: "2026-08-01T10:00:00Z",
      }],
    }));
    const user = userEvent.setup();

    render(<ConsentPanel />);
    await user.type(screen.getByPlaceholderText(/subject reference/i), "a@b.com");
    await user.click(screen.getByRole("button", { name: /look up/i }));

    // The purpose appears three times by design: as an <option> in the picker,
    // in the derived-state table, and in the ledger row that produced it. That
    // duplication IS the panel's point — the state is a summary of the ledger,
    // and both are shown so the summary can be checked against its evidence.
    await waitFor(() =>
      expect(screen.getAllByRole("cell", { name: "marketing" })).toHaveLength(2),
    );
    expect(screen.getAllByRole("cell", { name: "portal_submit" })).toHaveLength(2);
    // ...and the derived state says it is currently lawful.
    expect(screen.getByRole("cell", { name: /yes/i })).toBeInTheDocument();
  });
});

describe("ConsentPanel — errors", () => {
  it("shows the server's refusal instead of failing silently", async () => {
    vi.stubGlobal("fetch", mockJson(
      { detail: "'marketng' is not a known purpose" }, false, 400,
    ));
    const user = userEvent.setup();

    render(<ConsentPanel />);
    await user.type(screen.getByPlaceholderText(/subject reference/i), "a@b.com");
    await user.click(screen.getByRole("button", { name: /look up/i }));

    await waitFor(() =>
      expect(screen.getByText(/not a known purpose/i)).toBeInTheDocument(),
    );
  });
});
