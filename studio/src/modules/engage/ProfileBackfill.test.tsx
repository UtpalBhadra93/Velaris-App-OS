/**
 * Oct item 3 — the backfill screen.
 *
 * Pinned: a proposal shows its evidence (source and verified-ness per
 * identifier); reject is disabled until a reason is typed; a merge cannot be
 * accepted without a reason; running a scan re-reads the list.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { BackfillProposal } from "@shared/api/client";

const CREATE: BackfillProposal = {
  id: "p1", kind: "create_profile", status: "proposed",
  payload: { display_name: "Jane Smith", primary_email: "jane@x.test", case_ids: ["c1", "c2"],
             identifiers: [
               { kind: "email", value: "jane@x.test", display: "jane@x.test", verified: true, via: "portal account" },
               { kind: "phone", value: "+447700900111", display: "+44 7700 900111", verified: false, via: "portal account" },
             ] },
  evidence: { sources: ["case submitter", "portal account"], case_count: 2 },
  decided_by: null, decided_at: null, reason: null, result: null, updated_at: null,
};
const MERGE: BackfillProposal = {
  ...CREATE, id: "p2", kind: "merge_profiles",
  payload: { winner_id: "a", loser_ids: ["b"],
             profiles: [{ id: "a", display_name: "J Smith" }, { id: "b", display_name: "Jane S" }],
             shared: [] },
};

vi.mock("@shared/api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@shared/api/client");
  return {
    ...actual,
    engageBackfillProposals: vi.fn(async () => ({ total: 2, proposals: [CREATE, MERGE] })),
    engageBackfillRuns: vi.fn(async () => ({ runs: [{
      id: "r1", started_by: "ann", started_at: null, finished_at: null,
      counts: { proposed_new: 2, numbers_skipped_no_other_evidence: 1 } }] })),
    engageStartBackfill: vi.fn(async () => ({})),
    engageAcceptProposal: vi.fn(async () => ({})),
    engageRejectProposal: vi.fn(async () => ({})),
  };
});

import * as client from "@shared/api/client";
import ProfileBackfill from "./ProfileBackfill";

describe("profile backfill", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("shows each proposal's evidence and the skipped numbers", async () => {
    render(<ProfileBackfill />);
    expect(await screen.findByText("Jane Smith")).toBeTruthy();
    expect(screen.getByText("verified")).toBeTruthy();
    expect(screen.getByText("unverified")).toBeTruthy();
    expect(screen.getByText(/1 numbers with nothing else behind them/)).toBeTruthy();
    expect(screen.getByText(/2 case\(s\) to link/)).toBeTruthy();
  });

  it("accepts a new profile, and rejects only with a reason", async () => {
    render(<ProfileBackfill />);
    await screen.findByText("Jane Smith");
    const rejects = screen.getAllByRole("button", { name: "Reject" });
    expect(rejects[0]).toBeDisabled();
    await userEvent.click(screen.getAllByRole("button", { name: "Accept" })[0]);
    await waitFor(() => expect(client.engageAcceptProposal).toHaveBeenCalledWith("p1", undefined));
    await userEvent.type(screen.getAllByLabelText("Reason")[0], "test account");
    await userEvent.click(screen.getAllByRole("button", { name: "Reject" })[0]);
    await waitFor(() => expect(client.engageRejectProposal).toHaveBeenCalledWith("p1", "test account"));
  });

  it("will not merge without a reason", async () => {
    render(<ProfileBackfill />);
    const merge = await screen.findByRole("button", { name: "Merge" });
    expect(merge).toBeDisabled();
    await userEvent.type(screen.getAllByLabelText("Reason")[1], "same person");
    expect(merge).not.toBeDisabled();
    await userEvent.click(merge);
    await waitFor(() => expect(client.engageAcceptProposal).toHaveBeenCalledWith("p2", "same person"));
  });

  it("runs a scan and re-reads", async () => {
    render(<ProfileBackfill />);
    await userEvent.click(await screen.findByRole("button", { name: "Run a scan" }));
    await waitFor(() => expect(client.engageStartBackfill).toHaveBeenCalled());
    await waitFor(() => expect(client.engageBackfillProposals).toHaveBeenCalledTimes(2));
  });
});
