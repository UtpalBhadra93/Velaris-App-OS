/**
 * Moving a case between tenants — what the screen offers to whom.
 *
 * The server decides every action; what is worth pinning here is that the
 * screen never offers a button the server would refuse by rule:
 *
 *  * the requester is not offered Approve (two-person rule);
 *  * only the destination is offered Accept / Refuse, and only once approved;
 *  * the Move tab's button stays disabled until preflight says nothing blocks.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ user: { user_id: "me" } }),
}));

vi.mock("@shared/api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@shared/api/client");
  return {
    ...actual,
    listTenantTransfers: vi.fn(),
    preflightTenantTransfer: vi.fn(),
    requestTenantTransfer: vi.fn(),
    decideTenantTransfer: vi.fn(async () => ({})),
  };
});

import TenantTransfers, { MoveTenantTab } from "./TenantTransfer";
import * as client from "@shared/api/client";

const base = {
  case_id: "c1", case_number: "HLX-1", case_type_name: "Claims", reason: "customer moved",
  source_tenant_id: "s", destination_tenant_id: "d", requested_at: null, approved_at: null,
  decided_at: null, completed_at: null, decision_note: null, manifest: null,
};

beforeEach(() => vi.clearAllMocks());

describe("Outgoing", () => {
  it("does not offer Approve to the requester, but does to a second admin", async () => {
    vi.mocked(client.listTenantTransfers).mockResolvedValue({ items: [
      { ...base, id: "t1", state: "requested", side: "source", requested_by: "me" },
      { ...base, id: "t2", case_number: "HLX-2", state: "requested", side: "source", requested_by: "someone-else" },
    ] } as never);
    render(<TenantTransfers />);
    await screen.findByText("HLX-2");
    expect(screen.getAllByRole("button", { name: "Approve" })).toHaveLength(1);
    expect(screen.getByText(/Waiting for a different administrator/)).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Cancel request" })).toHaveLength(2);
    expect(screen.queryByRole("button", { name: /Accept/ })).toBeNull();
  });
});

describe("Incoming", () => {
  it("offers Accept and Refuse only on an approved request", async () => {
    vi.mocked(client.listTenantTransfers).mockImplementation(async (dir) => ({
      items: dir === "incoming"
        ? [{ ...base, id: "t3", state: "approved", side: "destination" },
           { ...base, id: "t4", case_number: "HLX-4", state: "accepted", side: "destination" }]
        : [],
    }) as never);
    const user = userEvent.setup();
    render(<TenantTransfers />);
    await user.click(await screen.findByRole("tab", { name: "Incoming" }));
    await screen.findByText("HLX-4");
    expect(screen.getAllByRole("button", { name: /Accept/ })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Refuse" })).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });
});

describe("Move tenant tab", () => {
  it("keeps the button disabled while preflight reports a blocker", async () => {
    vi.mocked(client.preflightTenantTransfer).mockResolvedValue({
      case_id: "c1", destination_tenant_id: "d", can_move: false,
      blockers: ["payment requests are attached"], open_transfer_id: null,
    });
    const user = userEvent.setup();
    render(<MoveTenantTab caseId="c1" />);
    await user.type(screen.getByPlaceholderText("e.g. acme"), "dstco");
    await user.type(screen.getByPlaceholderText("At least 10 characters"), "customer moved away");
    await screen.findByText("payment requests are attached");
    expect((screen.getByRole("button", { name: "Request the move" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("enables it once nothing blocks", async () => {
    vi.mocked(client.preflightTenantTransfer).mockResolvedValue({
      case_id: "c1", destination_tenant_id: "d", can_move: true, blockers: [], open_transfer_id: null,
    });
    const user = userEvent.setup();
    render(<MoveTenantTab caseId="c1" />);
    await user.type(screen.getByPlaceholderText("e.g. acme"), "dstco");
    await user.type(screen.getByPlaceholderText("At least 10 characters"), "customer moved away");
    await screen.findByText("Nothing blocks this move");
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "Request the move" }) as HTMLButtonElement).disabled).toBe(false));
  });
});
