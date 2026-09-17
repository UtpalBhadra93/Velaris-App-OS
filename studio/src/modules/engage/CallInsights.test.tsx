/**
 * Rep desk P5/P6 panels.
 *
 * Pinned: "Not useful" cannot be recorded without a reason; the recap says how
 * many points it dropped and is not offered before verification; wrap-up offers
 * the tenant's codes when named and free text when not.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@shared/api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@shared/api/client");
  return {
    ...actual,
    engageNextActions: vi.fn(async () => ({
      customer_id: "c1", verification: "partial",
      actions: [{ key: "repeat_contact", title: "Acknowledge the repeat contact",
                  why: "2 earlier contact(s) in the last 7 days", cites: ["i1"] }],
    })),
    engageNextActionFeedback: vi.fn(async () => ({ id: "f1", action_key: "repeat_contact", verdict: "rejected" })),
    engageRecap: vi.fn(async () => ({
      points: [{ text: "Two billing calls.", sources: [{ sid: "S1", kind: "interaction", ref: "i1" }] }],
      sources: [], withheld: [], dropped: 1,
    })),
    engageDispositionCodes: vi.fn(),
  };
});

import * as client from "@shared/api/client";
import { NextActions, Recap, WrapUpPicker } from "./CallInsights";

describe("rep desk panels", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("records a rejection only with a reason", async () => {
    render(<NextActions customerId="c1" interactionId="i9" tier="partial" />);
    await screen.findByText("Acknowledge the repeat contact");
    await userEvent.click(screen.getByRole("button", { name: "Not useful" }));
    const record = screen.getByRole("button", { name: "Record" });
    expect(record).toBeDisabled();
    await userEvent.type(screen.getByPlaceholderText("Why it does not apply"), "new issue");
    await userEvent.click(record);
    await waitFor(() => expect(client.engageNextActionFeedback).toHaveBeenCalledWith("c1", {
      action_key: "repeat_contact", verdict: "rejected", reason: "new issue", interaction_id: "i9" }));
  });

  it("does not offer a recap before verification", () => {
    render(<Recap customerId="c1" interactionId="i9" tier="none" />);
    expect(screen.queryByRole("button", { name: /Recap/ })).toBeNull();
  });

  it("shows the recap and how many points were dropped", async () => {
    render(<Recap customerId="c1" interactionId="i9" tier="full" />);
    await userEvent.click(screen.getByRole("button", { name: "Recap their history" }));
    expect(await screen.findByText(/Two billing calls/)).toBeTruthy();
    expect(screen.getByText(/1 point dropped for citing no record/)).toBeTruthy();
  });

  it("offers the tenant's codes when it named some", async () => {
    (client.engageDispositionCodes as any).mockResolvedValue({
      codes: [{ code: "billing", label: "Billing" }], free_text: false });
    const onWrap = vi.fn();
    render(<WrapUpPicker busy={false} onWrap={onWrap} />);
    await screen.findByRole("option", { name: "Billing" });
    await userEvent.selectOptions(screen.getByLabelText("Wrap-up code"), "billing");
    await userEvent.click(screen.getByRole("button", { name: "Wrap up" }));
    expect(onWrap).toHaveBeenCalledWith("billing");
  });

  it("falls back to free text when no codes are named", async () => {
    (client.engageDispositionCodes as any).mockResolvedValue({ codes: null, free_text: true });
    render(<WrapUpPicker busy={false} onWrap={() => {}} />);
    expect(await screen.findByPlaceholderText(/Wrap-up code/)).toBeTruthy();
  });
});
