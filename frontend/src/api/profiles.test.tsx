import { QueryClient } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { queryWrapper as wrapper } from "../test-utils";
import * as clientMod from "./client";
import { useCreateProfile, useProfile, useProfilesList, useUpdateProfile } from "./profiles";

afterEach(() => {
  vi.restoreAllMocks();
});

it("useProfilesList GETs /api/profiles", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: { profiles: [] }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useProfilesList(), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(get).toHaveBeenCalledWith("/api/profiles", {});
});

it("useProfile GETs the detail by id", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: { id: 5, bootstrap_status: null }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useProfile(5), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(get).toHaveBeenCalledWith("/api/profiles/{profile_id}", { params: { path: { profile_id: 5 } } });
});

it("useCreateProfile POSTs the body and returns the id", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 9 }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useCreateProfile(), { wrapper: wrapper(qc) });
  const out = await result.current.mutateAsync({ name: "X" } as never);
  expect(out).toEqual({ id: 9 });
  expect(post).toHaveBeenCalledWith("/api/profiles", { body: { name: "X" } });
});

it("useUpdateProfile PUTs the body + invalidates", async () => {
  const put = vi.spyOn(clientMod.api, "PUT").mockResolvedValue({ data: { id: 5 }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useUpdateProfile(5), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ description: "d" } as never);
  expect(put).toHaveBeenCalledWith("/api/profiles/{profile_id}", { params: { path: { profile_id: 5 } }, body: { description: "d" } });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["profile", 5] });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["profiles"] });
});
