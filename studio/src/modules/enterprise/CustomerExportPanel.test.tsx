/**
 * Oct item 8f — the customer data request panel.
 *
 * Pinned: nothing is sent without an identifier AND a reason; "Check" asks for
 * counts only; internal notes are off unless ticked; a refusal is shown.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as client from "@shared/api/client";

vi.mock("@shared/api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@shared/api/client");
  return { ...actual, customerExport: vi.fn() };
});

import CustomerExportPanel from "./CustomerExportPanel";

describe("CustomerExportPanel", () => {
  beforeEach(() => vi.clearAllMocks());

  it("needs an identifier and a reason, and Check asks for counts only", async () => {
    vi.mocked(client.customerExport).mockResolvedValue({
      exists: true, counts: { cases: 3, orders: 1, sms: 0 }, internal_notes_withheld: 2 });
    render(<CustomerExportPanel />);
    const check = screen.getByText("Check");
    expect((check as HTMLButtonElement).disabled).toBe(true);
    await userEvent.type(screen.getByLabelText("Customer emails"), "pat@x.test");
    expect((check as HTMLButtonElement).disabled).toBe(true);
    await userEvent.type(screen.getByLabelText("Request reason"), "DSAR #12");
    await userEvent.click(check);
    expect(client.customerExport).toHaveBeenCalledWith({
      emails: ["pat@x.test"], phones: [], include_internal: false,
      reason: "DSAR #12", summary: true });
    expect(await screen.findByText(/3 cases · 1 orders/)).toBeTruthy();
    expect(screen.getByText(/2 internal note/)).toBeTruthy();
  });

  it("shows a refusal", async () => {
    vi.mocked(client.customerExport).mockRejectedValue(new Error("policy refuses this export"));
    render(<CustomerExportPanel />);
    await userEvent.type(screen.getByLabelText("Customer phones"), "+447700900111");
    await userEvent.type(screen.getByLabelText("Request reason"), "DSAR #13");
    await userEvent.click(screen.getByText("Download export"));
    expect(await screen.findByText(/policy refuses/)).toBeTruthy();
  });
});
