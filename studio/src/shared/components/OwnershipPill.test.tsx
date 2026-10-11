/**
 * Oct 8k — what a tenant is shown of defaults and its own copies.
 *
 * Pinned: a default says so; a tenant's copy says so, and adds "Update
 * available" only when its default moved; a tenant's own row, and a server
 * too old to send the fields, show nothing.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { OwnershipPill } from "@shared/components";

describe("OwnershipPill", () => {
  it("marks a platform default", () => {
    render(<OwnershipPill row={{ tenant_id: null }} />);
    expect(screen.getByText("Default")).toBeTruthy();
  });

  it("marks a copy, and offers an update only when there is one", () => {
    const { rerender } = render(
      <OwnershipPill row={{ tenant_id: "t1", forked_from_id: "d1", update_available: false }} />);
    expect(screen.getByText("Your copy")).toBeTruthy();
    expect(screen.queryByText("Update available")).toBeNull();
    rerender(<OwnershipPill row={{ tenant_id: "t1", forked_from_id: "d1", update_available: true }} />);
    expect(screen.getByText("Update available")).toBeTruthy();
  });

  it("shows nothing for a tenant's own row or an older server", () => {
    const { container, rerender } = render(<OwnershipPill row={{ tenant_id: "t1", forked_from_id: null }} />);
    expect(container.textContent).toBe("");
    rerender(<OwnershipPill row={{}} />);
    expect(container.textContent).toBe("");
  });
});
