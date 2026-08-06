// Renders a failed request's error code as a link into the error catalog.
//
// The backend has returned `{"detail": …, "error": {"code", "docs"}}` since the
// catalog was wired, but nothing on this side read it — so the code reached
// the browser and stopped there, one step short of the person who could use
// it. This is that step.
//
// Deliberately silent when there is no code: most subsystems still raise a
// plain HTTPException, and a component that rendered "no code available" on
// every one of those errors would be noise on the exact screen where the user
// is already having a bad time.
//
// Copyright (c) 2024-2026 Velaris Contributors
// SPDX-License-Identifier: BSL-1.1
import React, { useState } from "react";

import ErrorDocModal from "./ErrorDocModal";
import { hasEntry } from "./catalog";

interface Props {
  /** The error thrown by the API client — an ApiError, or anything else. */
  error: unknown;
  /** Nav key of the page, so the modal can fall back to its component list. */
  componentKey?: string;
}

/** Narrow without importing ApiError, so this stays usable by modules that
 *  hand-roll their own fetch (several still do). */
function codeOf(error: unknown): string | undefined {
  const c = (error as { code?: unknown } | null)?.code;
  return typeof c === "string" && c.startsWith("VEL-") ? c : undefined;
}

export default function ErrorCodeLink({ error, componentKey }: Props) {
  const [open, setOpen] = useState(false);
  const code = codeOf(error);
  if (!code) return null;

  // A code with no catalog entry is worse than none — it promises an
  // explanation that does not exist. Show it as plain text instead.
  const documented = hasEntry(code);

  return (
    <>
      <span style={{ fontSize: 11, marginLeft: 8, fontFamily: "var(--font-mono, ui-monospace, monospace)" }}>
        {documented ? (
          <button
            type="button"
            onClick={() => setOpen(true)}
            title="What does this mean?"
            style={{
              background: "none", border: "none", padding: 0, cursor: "pointer",
              color: "var(--accent, #4a6cf7)", textDecoration: "underline",
              font: "inherit",
            }}
          >
            {code}
          </button>
        ) : (
          <span style={{ color: "var(--text-muted, #888)" }}>{code}</span>
        )}
      </span>
      <ErrorDocModal
        open={open}
        onClose={() => setOpen(false)}
        componentKey={componentKey}
        initialCode={code}
      />
    </>
  );
}
