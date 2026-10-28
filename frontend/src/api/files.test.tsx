import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { queryWrapper } from "../test-utils";
import * as clientMod from "./client";
import { ApiError, is4xx } from "./client";
import { useDeleteFile, useFiles, useUploadFile } from "./files";

afterEach(() => {
  vi.restoreAllMocks();
  // restoreAllMocks does NOT undo stubGlobal — the fetch stub below would leak
  // into every later test in this file.
  vi.unstubAllGlobals();
});

it("useFiles fetches the list", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: [{ id: "a1", filename: "f.png", mime_type: "image/png", uploaded_at: "2026-06-24T00:00:00Z" }],
    error: undefined,
  } as never);
  const { result } = renderHook(() => useFiles("ws-1"), { wrapper: queryWrapper() });
  await waitFor(() => expect(result.current.data?.[0]?.filename).toBe("f.png"));
});

it("useDeleteFile DELETEs", async () => {
  const spy = vi
    .spyOn(clientMod.api, "DELETE")
    .mockResolvedValue({ data: undefined, error: undefined } as never);
  const { result } = renderHook(() => useDeleteFile("ws-1"), { wrapper: queryWrapper() });
  result.current.mutate("a1");
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(spy).toHaveBeenCalled();
});

it("an upload refused with a NON-JSON 4xx body is still a 4xx to is4xx", async () => {
  // THE CASE THAT WAS WRONG (review 2026-10-28, A8 follow-up). `postFile`
  // builds its ApiError off a RAW fetch, and until this change it passed no
  // status — so the only machine-readable fact the error carried was the
  // envelope code. When the body is not JSON (an nginx 413 page, a proxy 403)
  // `res.json()` rejects, the code falls back to the default `"error"`, and
  // `"error"` is NOT in client.ts's CLIENT_ERROR_CODES. `is4xx` therefore
  // answered FALSE for a plain refusal, breaking its own documented contract
  // ("prefers the status ... falls back to the code") at one of the only two
  // sites that hold a Response and could always have supplied one.
  //
  // Latent, not live: this path is reached only from useMutation, and mutations
  // keep retry: 0. The contract is the defect.
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response("<html><body><h1>413 Request Entity Too Large</h1></body></html>", {
          status: 413,
          statusText: "Request Entity Too Large",
        }),
    ),
  );
  const { result } = renderHook(() => useUploadFile("ws-1"), { wrapper: queryWrapper() });
  result.current.mutate(new File(["x"], "big.pdf", { type: "application/pdf" }));
  await waitFor(() => expect(result.current.isError).toBe(true));
  const error = result.current.error as ApiError;
  // The body was not JSON, so the CODE is the useless default...
  expect(error.code).toBe("error");
  // ...which is why the STATUS has to be the thing that classifies it.
  expect(error.status).toBe(413);
  expect(is4xx(error)).toBe(true);
});
