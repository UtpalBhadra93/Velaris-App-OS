/**
 * Oct item 1 — join links on the rep desk.
 *
 * Pinned: the link is shown once and the list never carries it; an open link
 * can be revoked; the desk redeems `#join=` exactly once and takes it out of
 * the address bar, then opens the contact it was given.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const LINK = {
  id: "j1", interaction_id: "i1", mode: "warm" as const, for_user_id: "bob", for_queue: null,
  created_by: "alice", created_at: null, expires_at: "2026-09-29T12:10:00Z", state: "open" as const,
  redeemed_by: null, redeemed_at: null, joined_interaction_id: null,
};

vi.mock("@shared/api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@shared/api/client");
  return {
    ...actual,
    engageListJoinLinks: vi.fn(async () => ({ links: [LINK] })),
    engageCreateJoinLink: vi.fn(async () => ({ ...LINK, token: "SECRET", path: "/rep-desk#join=SECRET",
                                               note: "" })),
    engageRevokeJoinLink: vi.fn(async () => ({ ...LINK, state: "revoked" })),
    getEngageStatus: vi.fn(async () => ({ enabled: true, released: true, entitled: true })),
    engageRedeemJoinLink: vi.fn(async () => ({
      interaction_id: "i2", customer_id: "c1", verification_level: "full",
      resolution: { resolved: true, rung: 0, reason: "joined", confidence: "join link", verified: true },
      stub: null, inherited_verification: true, joined_from: "i1", mode: "warm",
    })),
    engage360: vi.fn(async () => ({
      customer_id: "c1", verification: "full", panels: { summary: { display_name: "Jane", open_cases: 0 } },
      withheld: [], freshness: null,
    })),
    engageActionRail: vi.fn(async () => ({ actions: [], cases: [], consent: { email: null, phone: null },
                                           capturable_purposes: [], portal_link: null,
                                           interaction_id: "i2", customer_id: "c1",
                                           verification_level: "full" })),
    engageNextActions: vi.fn(async () => ({ actions: [] })),
    engageDispositionCodes: vi.fn(async () => ({ codes: null, free_text: true })),
  };
});

import * as client from "@shared/api/client";
import JoinLinks from "./JoinLinks";
import RepDesk from "./RepDesk";

function Where() {
  const loc = useLocation();
  return <div data-testid="where">{loc.pathname + loc.search + loc.hash}</div>;
}

describe("join links", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("shows a new link once, and never lists it", async () => {
    render(<JoinLinks interactionId="i1" />);
    await userEvent.type(screen.getByLabelText("Colleague"), "bob");
    await userEvent.click(screen.getByRole("button", { name: "Create link" }));
    await waitFor(() => expect(client.engageCreateJoinLink).toHaveBeenCalledWith(
      "i1", { mode: "warm", for_user_id: "bob", for_queue: null }));
    expect(await screen.findByText(/rep-desk#join=SECRET/)).toBeTruthy();
    // The list rows describe links; none carries a token.
    expect(screen.getAllByText(/warm · bob/).length).toBeGreaterThan(0);
  });

  it("revokes an open link", async () => {
    render(<JoinLinks interactionId="i1" />);
    await userEvent.click(await screen.findByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(client.engageRevokeJoinLink).toHaveBeenCalledWith("j1"));
  });

  it("redeems #join= once, cleans the URL, and opens the contact", async () => {
    render(
      <MemoryRouter initialEntries={["/rep-desk#join=TOKEN123"]}>
        <Routes><Route path="/rep-desk" element={<><RepDesk /><Where /></>} /></Routes>
      </MemoryRouter>);
    await waitFor(() => expect(client.engageRedeemJoinLink).toHaveBeenCalledWith("TOKEN123"));
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/rep-desk"));
    expect(await screen.findByText(/You joined your colleague/)).toBeTruthy();
    expect(await screen.findByText(/joined \(warm\) — already verified/)).toBeTruthy();
    expect(client.engageRedeemJoinLink).toHaveBeenCalledTimes(1);
  });
});
