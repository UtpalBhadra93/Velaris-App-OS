/**
 * Portal error text.
 *
 * The portal shows customers the bare catalog code and never links to the
 * internal docs site. These tests pin that: the code has to survive into the
 * visible string (it is the whole point of coding an anonymous surface), and
 * the `docs` URL the backend also sends must not.
 */
import { describe, expect, it } from "vitest";

import { errorCode, portalErrorText } from "./portalError";

const coded = {
  detail: "Case not found or email does not match",
  error: {
    code: "VEL-PORTAL-ACCESS-404",
    docs: "/docs/errors#VEL-PORTAL-ACCESS-404",
  },
};

describe("portalErrorText", () => {
  it("shows the message with the code appended", () => {
    expect(portalErrorText(coded, "fallback")).toBe(
      "Case not found or email does not match (VEL-PORTAL-ACCESS-404)",
    );
  });

  it("never leaks the docs URL to a customer", () => {
    // Customers have no access to the docs site — a link would send them
    // somewhere they cannot read, which is worse than no link at all.
    expect(portalErrorText(coded, "fallback")).not.toContain("/docs/errors");
  });

  it("returns the message alone when the failure carries no code", () => {
    // 11 portal raise sites are deliberately uncoded pure validation.
    expect(portalErrorText({ detail: "rating must be 1–5" }, "fallback")).toBe(
      "rating must be 1–5",
    );
  });

  it("falls back when detail is missing, empty or not a string", () => {
    expect(portalErrorText({}, "Request failed")).toBe("Request failed");
    expect(portalErrorText({ detail: "   " }, "Request failed")).toBe("Request failed");
    // FastAPI's 422 puts an array in `detail` — rendering "[object Object]" at
    // a customer is the failure mode this guards.
    expect(portalErrorText({ detail: [{ msg: "x" }] }, "Request failed")).toBe("Request failed");
  });

  it("still shows the code when only the message is missing", () => {
    expect(portalErrorText({ error: { code: "VEL-PORTAL-RATE-LIMIT-429" } }, "Too many requests"))
      .toBe("Too many requests (VEL-PORTAL-RATE-LIMIT-429)");
  });

  it("survives a non-JSON or null body without throwing", () => {
    expect(portalErrorText(null, "Network error")).toBe("Network error");
    expect(portalErrorText("Bad Gateway", "Network error")).toBe("Network error");
  });
});

describe("errorCode", () => {
  it("extracts a code when present", () => {
    expect(errorCode(coded)).toBe("VEL-PORTAL-ACCESS-404");
  });

  it("returns null rather than an empty string for an uncoded failure", () => {
    expect(errorCode({ detail: "nope" })).toBeNull();
    expect(errorCode({ error: {} })).toBeNull();
    expect(errorCode({ error: { code: "" } })).toBeNull();
    expect(errorCode(null)).toBeNull();
  });
});
