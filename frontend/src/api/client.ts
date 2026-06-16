import createClient from "openapi-fetch";

import type { paths } from "./schema.gen";

// Same-origin: the httpOnly session cookie is sent automatically.
export const api = createClient<paths>({ baseUrl: "", credentials: "same-origin" });

export class ApiError extends Error {
  code: string;
  constructor(code: string, message: string) {
    // Put the machine code in the message too, so an ErrorBoundary / log shows
    // it and `toThrowError(/code/)` matches; `.code` stays the structured field.
    super(`${code}: ${message}`);
    this.name = "ApiError";
    this.code = code;
  }
}

// Full-page nav to the server-rendered /login, preserving where we were so the
// user lands back after signing in (mirrors the backend's 401-redirect next=).
export function redirectToLogin(): void {
  const next = window.location.pathname + window.location.search;
  window.location.assign(`/login?next=${encodeURIComponent(next)}`);
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
    throw new ApiError("unauthorized", "session expired");
  }
  if (res.error) {
    const e = res.error as { error?: { code?: string; message?: string } };
    throw new ApiError(e.error?.code ?? "error", e.error?.message ?? "request failed");
  }
  return res.data as T;
}
