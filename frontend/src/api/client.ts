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

// Collapse openapi-fetch's { data, error } envelope: return data, or throw the
// Plan-1 error envelope ({ error: { code, message } }) as a typed ApiError.
export function unwrap<T>(res: { data?: T; error?: unknown }): T {
  if (res.error) {
    const e = res.error as { error?: { code?: string; message?: string } };
    throw new ApiError(e.error?.code ?? "error", e.error?.message ?? "request failed");
  }
  return res.data as T;
}
