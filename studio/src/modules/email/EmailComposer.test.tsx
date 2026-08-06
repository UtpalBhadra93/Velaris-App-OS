/**
 * Email composer — the consent gate has to be reachable from here.
 *
 * The backend gate landed first and was, for a while, unreachable: the
 * composer never sent `purpose`, so every send defaulted to `case_processing`
 * and marketing filtering could only be triggered by calling the API directly.
 * A control nobody can invoke is not a control, so these tests assert the wire
 * rather than the widget.
 *
 * The second assertion is the other half: when the backend refuses, the
 * catalog code has to survive the trip to the user. This component hand-rolls
 * its fetch and used to throw `new Error(detail)`, discarding `error.code` —
 * the same flattening that `useApi` did before it was fixed.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import EmailComposer from "./EmailComposer";

const ACCOUNT = {
  id: "acc-1", name: "Support", address: "support@velaris.test",
  is_default_outbound: true,
};

/** Accounts + templates load first; the third call is the send. */
function mockFetch(sendResponse: { ok: boolean; status: number; body: unknown }) {
  return vi.fn((url: string, opts?: RequestInit) => {
    if (typeof url === "string" && url.includes("/email/accounts")) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve([ACCOUNT]) } as Response);
    }
    if (typeof url === "string" && url.includes("/email/templates")) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve([]) } as Response);
    }
    return Promise.resolve({
      ok: sendResponse.ok, status: sendResponse.status,
      json: () => Promise.resolve(sendResponse.body),
    } as Response);
  });
}

describe("EmailComposer — purpose reaches the API", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("defaults to case_processing so case mail is never consent-gated", async () => {
    const fetchMock = mockFetch({ ok: true, status: 201, body: { id: "1", status: "sent" } });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    render(<EmailComposer />);
    await waitFor(() => expect(screen.getByDisplayValue(/Support/)).toBeInTheDocument());

    const boxes = screen.getAllByRole("textbox");
    await user.type(boxes[0], "a@b.com");
    await user.type(boxes[2], "Hi");
    await user.type(boxes[3], "Body");
    await user.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => {
      const sendCall = fetchMock.mock.calls.find(c => String(c[0]).includes("/email/send"));
      expect(sendCall).toBeTruthy();
      expect(JSON.parse(String((sendCall![1] as RequestInit).body)).purpose).toBe("case_processing");
    });
  });

  it("sends marketing when chosen, which is what makes the gate reachable", async () => {
    const fetchMock = mockFetch({ ok: true, status: 201, body: { id: "1", status: "sent" } });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    render(<EmailComposer />);
    await waitFor(() => expect(screen.getByDisplayValue(/Support/)).toBeInTheDocument());

    await user.selectOptions(screen.getByDisplayValue("Case correspondence"), "marketing");

    const boxes = screen.getAllByRole("textbox");
    await user.type(boxes[0], "a@b.com");
    await user.type(boxes[2], "Hi");
    await user.type(boxes[3], "Body");
    await user.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => {
      const sendCall = fetchMock.mock.calls.find(c => String(c[0]).includes("/email/send"));
      expect(JSON.parse(String((sendCall![1] as RequestInit).body)).purpose).toBe("marketing");
    });
  });
});

describe("EmailComposer — a refusal keeps its code", () => {
  it("surfaces VEL-CONSENT-REFUSED-403 rather than flattening it to a message", async () => {
    vi.stubGlobal("fetch", mockFetch({
      ok: false, status: 403,
      body: {
        detail: "Refused: no recipient has a lawful basis for 'marketing'. 1 address(es) were dropped.",
        error: { code: "VEL-CONSENT-REFUSED-403", docs: "#VEL-CONSENT-REFUSED-403" },
      },
    }));
    const user = userEvent.setup();

    render(<EmailComposer />);
    await waitFor(() => expect(screen.getByDisplayValue(/Support/)).toBeInTheDocument());

    const boxes = screen.getAllByRole("textbox");
    await user.type(boxes[0], "a@b.com");
    await user.type(boxes[2], "Hi");
    await user.type(boxes[3], "Body");
    await user.click(screen.getByRole("button", { name: /send/i }));

    expect(await screen.findByText(/no recipient has a lawful basis/i)).toBeInTheDocument();
    // The code itself, not just the sentence — this is the deep link.
    expect(await screen.findByText("VEL-CONSENT-REFUSED-403")).toBeInTheDocument();
  });
});
