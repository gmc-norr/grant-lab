import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { queryWrapper as wrap } from "../test-utils";
import * as clientMod from "./client";
import { useFetchReference, useUpdateKind } from "./references";

afterEach(() => { vi.restoreAllMocks(); });

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
