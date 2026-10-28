import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { queryWrapper as wrap } from "../test-utils";
import * as clientMod from "./client";
import { ApiError, is4xx } from "./client";
import { useFetchReference, useUpdateKind, useUploadReference } from "./references";

afterEach(() => {
  vi.restoreAllMocks();
  // restoreAllMocks does NOT undo stubGlobal — the fetch stub below would leak
  // into every later test in this file.
  vi.unstubAllGlobals();
});

it("useUpdateKind PATCHes and returns the new kind", async () => {
  const spy = vi
    .spyOn(clientMod.api, "PATCH")
    .mockResolvedValue({ data: { kind: "data" }, error: undefined } as never);
  const { result } = renderHook(() => useUpdateKind("ws-1"), { wrapper: wrap() });
  result.current.mutate({ referenceId: "r1", kind: "data" });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data?.kind).toBe("data");
  expect(spy).toHaveBeenCalled();
});

it("useFetchReference POSTs the identifier", async () => {
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { reference_id: "r2", text_status: "done", text_error: null },
    error: undefined,
  } as never);
  const { result } = renderHook(() => useFetchReference("ws-1"), { wrapper: wrap() });
  result.current.mutate({ input: "10.1/x", kind: "paper" });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data?.reference_id).toBe("r2");
});

it("a reference upload refused with a NON-JSON 4xx body is still a 4xx to is4xx", async () => {
  // The SECOND of the only two ApiError sites built off a raw fetch (the other
  // is api/files.ts `postFile`, pinned by the twin of this test). Same defect,
  // same reason it mattered: with no status the classification fell back to the
  // envelope code, and a non-JSON refusal has no envelope — so the code was the
  // default `"error"`, which client.ts's CLIENT_ERROR_CODES does not contain,
  // and `is4xx` called a 403 from a proxy "not a 4xx".
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response("<html><body><h1>403 Forbidden</h1></body></html>", {
          status: 403,
          statusText: "Forbidden",
        }),
    ),
  );
  const { result } = renderHook(() => useUploadReference("ws-1"), { wrapper: wrap() });
  result.current.mutate({
    file: new File(["x"], "paper.pdf", { type: "application/pdf" }),
    kind: "paper",
    autoClassify: false,
  });
  await waitFor(() => expect(result.current.isError).toBe(true));
  const error = result.current.error as ApiError;
  expect(error.code).toBe("error");
  expect(error.status).toBe(403);
  expect(is4xx(error)).toBe(true);
});
