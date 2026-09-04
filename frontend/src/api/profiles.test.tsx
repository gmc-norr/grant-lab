import { QueryClient } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { queryWrapper as wrapper } from "../test-utils";
import * as clientMod from "./client";
import {
  useAddProfileViewer,
  useCreateProfile,
  useProfile,
  useProfilesList,
  useProfileViewers,
  useReplaceProfileViewers,
  useSetProfileVisibility,
  useUpdateProfile,
} from "./profiles";

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

// --- sharing -------------------------------------------------------------

const VIEWERS_PATH = "/api/profiles/{profile_id}/viewers";
const PARAMS = { params: { path: { profile_id: 5 } } };

it("useProfileViewers GETs the viewer list, and stays off the wire when disabled", async () => {
  const get = vi
    .spyOn(clientMod.api, "GET")
    .mockResolvedValue({ data: { viewers: [] }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useProfileViewers(5, true), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(get).toHaveBeenCalledWith(VIEWERS_PATH, PARAMS);

  get.mockClear();
  const off = renderHook(() => useProfileViewers(5, false), { wrapper: wrapper(new QueryClient()) });
  expect(off.result.current.fetchStatus).toBe("idle");
  expect(get).not.toHaveBeenCalled();
});

it("useAddProfileViewer POSTs the email and seeds the list from the response", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { viewers: [{ user_id: 7, name: "Ada", email: "ada@umu.se" }] }, error: undefined,
  } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useAddProfileViewer(5), { wrapper: wrapper(qc) });
  await result.current.mutateAsync("ada@umu.se");
  expect(post).toHaveBeenCalledWith(VIEWERS_PATH, { ...PARAMS, body: { email: "ada@umu.se" } });
  await waitFor(() =>
    expect(qc.getQueryData(["profile-viewers", 5])).toEqual({
      viewers: [{ user_id: 7, name: "Ada", email: "ada@umu.se" }],
    }),
  );
});

it("useReplaceProfileViewers PUTs the ids and reconciles from the server's answer", async () => {
  const put = vi
    .spyOn(clientMod.api, "PUT")
    .mockResolvedValue({ data: { viewers: [] }, error: undefined } as never);
  const qc = new QueryClient();
  qc.setQueryData(["profile-viewers", 5], { viewers: [{ user_id: 7, name: "Ada", email: "a@b.se" }] });
  const { result } = renderHook(() => useReplaceProfileViewers(5), { wrapper: wrapper(qc) });
  await result.current.mutateAsync([]);
  expect(put).toHaveBeenCalledWith(VIEWERS_PATH, { ...PARAMS, body: { user_ids: [] } });
  await waitFor(() => expect(qc.getQueryData(["profile-viewers", 5])).toEqual({ viewers: [] }));
});

it("useSetProfileVisibility PUTs the value and invalidates the profile", async () => {
  const put = vi
    .spyOn(clientMod.api, "PUT")
    .mockResolvedValue({ data: { visibility: "org" }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useSetProfileVisibility(5), { wrapper: wrapper(qc) });
  await result.current.mutateAsync("org");
  expect(put).toHaveBeenCalledWith("/api/profiles/{profile_id}/visibility", {
    ...PARAMS, body: { visibility: "org" },
  });
  await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["profile", 5] }));
});
