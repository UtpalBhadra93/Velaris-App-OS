/**
 * HxShield → Responses.
 *
 * The other HxShield tabs report. This one ACTS: approving a `suspend_user`
 * stops a person working. So the tests weight the two things that make it safe
 * rather than the table it draws —
 *
 *  * a destructive proposal is confirmed before anything is posted, and
 *    cancelling posts nothing at all; and
 *  * `shadowed` rows are announced as "working as configured" rather than
 *    looking like a queue nobody worked. That distinction is the whole reason
 *    the API returns `responses_enabled` on every read.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import HxShield from "./HxShield";

const PROPOSED = {
  id: "r1", incident_id: "i1", response_type: "suspend_user",
  target_kind: "user", target_id: "ann", state: "proposed",
  destructive: true, reason: "12 failed logins in 60s", score: 0.91,
  approved_by: null, approved_at: null, declined_by: null,
  executed_at: null, result: null, created_at: "2026-09-10T09:00:00Z",
};

const CATALOGUE = [{
  name: "suspend_user", label: "Suspend the user", target_kind: "user",
  destructive: true, summary: "Signs the account out and refuses new sessions.",
  reversible_by: "an administrator reinstating the account", needs_approval: true,
}];

function mockShield(body: Record<string, unknown>) {
  const calls: { url: string; init?: RequestInit }[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    if (url.includes("/responses/") ) {
      return { ok: true, status: 200, json: async () => ({}) } as unknown as Response;
    }
    if (url.includes("/responses")) {
      return { ok: true, status: 200, json: async () => body } as unknown as Response;
    }
    return { ok: true, status: 200, json: async () => [] } as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

async function openResponses() {
  const user = userEvent.setup();
  render(<HxShield />);
  await user.click(screen.getByRole("tab", { name: /responses/i }));
  return user;
}

beforeEach(() => {
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(window, "prompt").mockReturnValue("not a real attack");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("shadow mode", () => {
  it("says a queue of shadowed rows is the platform working as configured", async () => {
    mockShield({ responses_enabled: false, catalogue: CATALOGUE,
                 items: [{ ...PROPOSED, state: "shadowed" }] });
    await openResponses();

    await waitFor(() =>
      expect(screen.getByText(/Shadow mode/i)).toBeInTheDocument());
    expect(screen.getByText(/not a backlog/i)).toBeInTheDocument();
  });

  it("says nothing about shadow mode when responses are switched on", async () => {
    mockShield({ responses_enabled: true, catalogue: CATALOGUE, items: [PROPOSED] });
    await openResponses();

    // "Suspend the user" is on the row AND in the catalogue below it, so the
    // queue row is identified by the buttons only a proposal has.
    await waitFor(() => expect(screen.getByRole("button", { name: /approve/i })).toBeInTheDocument());
    expect(screen.queryByText(/Shadow mode/i)).not.toBeInTheDocument();
  });
});

describe("approving something destructive", () => {
  it("asks first, and names what the response will do", async () => {
    const calls = mockShield({ responses_enabled: true, catalogue: CATALOGUE, items: [PROPOSED] });
    const user = await openResponses();
    await waitFor(() => expect(screen.getByRole("button", { name: /approve/i })).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /approve/i }));

    const asked = (window.confirm as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(asked).toMatch(/Suspend the user/);
    expect(asked).toMatch(/Signs the account out/);
    // The undo is part of the question: "can this be taken back" is what the
    // person clicking actually needs to know.
    expect(asked).toMatch(/reinstating the account/);
    expect(calls.some(c => c.url.includes("/approve"))).toBe(true);
  });

  it("posts nothing when the question is answered no", async () => {
    (window.confirm as unknown as ReturnType<typeof vi.fn>).mockReturnValue(false);
    const calls = mockShield({ responses_enabled: true, catalogue: CATALOGUE, items: [PROPOSED] });
    const user = await openResponses();
    await waitFor(() => expect(screen.getByRole("button", { name: /approve/i })).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /approve/i }));

    expect(calls.some(c => c.url.includes("/approve"))).toBe(false);
  });
});

describe("declining", () => {
  it("sends the reason, because a decline is evidence about the detector", async () => {
    const calls = mockShield({ responses_enabled: true, catalogue: CATALOGUE, items: [PROPOSED] });
    const user = await openResponses();
    await waitFor(() => expect(screen.getByRole("button", { name: /decline/i })).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /decline/i }));

    const call = calls.find(c => c.url.includes("/decline"));
    expect(call).toBeTruthy();
    expect(JSON.parse(String(call!.init!.body))).toEqual({ reason: "not a real attack" });
  });
});

describe("a refusal from the server", () => {
  it("is shown on the screen rather than swallowed", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.includes("/responses/")) {
        return { ok: false, status: 409,
                 json: async () => ({ detail: "Responses are switched off" }) } as unknown as Response;
      }
      if (url.includes("/responses")) {
        return { ok: true, status: 200,
                 json: async () => ({ responses_enabled: true, catalogue: CATALOGUE,
                                      items: [PROPOSED] }) } as unknown as Response;
      }
      return { ok: true, status: 200, json: async () => [] } as unknown as Response;
    }));
    const user = await openResponses();
    await waitFor(() => expect(screen.getByRole("button", { name: /approve/i })).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /approve/i }));

    await waitFor(() =>
      expect(screen.getByText(/Responses are switched off/)).toBeInTheDocument());
  });
});

describe("an empty queue", () => {
  it("explains itself instead of showing an empty table", async () => {
    mockShield({ responses_enabled: true, catalogue: CATALOGUE, items: [] });
    await openResponses();

    await waitFor(() =>
      expect(screen.getByText(/Nothing proposed/i)).toBeInTheDocument());
  });
});
