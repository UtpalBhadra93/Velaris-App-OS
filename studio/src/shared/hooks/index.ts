import { useCallback, useEffect, useState } from "react";

/* ═══════════════════════════════════════════════════════════════════
   useApi — generic async data fetcher with loading/error states
   ═══════════════════════════════════════════════════════════════════ */

interface UseApiState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  /**
   * The thrown value itself, kept alongside its message.
   *
   * `error` is flattened to `err.message` for display, which discarded the
   * stable error code the API returns beside it — so every one of the 30-odd
   * modules using this hook lost the code at the same line. Additive: existing
   * consumers are untouched, and `<ErrorCodeLink error={errorObj} />` renders
   * nothing when there is no code.
   */
  errorObj: unknown;
  refetch: () => void;
}

export function useApi<T>(fetcher: () => Promise<T>, deps: unknown[] = []): UseApiState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [errorObj, setErrorObj] = useState<unknown>(null);
  const [tick, setTick] = useState(0);

  const refetch = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setErrorObj(null);

    fetcher()
      .then((result) => {
        if (!cancelled) {
          setData(result);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err.message || "Unknown error");
          setErrorObj(err);
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [tick, ...deps]);

  return { data, loading, error, errorObj, refetch };
}

/* ═══════════════════════════════════════════════════════════════════
   useInterval — poll at a fixed interval
   ═══════════════════════════════════════════════════════════════════ */

export function useInterval(callback: () => void, delayMs: number | null) {
  useEffect(() => {
    if (delayMs === null) return;
    const id = setInterval(callback, delayMs);
    return () => clearInterval(id);
  }, [callback, delayMs]);
}
export { useCommit } from "./useCommit";
export type { ChangeEntry, CommitSnapshot } from "./useCommit";
export { useBranchMode } from "./useBranchMode";
export type { BranchModeState } from "./useBranchMode";
export { useCurrentUserGroups } from "./useCurrentUserGroups";
