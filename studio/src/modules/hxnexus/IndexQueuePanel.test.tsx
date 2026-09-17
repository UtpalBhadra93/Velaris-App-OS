/**
 * HxNexus → Index Queue.
 *
 * Pinned: the missing-schedule warning (a queue nothing drains must say so),
 * the failure reason on the row, and that each button calls the per-case route
 * for the job's own case.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const calls: string[] = [];
let queue: any;

vi.mock("@shared/api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@shared/api/client");
  return {
    ...actual,
    apiFetch: vi.fn(async (path: string, opts?: RequestInit) => {
      calls.push(`${opts?.method ?? "GET"} ${path}`);
      return new Response(JSON.stringify(opts?.method === "POST" ? {} : queue), { status: 200 });
    }),
  };
});

import IndexQueuePanel from "./IndexQueuePanel";

const job = (over: any) => ({
  id: "j1", document_id: "d1", case_id: "c1", status: "pending", attempts: 0,
  max_attempts: 3, reindex: false, chunks_indexed: null, last_error: null,
  requested_by: "ann", created_at: "2026-09-17T10:00:00Z", started_at: null,
  finished_at: null, filename: "scan.pdf", case_number: "ACME-1", ...over,
});

describe("IndexQueuePanel", () => {
  beforeEach(() => { calls.length = 0; });

  it("warns when no schedule drains the queue", async () => {
    queue = { count: 1, counts: { pending: 1 }, drain_scheduled: false, jobs: [job({})] };
    render(<IndexQueuePanel />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/nothing drains this queue, and 1 job is waiting/);
  });

  it("shows why a job failed and retries it on its own case", async () => {
    queue = { count: 1, counts: { failed: 1 }, drain_scheduled: true,
              jobs: [job({ status: "failed", attempts: 3, last_error: "OCR timed out" })] };
    render(<IndexQueuePanel />);
    expect(await screen.findByText("OCR timed out")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() =>
      expect(calls).toContain("POST /hxnexus/cases/c1/documents/index-jobs/j1/retry"));
  });

  it("offers cancel only on a pending job", async () => {
    queue = { count: 2, counts: { pending: 1, done: 1 }, drain_scheduled: true,
              jobs: [job({}), job({ id: "j2", status: "done", chunks_indexed: 4 })] };
    render(<IndexQueuePanel />);
    await screen.findByText(/4 chunks/);
    expect(screen.getAllByRole("button", { name: "Cancel" })).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });
});
