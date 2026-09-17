/**
 * Admin console → Business Rules → Import DMN.
 *
 * The screen where somebody's Camunda decision tables become rules that decide
 * money and eligibility here. Two claims are worth holding down:
 *
 *  * **preview before create.** The API defaults `dry_run` to true; if the
 *    screen let the first click write rules, that default would be decoration.
 *  * **a refusal is shown verbatim.** DMN constructs this engine cannot express
 *    are refused rather than approximated, and the server's message names the
 *    construct. Rewording it here would throw away the only useful part.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

vi.mock("@shared/api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@shared/api/client");
  return {
    ...actual,
    getAdminOverview: vi.fn(async () => ({})),
    listRules: vi.fn(async () => ({ items: [] })),
    importDmn: vi.fn(async ({ dry_run }: { dry_run?: boolean }) => {
      if (dry_run === false) throw new Error("should have previewed first");
      return {
        dry_run: true, created: 0,
        tables: [{
          name: "Eligibility", rule_type: "decision_table", hit_policy: "UNIQUE",
          input_count: 2, output_count: 1, row_count: 4,
          notes: ["Row 3's second output cell was blank and is read as no value."],
        }],
      };
    }),
  };
});

import { importDmn } from "@shared/api/client";
import AdminConsole from "./AdminConsole";

const importMock = importDmn as unknown as ReturnType<typeof vi.fn>;

async function openImport() {
  const user = userEvent.setup();
  render(<AdminConsole />);
  await user.click(screen.getByRole("tab", { name: /business rules/i }));
  await user.click(await screen.findByRole("button", { name: /import dmn/i }));
  return user;
}

describe("importing a DMN file", () => {
  it("cannot import before it has previewed", async () => {
    const user = await openImport();
    await user.type(screen.getByPlaceholderText(/paste the DMN XML/i), "<definitions/>");

    expect(screen.getByRole("button", { name: /^import$/i })).toBeDisabled();
  });

  it("previews as a dry run, and creates nothing", async () => {
    importMock.mockClear();
    const user = await openImport();
    await user.type(screen.getByPlaceholderText(/paste the DMN XML/i), "<definitions/>");
    await user.click(screen.getByRole("button", { name: /preview/i }));

    await waitFor(() => expect(screen.getByText("Eligibility")).toBeInTheDocument());
    expect(importMock.mock.calls[0][0]).toMatchObject({ dry_run: true });
    // What the reader has to be able to see before confirming.
    expect(screen.getByText(/hit policy UNIQUE/)).toBeInTheDocument();
    expect(screen.getByText(/read as no value/)).toBeInTheDocument();
  });

  it("says imported rules land disabled, because a table that decides on arrival is nobody's decision", async () => {
    await openImport();
    expect(screen.getByText(/land disabled/i)).toBeInTheDocument();
  });

  it("shows the server's refusal word for word", async () => {
    importMock.mockRejectedValueOnce(
      new Error("Hit policy COLLECT returns several rows; this engine decides one."));
    const user = await openImport();
    await user.type(screen.getByPlaceholderText(/paste the DMN XML/i), "<definitions/>");
    await user.click(screen.getByRole("button", { name: /preview/i }));

    await waitFor(() =>
      expect(screen.getByText(/Hit policy COLLECT returns several rows/)).toBeInTheDocument());
  });
});
