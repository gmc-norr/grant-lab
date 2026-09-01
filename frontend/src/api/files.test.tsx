import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { queryWrapper } from "../test-utils";
import * as clientMod from "./client";
import { useDeleteFile, useFiles } from "./files";

afterEach(() => { vi.restoreAllMocks(); });

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
