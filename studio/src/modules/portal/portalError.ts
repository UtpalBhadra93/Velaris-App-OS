/**
 * Portal error text — message plus the bare catalog code.
 *
 * The backend attaches a `VEL-PORTAL-*` code and a `docs` deep link to most
 * portal failures. The portal deliberately uses **only the code**: these pages
 * answer anonymous customers, and the docs site is internal, so rendering the
 * link would send them somewhere they cannot read. The code is worth showing
 * anyway — it is what makes the support call that follows diagnosable.
 *
 * Studio is where the same code is clickable (`ErrorCodeLink`); the split is
 * intentional, not an oversight.
 */

/** The `error` object the backend attaches to a coded failure. */
type CodedError = { code?: unknown; docs?: unknown };

/** A parsed FastAPI error body: `{ detail, error: { code, docs } }`. */
export type ErrorBody = { detail?: unknown; error?: CodedError };

/** The code in an error body, or null when the failure carries none. */
export function errorCode(body: unknown): string | null {
  const err = (body as ErrorBody | null)?.error;
  return typeof err?.code === "string" && err.code ? err.code : null;
}

/**
 * User-facing text for a failed portal response.
 *
 * `fallback` is used when the body carries no usable `detail` — a non-JSON
 * response, or an error shape we do not recognise.
 */
export function portalErrorText(body: unknown, fallback: string): string {
  const detail = (body as ErrorBody | null)?.detail;
  const message = typeof detail === "string" && detail.trim() ? detail : fallback;
  const code = errorCode(body);
  return code ? `${message} (${code})` : message;
}
