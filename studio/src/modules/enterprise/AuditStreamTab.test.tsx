/**
 * Enterprise → Audit stream.
 *
 * The screen where this platform's security events start leaving it. What is
 * worth pinning is not the table:
 *
 *  * **creating is not enabling.** A target that starts receiving events the
 *    moment a URL is pasted is a decision nobody made yet, so the API creates
 *    it switched off and the button has to say so.
 *  * **the platform scope is acknowledged, not assumed.** `security_events` has
 *    no tenant column, so a target gets EVERY tenant's events. The API refuses a
 *    create without the acknowledgement; if the screen let somebody submit
 *    without it, the refusal would arrive as an unexplained error.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

// `vi.mock` is hoisted above every const in the file, so the spy has to be
// created inside the factory and read back afterwards.
vi.mock("@shared/api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@shared/api/client");
  return {
    ...actual,
    getEnterpriseSystemInfo: vi.fn(async () => ({})),
    // `items`, exactly as the API spells it. The first version of this mock
    // said `targets`, so the screen read a key the server never sends and the
    // test passed against a screen that would have rendered empty.
    listAuditStreamTargets: vi.fn(async () => ({
      items: [{
        id: "t1", name: "Corporate Splunk", kind: "splunk_hec",
        endpoint_url: "https://splunk.internal:8088/services/collector",
        has_token: true, verify_tls: true, enabled: false, scope: "platform",
        min_severity: null, event_types: [], cursor_timestamp: null,
        last_success_at: null, last_error: null, last_error_at: null,
        forwarded_count: 0, created_at: "2026-09-09T10:00:00Z",
      }],
      kinds: ["splunk_hec"],
    })),
    createAuditStreamTarget: vi.fn(async () => ({})),
    updateAuditStreamTarget: vi.fn(async () => ({})),
    deleteAuditStreamTarget: vi.fn(async () => undefined),
    testAuditStreamTarget: vi.fn(async () => ({ ok: true, message: "The SIEM accepted a test event." })),
    runAuditStreamTarget: vi.fn(async () => ({ ok: true, sent: 3 })),
  };
});

import { createAuditStreamTarget } from "@shared/api/client";
import Enterprise from "./Enterprise";

const createMock = createAuditStreamTarget as unknown as ReturnType<typeof vi.fn>;

async function openAuditStream() {
  const user = userEvent.setup();
  render(<MemoryRouter><Enterprise /></MemoryRouter>);
  await user.click(screen.getByRole("tab", { name: /audit stream/i }));
  await waitFor(() => expect(screen.getByText(/Corporate Splunk/)).toBeInTheDocument());
  return user;
}

describe("what the screen says about an existing target", () => {
  it("shows a target that is not forwarding as off, not as broken", async () => {
    await openAuditStream();
    expect(screen.getByText(/^off$/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /switch on/i })).toBeInTheDocument();
  });

  it("says forwarding needs a schedule, so an enabled-but-quiet target is explicable", async () => {
    await openAuditStream();
    expect(screen.getByText(/audit_stream/)).toBeInTheDocument();
  });

  it("will not offer 'forward now' on a target that is switched off", async () => {
    await openAuditStream();
    expect(screen.getByRole("button", { name: /forward now/i })).toBeDisabled();
  });
});

describe("creating a target", () => {
  it("cannot be submitted until the platform scope is acknowledged", async () => {
    const user = await openAuditStream();
    await user.click(screen.getByRole("button", { name: /add a target/i }));

    await user.type(screen.getByPlaceholderText(/Corporate Splunk/), "SOC");
    await user.type(screen.getByPlaceholderText(/services\/collector/),
                    "https://siem.internal:8088/services/collector");

    const create = screen.getByRole("button", { name: /create/i });
    expect(create).toBeDisabled();

    await user.click(screen.getByRole("checkbox", { name: /every tenant/i }));
    expect(create).toBeEnabled();
  });

  it("says on the button that the target is created switched off", async () => {
    const user = await openAuditStream();
    await user.click(screen.getByRole("button", { name: /add a target/i }));
    expect(screen.getByRole("button", { name: /switched off/i })).toBeInTheDocument();
  });

  it("sends the acknowledgement the API refuses a create without", async () => {
    createMock.mockClear();
    const user = await openAuditStream();
    await user.click(screen.getByRole("button", { name: /add a target/i }));
    await user.type(screen.getByPlaceholderText(/Corporate Splunk/), "SOC");
    await user.type(screen.getByPlaceholderText(/services\/collector/),
                    "https://siem.internal:8088/services/collector");
    await user.click(screen.getByRole("checkbox", { name: /every tenant/i }));
    await user.click(screen.getByRole("button", { name: /create/i }));

    await waitFor(() => expect(createMock).toHaveBeenCalled());
    expect(createMock.mock.calls[0][0]).toMatchObject({
      acknowledge_platform_scope: true,
      endpoint_url: "https://siem.internal:8088/services/collector",
    });
  });
});
