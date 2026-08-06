/**
 * ErrorCodeLink — turns a failed request's code into a catalog link.
 *
 * Its two silent-failure modes are what the tests pin: rendering noise on
 * every uncoded error, and promising an explanation for a code nothing
 * documents. Both look fine in a screenshot.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import ErrorCodeLink from "./ErrorCodeLink";
import { allErrors } from "./catalog";

const KNOWN = allErrors()[0]!.code;

describe("when there is no code", () => {
  it("renders nothing for a plain Error", () => {
    // Most subsystems still raise bare HTTPException. Rendering "no code
    // available" on every one of those is noise on the exact screen where
    // the user is already having a bad time.
    const { container } = render(<ErrorCodeLink error={new Error("boom")} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing for null or a string", () => {
    const { container } = render(<ErrorCodeLink error={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("ignores a code that is not ours", () => {
    const { container } = render(
      <ErrorCodeLink error={{ code: "ECONNREFUSED" } as unknown} />,
    );
    // Node and browser errors carry `code` too; only VEL- codes are ours.
    expect(container).toBeEmptyDOMElement();
  });
});

describe("when the code is documented", () => {
  it("renders it as a button", () => {
    render(<ErrorCodeLink error={{ code: KNOWN }} />);
    expect(screen.getByRole("button", { name: KNOWN })).toBeInTheDocument();
  });

  it("opens the catalog on click", async () => {
    const user = userEvent.setup();
    render(<ErrorCodeLink error={{ code: KNOWN }} />);

    await user.click(screen.getByRole("button", { name: KNOWN }));
    expect(screen.getByRole("dialog", { name: /error documentation/i })).toBeInTheDocument();
  });
});

describe("when the code is not documented", () => {
  it("shows plain text, never a link", () => {
    render(<ErrorCodeLink error={{ code: "VEL-GHOST-MISSING-418" }} />);

    // Promising an explanation that does not exist is worse than showing the
    // bare code — the user follows the link and finds nothing.
    expect(screen.getByText("VEL-GHOST-MISSING-418")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
