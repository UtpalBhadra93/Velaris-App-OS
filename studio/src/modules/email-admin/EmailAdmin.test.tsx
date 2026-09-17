/**
 * Email Admin — one tenant per mailbox.
 *
 * Pinned: every mailbox row shows its tenant; only a superadmin gets a tenant
 * picker (the server ignores a tenant sent by anyone else, so offering the field
 * to a tenant admin would be a control that silently does nothing); and a tenant
 * admin's save does not send a tenant at all.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let roles: string[] = ["admin"];

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ user: { roles, tenant_id: roles.includes("superadmin") ? null : "alpha" } }),
}));
vi.mock("@/auth/tokenManager", () => ({ getAccessToken: () => "t" }));
vi.mock("@shared/api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@shared/api/client");
  return {
    ...actual,
    listTenants: vi.fn(async () => [
      { id: "1", slug: "alpha", name: "Alpha" }, { id: "2", slug: "beta", name: "Beta" },
    ]),
  };
});

import EmailAdmin from "./EmailAdmin";

const account = {
  id: "acc1", name: "Alpha box", address: "a@alpha.test", smtp_host: "smtp.test", smtp_port: 587,
  smtp_username: "a", smtp_use_tls: true, imap_host: null, imap_port: 993, imap_username: null,
  imap_use_ssl: true, imap_folder: "INBOX", poll_interval_seconds: 15, is_active: true,
  is_default_outbound: true, tenant_id: "alpha",
};

const sent: { url: string; method: string; body?: any }[] = [];

beforeEach(() => {
  sent.length = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, opts: RequestInit = {}) => {
    sent.push({ url, method: opts.method || "GET", body: opts.body ? JSON.parse(String(opts.body)) : undefined });
    const payload = String(url).includes("/email/accounts") && (opts.method || "GET") === "GET"
      ? [account] : {};
    return new Response(JSON.stringify(payload), { status: 200 });
  }));
});
afterEach(() => { vi.unstubAllGlobals(); });

function renderIt() {
  return render(<MemoryRouter><EmailAdmin /></MemoryRouter>);
}

describe("EmailAdmin tenant", () => {
  it("shows each mailbox's tenant", async () => {
    roles = ["admin"];
    renderIt();
    const row = (await screen.findByText("Alpha box")).closest("tr")!;
    expect(within(row).getByText("alpha")).toBeTruthy();
  });

  it("gives a tenant admin no picker and sends no tenant", async () => {
    roles = ["admin"];
    renderIt();
    await screen.findByText("Alpha box");
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.queryByLabelText("Tenant")).toBeNull();
    expect(screen.getByText(/Only a superadmin can move a mailbox/)).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Commit" }));
    await waitFor(() => expect(sent.some(s => s.method === "PATCH")).toBe(true));
    const patch = sent.find(s => s.method === "PATCH")!;
    expect("tenant_id" in patch.body).toBe(false);
  });

  it("lets a superadmin choose the tenant", async () => {
    roles = ["superadmin"];
    renderIt();
    await screen.findByText("Alpha box");
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    const picker = await screen.findByLabelText("Tenant");
    await screen.findByRole("option", { name: "Beta (beta)" });
    await userEvent.selectOptions(picker, "beta");
    await userEvent.click(screen.getByRole("button", { name: "Commit" }));
    await waitFor(() => expect(sent.some(s => s.method === "PATCH")).toBe(true));
    expect(sent.find(s => s.method === "PATCH")!.body.tenant_id).toBe("beta");
  });
});
