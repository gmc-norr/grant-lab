import createClient from "openapi-fetch";

import type { paths } from "./schema.gen";

// Same-origin: the httpOnly session cookie is sent automatically.
export const api = createClient<paths>({ baseUrl: "", credentials: "same-origin" });

export class ApiError extends Error {
  code: string;
  userMessage: string;
  /** The HTTP status that produced this error, when a response was in hand.
   *
   *  ADDED 2026-10-28 (review M-fe-2 / A8). Without it the only machine-readable
   *  fact an error carried was the envelope CODE, which domain handlers are free
   *  to choose — so no retry policy could tell "the server refused this request"
   *  from "the server fell over", and a `retry` predicate written against this
   *  class would have matched nothing while looking correct. Optional because two
   *  callers build an ApiError from a raw fetch and a handful of tests build one
   *  by hand; `is4xx` falls back to the code for those. */
  status?: number;
  constructor(code: string, message: string, status?: number) {
    // Put the machine code in the message too, so an ErrorBoundary / log shows
    // it and `toThrowError(/code/)` matches; `.code` stays the structured field.
    super(`${code}: ${message}`);
    this.name = "ApiError";
    this.code = code;
    this.userMessage = message;
    this.status = status;
  }
}

// The envelope codes the backend emits for a 4xx, from
// src/grant_lab/web/api/errors.py STATUS_CODES — plus `account_inactive`, the
// one domain code unwrap() mints itself (on a 403). Used ONLY when no status
// reached the error; a status, when present, is the authority.
const CLIENT_ERROR_CODES: ReadonlySet<string> = new Set([
  "bad_request",
  "unauthorized",
  "forbidden",
  "not_found",
  "method_not_allowed",
  "conflict",
  "payload_too_large",
  "validation_error",
  "too_many_requests",
  "account_inactive",
]);

/** Whether an error is the server REFUSING the request, rather than failing it.
 *
 *  Retrying a 4xx cannot change the answer — `?sort=foo` is still `?sort=foo` on
 *  the fourth attempt — so the app's query default stops at the first one. A
 *  transport failure (`TypeError: Failed to fetch`) is deliberately NOT a 4xx:
 *  that is the case retries exist for. */
export function is4xx(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  if (error.status !== undefined) return error.status >= 400 && error.status < 500;
  return CLIENT_ERROR_CODES.has(error.code);
}

export function apiErrorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.userMessage : fallback;
}

// Full-page nav to /login (now a React pre-auth page), preserving where we were
// so the user lands back after signing in (mirrors the backend's 401 next=).
export function redirectToLogin(): void {
  const next = window.location.pathname + window.location.search;
  window.location.assign(`/login?next=${encodeURIComponent(next)}`);
}

/** Full-page nav to the holding page for a pending/disabled account. The server
 *  supplies the target on the 403 envelope; the SSE probe (api/events.ts) has no
 *  envelope to read, so it falls back to the same default the backend uses. */
export function redirectToNoAccess(target?: string): void {
  window.location.assign(target ?? "/auth/no-access");
}

// Collapse openapi-fetch's { data, error } envelope: return data, or throw the
// Plan-1 error envelope ({ error: { code, message } }) as a typed ApiError.
//
// 401 is the SINGLE chokepoint for session expiry: every query/mutation goes
// through unwrap, so the dashboard (which never mounts useSession) is covered
// too. A logged-out browser reaches the SPA shell at `/` (served with no auth),
// fires /api/calls → 401 JSON envelope; without this it would dead-end on a
// generic error with no way to re-authenticate.
export function unwrap<T>(res: { data?: T; error?: unknown; response?: Response }): T {
  if (res.response?.status === 401) {
    redirectToLogin();
    throw new ApiError("unauthorized", "session expired", 401);
  }
  if (res.error) {
    const e = res.error as {
      error?: { code?: string; message?: string; detail?: { redirect?: string } };
    };
    // A pending/disabled account mid-session returns 403 account_inactive carrying
    // the holding-page target ({detail:{redirect}}). Full-page-nav there (mirrors
    // the 401 -> /login chokepoint); the cookie is already cleared server-side. Only
    // this code redirects — a plain 403 (forbidden) flows through as an ApiError.
    if (res.response?.status === 403 && e.error?.code === "account_inactive") {
      redirectToNoAccess(e.error.detail?.redirect);
      throw new ApiError("account_inactive", "account not active", res.response?.status);
    }
    throw new ApiError(
      e.error?.code ?? "error",
      e.error?.message ?? "request failed",
      res.response?.status,
    );
  }
  // Catch non-JSON error responses (502/504 gateway, HTML 500, etc.) that
  // openapi-fetch leaves with res.data=undefined and res.error=undefined because
  // the body isn't a JSON error envelope.  Without this guard a mutation would
  // resolve successfully with undefined data and silently swallow the failure.
  if (res.response && !res.response.ok) {
    throw new ApiError("network_error", `HTTP ${res.response.status}`, res.response.status);
  }
  return res.data as T;
}
