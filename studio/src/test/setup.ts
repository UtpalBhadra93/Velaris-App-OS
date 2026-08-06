/**
 * Global test setup.
 *
 * Two things every test in this suite depends on, done once here rather than
 * repeated (and eventually forgotten) in each file.
 */
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

beforeEach(() => {
  // Every module in this app reads its bearer token from localStorage. jsdom
  // gives each file a fresh store but NOT a fresh one per test, so a token set
  // in one test leaks into the next and hides a missing-auth bug.
  localStorage.clear();

  // No test may reach the network. An unmocked fetch in jsdom fails with an
  // obscure connection error attributed to the component rather than to the
  // missing mock, so it is stubbed to fail loudly instead.
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.reject(
        new Error("fetch was not mocked in this test — stub it explicitly"),
      ),
    ),
  );
});
