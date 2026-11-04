import { QueryClient } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { queryWrapper as wrapper } from "../test-utils";
import * as clientMod from "./client";
import {
  useAdminDashboard,
  useFetchSource,
  useReenableDigestUser,
  useRerank,
  useSendDigest,
} from "./adminDashboard";

afterEach(() => {
  vi.restoreAllMocks();
});

it("useAdminDashboard GETs the dashboard", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: {}, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useAdminDashboard(), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(get).toHaveBeenCalledWith("/api/admin/dashboard", {});
});

it("useRerank POSTs and invalidates the dashboard", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { enqueued: 2 }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useRerank(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync();
  expect(post).toHaveBeenCalledWith("/api/admin/rerank", {});
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-dashboard"] });
});

it("useSendDigest POSTs digest/send and invalidates the dashboard", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { triggered: true }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useSendDigest(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync();
  expect(post).toHaveBeenCalledWith("/api/admin/digest/send", {});
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-dashboard"] });
});

it("useReenableDigestUser POSTs the user_id body and invalidates the dashboard", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 9 }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useReenableDigestUser(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ user_id: 9 });
  expect(post).toHaveBeenCalledWith("/api/admin/digest/reenable", { body: { user_id: 9 } });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-dashboard"] });
});

it("useFetchSource POSTs the slug path param and invalidates the dashboard", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { triggered: true }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useFetchSource(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ slug: "vinnova" });
  expect(post).toHaveBeenCalledWith("/api/admin/sources/{slug}/fetch", {
    params: { path: { slug: "vinnova" } },
  });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-dashboard"] });
});
