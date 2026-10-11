/**
 * Oct item 18a, phases 2–3 — the rollout details panel.
 *
 * Pinned: the policy is sent in the server's shape (lists split, numbers as
 * numbers, no guard when none is chosen); outcomes show each verdict; the
 * rebase previews before it moves anything.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as client from "@shared/api/client";
import type { ContentRollout } from "@shared/api/client";

const r: ContentRollout = {
  id: "r1", artifact_kind: "case_type", artifact_name: "Claims", baseline_id: "b",
  candidate_id: "c", baseline_version: "1", candidate_version: "2", strategy: "percentage",
  percent: 10, state: "rolling", open: true, source: "local", requested_by: "a",
  approved_by: "b", closed_by: null, superseded_by: null, note: null, created_at: null,
  approved_at: null, closed_at: null, arms: { baseline: 1, candidate: 1 }, policy: null,
};

vi.mock("@shared/api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@shared/api/client");
  return {
    ...actual,
    setRolloutPolicy: vi.fn(async () => ({ rollout: r })),
    rolloutOutcomes: vi.fn(async () => ({
      arms: { baseline: { cases: 60, resolved: 60, open: 0, sla_breached: 3, reopened: 0 },
              candidate: { cases: 60, resolved: 60, open: 0, sla_breached: 30, reopened: 0 } },
      metrics: { sla_breach_rate: { baseline: 0.05, candidate: 0.5, delta: 0.45,
                                    p_value: 0, verdict: "worse" } },
      min_cases: 30, note: "n" })),
    rolloutShadow: vi.fn(),
    rebaseRollout: vi.fn(async () => ({ dry_run: true, note: "", movable: [{ case_id: "x",
                                        case_number: null }], blocked: [] })),
  };
});

import RolloutDetails from "./RolloutDetails";

describe("RolloutDetails", () => {
  beforeEach(() => vi.clearAllMocks());

  it("sends the policy in the server's shape", async () => {
    render(<RolloutDetails r={r} onChanged={() => {}} />);
    await userEvent.type(screen.getByLabelText("Pilot subjects"), "a@x.test\nb@x.test");
    await userEvent.type(screen.getByLabelText("Ramp steps"), "25, 50");
    await userEvent.click(screen.getByText("Save policy"));
    expect(client.setRolloutPolicy).toHaveBeenCalledWith("r1", {
      shadow: false,
      targeting: { include: ["a@x.test", "b@x.test"], exclude: [] },
      ramp: { steps: [25, 50], every_hours: 24 },
      guard: null,
    });
  });

  it("shows the A/B verdicts", async () => {
    render(<RolloutDetails r={r} onChanged={() => {}} />);
    await userEvent.click(screen.getByText("Outcomes (A/B)"));
    expect(await screen.findByText("worse")).toBeTruthy();
  });

  it("previews a rebase before moving anything", async () => {
    render(<RolloutDetails r={{ ...r, state: "rolled_back", open: false }} onChanged={() => {}} />);
    await userEvent.click(screen.getByText("Move cases back"));
    await userEvent.type(screen.getByLabelText("Rebase reason"), "bad version");
    await userEvent.click(screen.getByText("Preview"));
    expect(client.rebaseRollout).toHaveBeenCalledWith("r1", "bad version", true);
    expect(await screen.findByText("Move 1 case(s) back")).toBeTruthy();
  });
});
