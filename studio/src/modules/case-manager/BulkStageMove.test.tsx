/**
 * Bulk "Move to stage".
 *
 * What is worth pinning is the confirmation, not the dropdown:
 *
 *  * **the confirm sends back what the preview confirmed** — the eligible ids
 *    and the token, not the raw selection. Sending the selection would include
 *    the refused cases and the server would reject the whole run.
 *  * **a changed stage voids the preview**, so a confirm can never carry a
 *    token for one stage while the screen shows another.
 *  * **nothing to move means no button**, not a button that 409s.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi, beforeEach } from "vitest";

vi.mock("@shared/api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@shared/api/client");
  return {
    ...actual,
    getCaseType: vi.fn(async () => ({
      id: "ct1", name: "Claims",
      definition_json: { stages: [{ id: "triage", name: "Triage" }, { id: "review", name: "Review" }] },
    })),
    bulkStagePreview: vi.fn(),
    bulkStageExecute: vi.fn(async () => ({
      target_stage_id: "review", bulk_run_id: "r1", requested: 2, moved: 2,
      unchanged: 0, failed: 0, results: [],
    })),
  };
});

import BulkStageMove from "./BulkStageMove";
import * as client from "@shared/api/client";

const preview = {
  target_stage_id: "review", requested: 3, would_move: 2, unchanged: 0, refused: 1,
  effects: [{ case_type_id: "ct1", case_type_name: "Claims", stage_name: "Review",
              cases: 2, starts_sla: "Review SLA", on_enter_status: "in_progress",
              signals_workflows: true }],
  results: [
    { case_id: "aaaaaaaa-1", ok: true }, { case_id: "bbbbbbbb-2", ok: true },
    { case_id: "cccccccc-3", ok: false, code: "VEL-CASE-ACTION-SEALED-409", error: "This case is archived." },
  ],
  eligible_case_ids: ["aaaaaaaa-1", "bbbbbbbb-2"],
  confirmation_token: "v1.123.sig", confirm_expires_at: 123,
};

describe("BulkStageMove", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("previews, shows what fires, and confirms exactly the previewed set", async () => {
    (client.bulkStagePreview as any).mockResolvedValue(preview);
    const onDone = vi.fn();
    render(<BulkStageMove selectedIds={["aaaaaaaa-1", "bbbbbbbb-2", "cccccccc-3"]}
                          caseTypeIds={["ct1"]} onDone={onDone} />);

    await screen.findByRole("option", { name: "Review" });
    await userEvent.selectOptions(screen.getByLabelText("Target stage"), "review");
    await userEvent.click(screen.getByRole("button", { name: "Preview move" }));

    expect(await screen.findByText(/starts SLA/)).toBeTruthy();
    expect(screen.getByText("Review SLA")).toBeTruthy();
    expect(screen.getByText(/This case is archived/)).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Move 2 cases" }));
    await waitFor(() => expect(client.bulkStageExecute).toHaveBeenCalledTimes(1));
    const [sent] = (client.bulkStageExecute as any).mock.calls[0];
    expect(sent.eligible_case_ids).toEqual(["aaaaaaaa-1", "bbbbbbbb-2"]);
    expect(sent.confirmation_token).toBe("v1.123.sig");
    expect(onDone).toHaveBeenCalled();
  });

  it("throws the preview away when the stage changes", async () => {
    (client.bulkStagePreview as any).mockResolvedValue(preview);
    render(<BulkStageMove selectedIds={["aaaaaaaa-1"]} caseTypeIds={["ct1"]} onDone={() => {}} />);
    await screen.findByRole("option", { name: "Review" });
    await userEvent.selectOptions(screen.getByLabelText("Target stage"), "review");
    await userEvent.click(screen.getByRole("button", { name: "Preview move" }));
    await screen.findByRole("button", { name: "Move 2 cases" });

    await userEvent.selectOptions(screen.getByLabelText("Target stage"), "triage");
    expect(screen.queryByRole("button", { name: /^Move \d/ })).toBeNull();
  });

  it("offers no confirm when nothing would move", async () => {
    (client.bulkStagePreview as any).mockResolvedValue({
      ...preview, would_move: 0, refused: 3, effects: [], eligible_case_ids: [],
      confirmation_token: null, confirm_expires_at: null,
    });
    render(<BulkStageMove selectedIds={["x"]} caseTypeIds={["ct1"]} onDone={() => {}} />);
    await screen.findByRole("option", { name: "Review" });
    await userEvent.selectOptions(screen.getByLabelText("Target stage"), "review");
    await userEvent.click(screen.getByRole("button", { name: "Preview move" }));
    expect(await screen.findByText("Nothing to move.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Move \d/ })).toBeNull();
  });
});
