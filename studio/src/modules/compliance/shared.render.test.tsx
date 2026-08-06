/**
 * Presentation helpers shared by the five Compliance tabs.
 *
 * `Kpi` and `Banner` encode status as colour. Colour is the whole message on
 * these screens — an amber "untested" backup rendered green reads as healthy,
 * and nothing in a type-check or a snapshot would catch it.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Banner, EmptyHint, Kpi } from "./shared";

describe("Kpi", () => {
  it("renders its label and value", () => {
    render(<Kpi label="Payments captured" value="USD 100.00" />);
    expect(screen.getByText("Payments captured")).toBeInTheDocument();
    expect(screen.getByText("USD 100.00")).toBeInTheDocument();
  });

  it("renders zero rather than treating it as absent", () => {
    // `0` is falsy; a naive `{value && …}` would render nothing, and a
    // reconciliation showing no difference at all reads as "not loaded".
    render(<Kpi label="Difference" value={0} />);
    expect(screen.getByText("0")).toBeInTheDocument();
  });

  it("colours warn and good differently from neutral", () => {
    render(<Kpi label="a" value="v-neutral" />);
    render(<Kpi label="b" value="v-warn" warn />);
    render(<Kpi label="c" value="v-good" good />);

    const colourOf = (text: string) =>
      (screen.getByText(text) as HTMLElement).style.color;

    expect(colourOf("v-warn")).not.toBe(colourOf("v-neutral"));
    expect(colourOf("v-good")).not.toBe(colourOf("v-neutral"));
    expect(colourOf("v-good")).not.toBe(colourOf("v-warn"));
  });
});

describe("Banner", () => {
  it("renders its children", () => {
    render(<Banner tone="bad">something went wrong</Banner>);
    expect(screen.getByText("something went wrong")).toBeInTheDocument();
  });

  it("gives every tone a distinct colour", () => {
    const tones = ["ok", "warn", "bad", "info"] as const;
    const colours = tones.map(tone => {
      const { container } = render(<Banner tone={tone}>x</Banner>);
      return (container.firstChild as HTMLElement).style.background;
    });
    // Two tones sharing a colour is the DR panel's failure mode: `untested`
    // becomes indistinguishable from `ok` at a glance.
    expect(new Set(colours).size).toBe(tones.length);
  });
});

describe("EmptyHint", () => {
  it("renders an explanation rather than nothing", () => {
    render(<EmptyHint>nothing has looked yet</EmptyHint>);
    // An empty table implies "all clear". These screens must say why they
    // are empty — no scan has run, no region is registered, no policy exists.
    expect(screen.getByText("nothing has looked yet")).toBeInTheDocument();
  });
});
