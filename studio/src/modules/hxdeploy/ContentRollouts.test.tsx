/**
 * Oct item 18a — the Rollouts screen.
 *
 * Pinned: a requested rollout offers Approve and nothing that changes it; a
 * flag offers a switch that sends 0/100, never a percentage; the lock's refusal
 * is shown as its message, not "[object Object]"; history loads on demand.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as client from "@shared/api/client";
import type { ContentRollout } from "@shared/api/client";

const base: ContentRollout = {
  id: "r1", artifact_kind: "case_type", artifact_name: "Claims",
  baseline_id: "b", candidate_id: "c", baseline_version: "1.0.0", candidate_version: "2.0.0",
  strategy: "flag", percent: 0, state: "requested", open: true, source: "local",
  requested_by: "alice", approved_by: null, closed_by: null, superseded_by: null, note: null,
  created_at: null, approved_at: null, closed_at: null, arms: { baseline: 3, candidate: 0 },
};

vi.mock("@shared/api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@shared/api/client");
  return {
    ...actual,
    listRollouts: vi.fn(),
    approveRollout: vi.fn(async () => ({ rollout: base })),
    setRolloutPercent: vi.fn(async () => ({ rollout: base })),
    closeRollout: vi.fn(async () => ({ rollout: base })),
    requestRollout: vi.fn(),
    getRollout: vi.fn(async () => ({ rollout: base, events: [
      { action: "requested", from_state: null, to_state: "requested", from_percent: null,
        to_percent: 0, actor: "alice", reason: null, at: "2026-09-29T10:00:00" }] })),
    supersedeRollout: vi.fn(),
    rolloutVersions: vi.fn(async () => ({ versions: [] })),
  };
});


import ContentRollouts from "./ContentRollouts";

describe("ContentRollouts", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ items: [] }) })));
  });

  it("a requested rollout can only be approved or rolled back", async () => {
    vi.mocked(client.listRollouts).mockResolvedValue({ rollouts: [base], count: 1 });
    render(<ContentRollouts />);
    await screen.findByText("Claims");
    expect(screen.queryByText("Promote")).toBeNull();
    expect(screen.queryByText("Switch on")).toBeNull();
    await userEvent.click(screen.getByText("Approve"));
    expect(client.approveRollout).toHaveBeenCalledWith("r1");
  });

  it("a flag switches 0/100, and history loads on demand", async () => {
    vi.mocked(client.listRollouts).mockResolvedValue({
      rollouts: [{ ...base, state: "dormant", approved_by: "bob" }], count: 1 });
    render(<ContentRollouts />);
    await userEvent.click(await screen.findByText("Switch on"));
    expect(client.setRolloutPercent).toHaveBeenCalledWith("r1", 100);
    await userEvent.click(screen.getByText("History"));
    await screen.findByText(/requested by alice/);
  });

  it("the lock's refusal is readable", async () => {
    vi.mocked(client.listRollouts).mockResolvedValue({
      rollouts: [{ ...base, state: "rolling", strategy: "percentage", percent: 20 }], count: 1 });
    vi.mocked(client.closeRollout).mockRejectedValue(
      new client.ApiError(409, client.detailText({ message: "'Claims' already has an open rollout" })));
    render(<ContentRollouts />);
    await userEvent.click(await screen.findByText("Promote"));
    await waitFor(() => expect(screen.getByText(/already has an open rollout/)).toBeTruthy());
  });
});
