import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { queryWrapper as wrap } from "../test-utils";
import * as clientMod from "./client";
import { useAddMember, useSettings } from "./settings";

afterEach(() => { vi.restoreAllMocks(); });

it("useSettings fetches the settings", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { title: "T", status: "drafting", deadline: null, citation_style: "vancouver" },
    error: undefined,
  } as never);
  const { result } = renderHook(() => useSettings("ws-1"), { wrapper: wrap() });
  await waitFor(() => expect(result.current.data?.title).toBe("T"));
});

it("useAddMember POSTs the email", async () => {
  const spy = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { user_id: 2, email: "a@ki.se", role: "member" }, error: undefined } as never);
  const { result } = renderHook(() => useAddMember("ws-1"), { wrapper: wrap() });
  result.current.mutate("a@ki.se");
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(spy).toHaveBeenCalled();
});
