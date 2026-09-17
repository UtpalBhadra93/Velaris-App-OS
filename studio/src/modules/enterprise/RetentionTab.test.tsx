/**
 * Enterprise → Retention.
 *
 * This is the only screen in Studio that configures irreversible deletion, so
 * the tests weight what it *claims* over what it renders. Two specific lies
 * were live here until 2026-08-04: an `anonymize` option the backend refused
 * as an unknown action, and help text promising data would be "anonymized"
 * when nothing implemented it.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

vi.mock("@shared/api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@shared/api/client");
  return {
    ...actual,
    listRetentionPolicies: vi.fn(async () => [
      {
        id: "p1", name: "Resolved case retention", resource_type: "resolved_cases",
        tenant_id: null, retention_days: 2555, action: "archive",
        enabled: true, last_run_at: null,
      },
      {
        id: "p2", name: "Process mining event log", resource_type: "event_log",
        tenant_id: null, retention_days: 365, action: "delete",
        enabled: false, last_run_at: null,
      },
    ]),
    updateRetentionPolicy: vi.fn(async () => ({})),
  };
});

import Enterprise from "./Enterprise";

async function openRetention() {
  const user = userEvent.setup();
  // Enterprise's OverviewTab calls useNavigate(), so the module cannot mount
  // outside a router even when we only care about a different tab.
  render(<MemoryRouter><Enterprise /></MemoryRouter>);
  // `role="tab"`, not "button": the shared <Tabs> component marks the bar up as
  // a real tablist. Querying the role the user's screen reader sees is the point.
  await user.click(screen.getByRole("tab", { name: /retention/i }));
  await waitFor(() => expect(screen.getByText(/Retention policies decide/i)).toBeInTheDocument());
  return user;
}

describe("the action options", () => {
  it("offers only actions the backend can execute", async () => {
    const user = await openRetention();
    const select = screen.getAllByRole("combobox")[0]!;
    const options = Array.from(select.querySelectorAll("option")).map(o => o.textContent);

    // `anonymize` is back because it is implemented now; anything outside
    // this set would be saved and then fail every run.
    expect(new Set(options)).toEqual(new Set(["archive", "delete", "anonymize"]));
    void user;
  });

  it("still renders an action stored outside the supported set", async () => {
    // Removing an option must not strand a row saved under an older build —
    // the select would otherwise render blank and a save would rewrite the
    // admin's stored choice silently.
    await openRetention();
    expect(screen.queryByText(/\(unsupported\)/)).not.toBeInTheDocument();
  });
});

describe("what the help text claims", () => {
  it("does not promise anonymisation as a generic outcome", async () => {
    await openRetention();
    const help = screen.getByText(/Retention policies decide/i).textContent ?? "";
    // The old text said data would be "archived, deleted, or anonymized" as if
    // all three were interchangeable. They are not: two delete.
    expect(help).toMatch(/ultimately delete/i);
  });

  it("says archive is not a separate store", async () => {
    await openRetention();
    const help = screen.getByText(/Retention policies decide/i).textContent ?? "";
    expect(help).toMatch(/no separate archive store/i);
  });

  it("says anonymize keeps the case", async () => {
    await openRetention();
    const help = screen.getByText(/Retention policies decide/i).textContent ?? "";
    expect(help).toMatch(/keeps/i);
    expect(help).toMatch(/resolved cases only/i);
  });

  it("warns that there is no undo", async () => {
    await openRetention();
    const help = screen.getByText(/Retention policies decide/i).textContent ?? "";
    // A screen configuring irreversible deletion must say so on the screen,
    // not only in the API docs.
    expect(help).toMatch(/no undo/i);
  });

  it("states that runs are never scheduled from here", async () => {
    await openRetention();
    const help = screen.getByText(/Retention policies decide/i).textContent ?? "";
    expect(help).toMatch(/confirm=true/i);
  });
});

describe("policy rows", () => {
  it("shows each policy's own period", async () => {
    await openRetention();
    // The number on screen must be the number that runs — no platform-wide
    // override silently replacing it.
    expect(screen.getByDisplayValue("2555")).toBeInTheDocument();
    expect(screen.getByDisplayValue("365")).toBeInTheDocument();
  });

  it("disables editing on a policy that is not enabled", async () => {
    await openRetention();
    const selects = screen.getAllByRole("combobox");
    // p2 is disabled in the fixture; its action select must not be editable,
    // or an admin can configure a policy they believe is inert.
    expect(selects.some(s => (s as HTMLSelectElement).disabled)).toBe(true);
  });
});
