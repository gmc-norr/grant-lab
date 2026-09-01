import { render, renderHook, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { queryWrapper as wrap } from "../test-utils";
import * as clientMod from "./client";
import { useCockpitMeta, useCreateWorkspace, useWorkspaces } from "./workspace";

function Probe({ id }: { id: string }) {
  const q = useCockpitMeta(id);
  return <span data-testid="title">{q.data?.workspace_title ?? "—"}</span>;
}
function renderWith(id: string) {
  return render(<Probe id={id} />, { wrapper: wrap() });
}

it("useCockpitMeta does NOT fetch when id is empty (off-cockpit gating)", () => {
  const get = vi.spyOn(clientMod.api, "GET");
  renderWith("");
  expect(get).not.toHaveBeenCalled();
});

it("useCockpitMeta fetches and returns the header when id is present", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { workspace_title: "My WS", workspace_deadline: null, current_phase: "active", role: "owner", spec_locked: false, member_count: 2 },
    error: undefined,
  } as never);
  const { getByTestId } = renderWith("ws-1");
  await waitFor(() => expect(getByTestId("title").textContent).toBe("My WS"));
});

it("useWorkspaces fetches the list with an empty query when status is null", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: [
      {
        id: "w1",
        title: "A",
        status: "drafting",
        current_phase: "active",
        deadline: null,
        updated_at: "2026-06-24T00:00:00Z",
        linked_call_title: "C",
        is_owner: true,
      },
    ],
    error: undefined,
  } as never);
  const { result } = renderHook(() => useWorkspaces(null), { wrapper: wrap() });
  await waitFor(() => expect(result.current.data?.[0]?.title).toBe("A"));
  expect(get).toHaveBeenCalledWith("/api/workspaces", { params: { query: {} } });
});

it("useWorkspaces passes the status filter as a query param", async () => {
  const get = vi
    .spyOn(clientMod.api, "GET")
    .mockResolvedValue({ data: [], error: undefined } as never);
  const { result } = renderHook(() => useWorkspaces("submitted"), { wrapper: wrap() });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(get).toHaveBeenCalledWith("/api/workspaces", {
    params: { query: { status: "submitted" } },
  });
});

it("useCreateWorkspace posts the body and returns the new id", async () => {
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { id: "new-1" }, error: undefined } as never);
  const { result } = renderHook(() => useCreateWorkspace(), { wrapper: wrap() });
  const out = await result.current.mutateAsync({ call_id: 3, title: "T", profile_id: 7 });
  expect(out.id).toBe("new-1");
  expect(post).toHaveBeenCalledWith("/api/workspaces", {
    body: { call_id: 3, title: "T", profile_id: 7 },
  });
});
