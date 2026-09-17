/**
 * One tab bar for every module.
 *
 * **Why this exists.** Studio had four tab styles at once, all hand-rolled: an
 * underlined bar (HxSync, Dev Connectors, HxCheckout), an UPPERCASE monospace
 * version of the same (Marketplace, Calendar, HxDeploy), a pill on a card
 * (Access Directory) and a segmented button group (Push, Email Admin). Nobody
 * chose four — each module copied whatever its neighbour did on the day it was
 * written, and the copy drifted.
 *
 * The underlined bar wins because it is the one that scales: it survives eight
 * tabs without wrapping, and the active state reads at a glance without a filled
 * background competing with the page's own cards.
 *
 * Sentence case, not uppercase. `TEST SEND` and `ACCELERATOR PACKS` are wider
 * and slower to read than `Test send` and `Accelerator packs`, and the monospace
 * face those styles used is for data, not navigation.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React from "react";

export interface TabItem<K extends string = string> {
  key: K;
  label: string;
  /** Rendered after the label — a count, usually. Kept out of `label` so it
   *  cannot end up inside the accessible name of the button. */
  badge?: React.ReactNode;
  disabled?: boolean;
}

const S: Record<string, React.CSSProperties> = {
  // Geometry copied from the Help Centre, which is the reference the platform
  // settled on. The 14px top padding is the visible gap between a module's
  // header and its tab labels — the 8px some modules used made the bar look
  // stuck to the title above it.
  bar: {
    display: "flex",
    gap: 2,
    padding: "0 32px",
    borderBottom: "1px solid var(--border-subtle)",
    flexShrink: 0,
    overflowX: "auto",
  },
  tab: {
    padding: "14px 20px",
    border: "none",
    background: "none",
    fontSize: 13,
    fontWeight: 500,
    cursor: "pointer",
    color: "var(--text-muted)",
    // Transparent rather than absent: a border that appears only when active
    // moves every label by two pixels as you click along the bar.
    borderBottom: "2px solid transparent",
    marginBottom: -1,
    whiteSpace: "nowrap",
    transition: "color .15s",
  },
  active: {
    borderBottom: "2px solid var(--accent)",
    color: "var(--accent)",
  },
  disabled: { opacity: 0.45, cursor: "not-allowed" },
  badge: { marginLeft: 6, fontSize: 11, color: "var(--text-muted)" },
};

export function Tabs<K extends string>({
  tabs, value, onChange, style, ariaLabel, right,
}: {
  tabs: readonly TabItem<K>[];
  value: K;
  onChange: (key: K) => void;
  style?: React.CSSProperties;
  ariaLabel?: string;
  /** Pushed to the far right of the bar — a status pill, a refresh button.
   *  A slot rather than a second row, because a module that needs one otherwise
   *  builds its own bar around the tabs and the geometry drifts again. */
  right?: React.ReactNode;
}) {
  return (
    <div role="tablist" aria-label={ariaLabel} style={{ ...S.bar, ...style }}>
      {tabs.map(t => {
        const on = t.key === value;
        return (
          <button
            key={t.key}
            role="tab"
            type="button"
            aria-selected={on}
            disabled={t.disabled}
            onClick={() => !t.disabled && onChange(t.key)}
            style={{
              ...S.tab,
              ...(on ? S.active : {}),
              ...(t.disabled ? S.disabled : {}),
            }}
          >
            {t.label}
            {t.badge != null && <span style={S.badge}>{t.badge}</span>}
          </button>
        );
      })}
      {right != null && (
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center",
                      gap: 6, alignSelf: "center" }}>
          {right}
        </div>
      )}
    </div>
  );
}

export default Tabs;
