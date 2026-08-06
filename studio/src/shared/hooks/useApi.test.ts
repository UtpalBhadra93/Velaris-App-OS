/**
 * useApi — the fetch hook behind 30-plus Studio modules.
 *
 * Its cancellation guard is the load-bearing part. Without it, a request that
 * resolves after the component unmounts calls setState on a dead component,
 * and a slow first request can overwrite the result of a faster second one —
 * so a filter change briefly shows the previous filter's data as if it were
 * current. Neither failure throws; both just show the wrong thing.
 */
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useApi } from "./index";

describe("useApi", () => {
  it("starts loading, then delivers data", async () => {
    const { result } = renderHook(() => useApi(async () => ({ ok: 1 })));

    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBeNull();

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual({ ok: 1 });
    expect(result.current.error).toBeNull();
  });

  it("surfaces the error message and stops loading", async () => {
    const { result } = renderHook(() =>
      useApi(async () => { throw new Error("that basis cannot be withdrawn"); }),
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe("that basis cannot be withdrawn");
    expect(result.current.data).toBeNull();
  });

  it("falls back to a message when the thrown value has none", async () => {
    const { result } = renderHook(() =>
      useApi(async () => { throw new Error(""); }),
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    // An empty banner is indistinguishable from no error at all.
    expect(result.current.error).toBe("Unknown error");
  });

  it("clears a previous error on refetch", async () => {
    let shouldFail = true;
    const { result } = renderHook(() =>
      useApi(async () => {
        if (shouldFail) throw new Error("boom");
        return { ok: true };
      }),
    );

    await waitFor(() => expect(result.current.error).toBe("boom"));

    shouldFail = false;
    act(() => result.current.refetch());

    await waitFor(() => expect(result.current.data).toEqual({ ok: true }));
    // A stale error beside fresh data tells the user the page failed when it
    // did not — the retention preview would show a refusal that no longer applies.
    expect(result.current.error).toBeNull();
  });

  it("does not set state after unmount", async () => {
    let resolve!: (v: unknown) => void;
    const pending = new Promise(r => { resolve = r; });
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});

    const { unmount } = renderHook(() => useApi(() => pending as Promise<unknown>));
    unmount();
    await act(async () => { resolve({ late: true }); await pending; });

    // React logs a warning for setState on an unmounted component; the
    // cancellation guard is what keeps it quiet.
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  });

  it("keeps the thrown object alongside the message", async () => {
    // `error` is flattened to err.message for display, which discarded the
    // stable error code beside it — so all 30-odd modules using this hook
    // lost it at the same line. <ErrorCodeLink> reads `errorObj`.
    const thrown = Object.assign(new Error("storage down"), {
      code: "VEL-DOC-STORAGE-503",
    });
    const { result } = renderHook(() => useApi(async () => { throw thrown; }));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe("storage down");
    expect((result.current.errorObj as { code?: string })?.code)
      .toBe("VEL-DOC-STORAGE-503");
  });

  it("clears the error object on refetch too", async () => {
    let fail = true;
    const { result } = renderHook(() =>
      useApi(async () => {
        if (fail) throw new Error("boom");
        return { ok: true };
      }),
    );

    await waitFor(() => expect(result.current.errorObj).toBeTruthy());
    fail = false;
    act(() => result.current.refetch());

    // A stale object would keep rendering a code for an error that no longer
    // applies — worse than showing none, because it looks current.
    await waitFor(() => expect(result.current.data).toBeTruthy());
    expect(result.current.errorObj).toBeNull();
  });

  it("refetches when a dependency changes", async () => {
    const fetcher = vi.fn(async () => ({ n: 1 }));
    const { rerender } = renderHook(
      ({ dep }) => useApi(fetcher, [dep]),
      { initialProps: { dep: "a" } },
    );

    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    rerender({ dep: "b" });
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  });

  it("does not refetch when dependencies are unchanged", async () => {
    const fetcher = vi.fn(async () => ({ n: 1 }));
    const { rerender } = renderHook(
      ({ dep }) => useApi(fetcher, [dep]),
      { initialProps: { dep: "a" } },
    );

    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    rerender({ dep: "a" });
    rerender({ dep: "a" });
    // Every module mounts this hook; a re-render loop here becomes a request
    // storm across the whole app.
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
