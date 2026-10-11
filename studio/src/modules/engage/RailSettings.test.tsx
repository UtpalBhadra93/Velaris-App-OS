/**
 * Oct item 5 — industry rails on the rep desk.
 *
 * Pinned: the rep can pick a rail and sees its guidance; an action's wording,
 * guidance and presets come from the rail, and `{portal_link}` is the only
 * thing ever filled into a message; switching a rail uses the toggle (not an
 * edit); invalid JSON is caught before it is sent.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ActionRailView } from "@shared/api/client";

const VIEW: ActionRailView = {
  interaction_id: "i1", customer_id: "c1", verification_level: "full",
  cases: [{ id: "k1", case_number: "CD-1", status: "open", payment_steps: [],
            conversations: [{ id: "v1", channel: "sms", peer_hint: "…1", opted_out: false,
                              template_only: false }] }],
  consent: { email: null, phone: null },
  actions: [{ key: "document_request", label: "Request dispute evidence", enabled: true,
              blocked_by: [], notes: [], min_tier: "full", privilege: null, rides_on: "",
              guidance: "Receipts or merchant messages.",
              defaults: { message: "Upload here: {portal_link} — thanks" } }],
  capturable_purposes: [], portal_link: "https://v.example/portal/acme/login",
  rail: { key: "card-dispute", name: "Card dispute", industry: "banking",
          guidance: "Confirm the last four digits first." },
  available_rails: [{ key: "card-dispute", name: "Card dispute", industry: "banking" },
                    { key: "clinic", name: "Clinic booking", industry: "healthcare" }],
};

vi.mock("@shared/api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@shared/api/client");
  return {
    ...actual,
    engageActionRail: vi.fn(async () => VIEW),
    engageRails: vi.fn(async () => ({ rails: [{
      key: "card-dispute", name: "Card dispute", industry: "banking", source: "pack",
      package_id: "velaris/banking-dispute-rail", pack_version: "1.0.0", enabled: true,
      edited: false, config: { name: "Card dispute", actions: [{ key: "reply" }] }, updated_at: null }] })),
    engageToggleRail: vi.fn(async () => ({})),
    engagePutRail: vi.fn(async () => ({})),
    engageDeleteRail: vi.fn(async () => ({})),
  };
});

import * as client from "@shared/api/client";
import ActionRail from "./ActionRail";
import RailSettings from "./RailSettings";

describe("industry rails", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("shows the rail, its guidance, and fills only the portal link", async () => {
    render(<ActionRail customerId="c1" interactionId="i1" tier="full" />);
    expect(await screen.findByText("Confirm the last four digits first.")).toBeTruthy();
    expect(screen.getByText("Receipts or merchant messages.")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Request dispute evidence" }));
    expect((screen.getByLabelText("Message") as HTMLTextAreaElement).value)
      .toBe("Upload here: https://v.example/portal/acme/login — thanks");
    await userEvent.selectOptions(screen.getByLabelText("Industry rail"), "clinic");
    await waitFor(() => expect(client.engageActionRail).toHaveBeenLastCalledWith("c1", "i1", "clinic"));
  });

  it("switches a rail with the toggle, not an edit", async () => {
    render(<RailSettings />);
    await userEvent.click(await screen.findByRole("button", { name: "Switch off" }));
    await waitFor(() => expect(client.engageToggleRail).toHaveBeenCalledWith("card-dispute", false));
    expect(client.engagePutRail).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Remove (switch off)" })).toBeTruthy();
  });

  it("catches invalid JSON before sending it", async () => {
    render(<RailSettings />);
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const box = screen.getByLabelText("Rail JSON");
    await userEvent.clear(box);
    await userEvent.type(box, "not json");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("That is not valid JSON.")).toBeTruthy();
    expect(client.engagePutRail).not.toHaveBeenCalled();
  });
});
