/**
 * Oct item 4 — the Related people card.
 *
 * Pinned: a suggestion is drawn as "suggested", masked, with its reasons;
 * confirming it records an ordinary relation and NEVER may_act_for; a
 * recorded one is not offered again; skipped switchboards are said out loud.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ThreeSixty } from "@shared/api/client";

vi.mock("@shared/api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@shared/api/client");
  return { ...actual, engageRelate: vi.fn(async () => ({})) };
});

import * as client from "@shared/api/client";
import RelatedPeople from "./RelatedPeople";

const VIEW: ThreeSixty = {
  customer_id: "jane", party_kind: "person", verification: "full",
  panels: {
    relations: [],
    suggested_relations: {
      max_hops: 2, skipped_as_too_common: { number: 1 },
      suggestions: [
        { customer_id: "bob", name_initial: "B", display_name_masked: "•••••••th",
          suggested: "shares a line", because: ["calls from a number ending 500"], score: 0.9,
          hops: 1, already_related: false, suggestion: true, may_act_for: false },
        { customer_id: "fay", name_initial: "F", display_name_masked: "••••••en",
          suggested: "co-party", because: ["connected through someone who is on a case with them"],
          score: 0.36, hops: 2, already_related: true, suggestion: true, may_act_for: false },
      ],
    },
  },
  withheld: [], source_of_truth: "velaris", external_system: null,
  freshness: { stale: false, reason: "" },
};

describe("related people", () => {
  it("draws suggestions as suggestions and records a confirmation safely", async () => {
    const changed = vi.fn();
    render(<RelatedPeople view={VIEW} customerId="jane" onChanged={changed} />);
    expect(screen.getAllByText("suggested")).toHaveLength(2);
    expect(screen.getByText(/B · •••••••th/)).toBeTruthy();
    expect(screen.getByText("2 steps away")).toBeTruthy();
    expect(screen.getByText(/1 shared switchboard number/)).toBeTruthy();
    // Fay is already recorded, so only Bob is offered.
    const buttons = screen.getAllByRole("button", { name: "They are connected" });
    expect(buttons).toHaveLength(1);
    await userEvent.click(buttons[0]);
    await waitFor(() => expect(client.engageRelate).toHaveBeenCalledWith("jane", {
      to_customer_id: "bob", relation: "related_to",
      note: "confirmed from suggestion: calls from a number ending 500" }));
    expect(changed).toHaveBeenCalled();
  });
});
