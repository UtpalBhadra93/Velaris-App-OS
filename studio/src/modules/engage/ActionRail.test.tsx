/**
 * Oct item 2 — the rep desk's action rail.
 *
 * Pinned: a blocked action stays on screen, disabled, with the KIND of reason
 * (the rep's next step differs per kind); an opted-out conversation is listed
 * but cannot be chosen; consent is recorded through the rep-desk route with the
 * call's interaction id, never as a bare checkbox.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ActionRailView } from "@shared/api/client";

const RAIL: ActionRailView = {
  interaction_id: "i1", customer_id: "c1", verification_level: "full",
  cases: [{
    id: "k1", case_number: "CLM-1", status: "open", payment_steps: [],
    conversations: [
      { id: "v1", channel: "sms", peer_hint: "…123", opted_out: true, template_only: false },
      { id: "v2", channel: "whatsapp", peer_hint: "…456", opted_out: false, template_only: true },
    ],
  }],
  consent: { email: { readable: true, purposes: { marketing: "none", recording: "granted" } },
             phone: null },
  actions: [
    { key: "reply", label: "Reply on their channel", enabled: true, blocked_by: [],
      notes: [], min_tier: "partial", privilege: "channels.conversations.write", rides_on: "" },
    { key: "payment", label: "Send a payment link", enabled: false,
      blocked_by: [{ kind: "prerequisite", reason: "No payment connector is configured for this tenant." }],
      notes: [], min_tier: "full", privilege: "payments.requests.write", rides_on: "" },
    { key: "consent", label: "Capture or withdraw consent", enabled: true, blocked_by: [],
      notes: [], min_tier: "full", privilege: "engage.actions.consent", rides_on: "" },
  ],
  capturable_purposes: ["marketing", "recording"],
  portal_link: null,
};

vi.mock("@shared/api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@shared/api/client");
  return {
    ...actual,
    engageActionRail: vi.fn(async () => RAIL),
    engageCaptureConsent: vi.fn(async () => ({ id: "r1", purpose: "marketing", action: "granted" })),
    replyToConversation: vi.fn(async () => ({})),
    initiatePaymentRequest: vi.fn(async () => ({ checkout_url: "https://pay.example/c/1" })),
  };
});

import * as client from "@shared/api/client";
import ActionRail from "./ActionRail";

describe("action rail", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("keeps a blocked action on screen, disabled, with its reason", async () => {
    render(<ActionRail customerId="c1" interactionId="i1" tier="full" />);
    const pay = await screen.findByRole("button", { name: "Send a payment link" });
    expect(pay).toBeDisabled();
    expect(screen.getByText("not set up")).toBeTruthy();
    expect(screen.getByText(/No payment connector/)).toBeTruthy();
  });

  it("lists an opted-out conversation but will not send on it", async () => {
    render(<ActionRail customerId="c1" interactionId="i1" tier="full" />);
    await userEvent.click(await screen.findByRole("button", { name: "Reply on their channel" }));
    const opted = screen.getByRole("option", { name: /opted out/ }) as HTMLOptionElement;
    expect(opted.disabled).toBe(true);
    // The usable one is chosen, and its closed WhatsApp window is flagged.
    expect((screen.getByLabelText("Conversation") as HTMLSelectElement).value).toBe("v2");
    expect(screen.getByText("template only")).toBeTruthy();
    await userEvent.type(screen.getByLabelText("Message"), "hello");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(client.replyToConversation).toHaveBeenCalledWith("v2", { body: "hello" }));
  });

  it("records consent bound to this call", async () => {
    render(<ActionRail customerId="c1" interactionId="i1" tier="full" />);
    await userEvent.click(await screen.findByRole("button", { name: "Capture or withdraw consent" }));
    expect(screen.getByText("recording: granted")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "They agree" }));
    await waitFor(() => expect(client.engageCaptureConsent).toHaveBeenCalledWith("c1", {
      interaction_id: "i1", purpose: "marketing", action: "granted", subject_kind: "email" }));
    // The rail is re-read after acting, so the new state shows without a reload.
    await waitFor(() => expect(client.engageActionRail).toHaveBeenCalledTimes(2));
  });

  it("sends the payment link it creates, on their channel", async () => {
    (client.engageActionRail as any).mockResolvedValue({
      ...RAIL,
      cases: [{ ...RAIL.cases[0], payment_steps: [{ id: "fee", name: "Excess fee" }] }],
      actions: [{ ...RAIL.actions[1], enabled: true, blocked_by: [] }],
    });
    render(<ActionRail customerId="c1" interactionId="i1" tier="full" />);
    await userEvent.click(await screen.findByRole("button", { name: "Send a payment link" }));
    await userEvent.type(screen.getByLabelText("Amount"), "12.50");
    await userEvent.click(screen.getByRole("button", { name: "Create link" }));
    await waitFor(() => expect(client.initiatePaymentRequest).toHaveBeenCalledWith(
      "k1", "fee", 1250, "gbp", "Payment"));
    const msg = await screen.findByLabelText("Message") as HTMLTextAreaElement;
    expect(msg.value).toContain("https://pay.example/c/1");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(client.replyToConversation).toHaveBeenCalledWith(
      "v2", { body: expect.stringContaining("https://pay.example/c/1") }));
  });
});
