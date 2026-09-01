import { QueryClient } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { queryWrapper as wrapper } from "../test-utils";
import * as clientMod from "./client";
import {
  useAdminPendingUsers,
  useAdminUsers,
  useApproveUser,
  useChangeRole,
  useDenyUser,
  useDisableUser,
  useEnableUser,
  useResetPassword,
} from "./admin";

afterEach(() => {
  vi.restoreAllMocks();
});

it("useAdminUsers GETs the page", async () => {
  const get = vi
    .spyOn(clientMod.api, "GET")
    .mockResolvedValue({ data: { users: [], page: 1, per_page: 50, total: 0 }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useAdminUsers(2), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(get).toHaveBeenCalledWith("/api/admin/users", { params: { query: { page: 2 } } });
});

it("useAdminPendingUsers GETs the pending page", async () => {
  const get = vi
    .spyOn(clientMod.api, "GET")
    .mockResolvedValue({ data: { users: [], page: 1 }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useAdminPendingUsers(1), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(get).toHaveBeenCalledWith("/api/admin/users/pending", { params: { query: { page: 1 } } });
});

it("useApproveUser POSTs role+password and invalidates both lists", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 7 }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useApproveUser(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ id: 7, role: "admin", initial_password: "pw-strong-1234" });
  expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/approve", {
    params: { path: { user_id: 7 } },
    body: { role: "admin", initial_password: "pw-strong-1234" },
  });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-pending"] });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-users"] });
});

it("useApproveUser defaults role to member", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 1 }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useApproveUser(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ id: 1 });
  expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/approve", {
    params: { path: { user_id: 1 } },
    body: { role: "member", initial_password: undefined },
  });
});

it("useDenyUser POSTs and invalidates both lists", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 3 }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useDenyUser(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ id: 3 });
  expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/deny", { params: { path: { user_id: 3 } } });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-pending"] });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-users"] });
});

it("useDisableUser invalidates the users list only", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 4 }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useDisableUser(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ id: 4 });
  expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/disable", { params: { path: { user_id: 4 } } });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-users"] });
  expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ["admin-pending"] });
});

it("useChangeRole POSTs the role body", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 5 }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useChangeRole(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ id: 5, role: "member" });
  expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/role", {
    params: { path: { user_id: 5 } },
    body: { role: "member" },
  });
});

it("useResetPassword POSTs the new password", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 6 }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useResetPassword(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ id: 6, new_password: "fresh-strong-pw" });
  expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/reset-password", {
    params: { path: { user_id: 6 } },
    body: { new_password: "fresh-strong-pw" },
  });
});

it("useEnableUser POSTs enable", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 8 }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useEnableUser(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ id: 8 });
  expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/enable", { params: { path: { user_id: 8 } } });
});
