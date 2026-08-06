/**
 * HxDR panel.
 *
 * One assertion matters more than the rest: an untested backup must never
 * render as OK. That is the exact failure disaster recovery exists to prevent,
 * and a UI that rounds amber up to green reintroduces it after the backend
 * carefully avoided it.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import DRPanel from "./DRPanel";

function posture(regions: unknown[], extra: Record<string, unknown> = {}) {
  return {
    rpo_hours: 24, rto_hours: 4, restore_test_valid_days: 90,
    regions,
    all_regions_ok: false,
    regions_at_risk: [],
    ...extra,
  };
}

function stub(body: unknown) {
  vi.stubGlobal("fetch", vi.fn(() =>
    Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) } as Response),
  ));
}

const UNTESTED = {
  region: "eu-west", region_id: "r1", is_primary: true,
  state: "untested",
  detail: "never restore-tested — an untested backup is a hope",
  last_backup_at: "2026-08-04T08:00:00Z", last_restore_test_at: null,
};

describe("DRPanel — untested is not OK", () => {
  it("renders UNTESTED for a recent backup that was never restored", async () => {
    stub(posture([UNTESTED]));
    render(<DRPanel />);

    await waitFor(() => expect(screen.getByText("UNTESTED")).toBeInTheDocument());
    expect(screen.queryByText("OK")).not.toBeInTheDocument();
  });

  it("shows why, not just the state", async () => {
    stub(posture([UNTESTED]));
    render(<DRPanel />);

    await waitFor(() =>
      expect(screen.getByText(/an untested backup is a hope/i)).toBeInTheDocument(),
    );
  });
});

describe("DRPanel — no single green light", () => {
  it("names the regions at risk rather than one aggregate status", async () => {
    stub(posture(
      [
        UNTESTED,
        { region: "us-east", region_id: "r2", is_primary: false, state: "ok",
          detail: "recent and demonstrably restorable",
          last_backup_at: "2026-08-04T08:00:00Z",
          last_restore_test_at: "2026-07-20T08:00:00Z" },
      ],
      { regions_at_risk: ["eu-west"] },
    ));
    render(<DRPanel />);

    await waitFor(() =>
      expect(screen.getByText(/At risk: eu-west/i)).toBeInTheDocument(),
    );
  });

  it("confirms all-clear only when every region is ok", async () => {
    stub(posture(
      [{ region: "us-east", region_id: "r2", is_primary: true, state: "ok",
         detail: "recent and demonstrably restorable",
         last_backup_at: "2026-08-04T08:00:00Z",
         last_restore_test_at: "2026-07-20T08:00:00Z" }],
      { all_regions_ok: true },
    ));
    render(<DRPanel />);

    await waitFor(() =>
      expect(screen.getByText(/demonstrably restorable backup/i)).toBeInTheDocument(),
    );
  });
});

describe("DRPanel — nothing registered", () => {
  it("explains the invisible-backup trap instead of showing an empty table", async () => {
    stub(posture([]));
    render(<DRPanel />);

    await waitFor(() =>
      expect(screen.getByText(/No regions are registered/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/stored but never shown/i)).toBeInTheDocument();
  });

  it("offers no restore-drill buttons for a region with no backup", async () => {
    stub(posture([{
      region: "eu-west", region_id: "r1", is_primary: false,
      state: "no_backup", detail: "no successful backup recorded",
      last_backup_at: null, last_restore_test_at: null,
    }]));
    render(<DRPanel />);

    await waitFor(() => expect(screen.getByText("NO BACKUP")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /^passed$/i })).not.toBeInTheDocument();
  });
});
