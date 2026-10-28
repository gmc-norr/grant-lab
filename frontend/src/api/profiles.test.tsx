import { QueryClient } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

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
  useTransferProfileOwner,
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

// Review 2026-10-26 M-fe-3 and the invalidation Lows (A8). BEHAVIOURAL, not
// "was invalidateQueries called with X": the cached entry the next screen reads
// must come out STALE. Each case asserts the entry is fresh BEFORE the mutation
// and stale after, so a key nothing registers cannot make the test pass.
describe("a profile write marks every cache the next screen reads stale", () => {
  const stale = (qc: QueryClient, key: unknown[]) => qc.getQueryState(key)?.isInvalidated;

  // The rail's profile picker and the dashboard's Profile facet are both built
  // from the ["calls", params] response, so the key seeded here is the real
  // shape useDashboard uses, not a bare prefix.
  const CALLS_KEY = ["calls", { view: "inbox", profile: 3 }];

  it("a transfer that loses read leaves the profiles LIST stale", async () => {
    // ProfileTransferCard navigates to /profiles straight after this, and with
    // `keep_read_access: false` the outgoing owner can no longer read the
    // profile. The pre-transfer list still named it, so clicking the entry said
    // "This profile isn't shared with you, or it doesn't exist."
    vi.spyOn(clientMod.api, "PUT").mockResolvedValue({
      data: { outgoing_lost_read: true, owner: { user_id: 8, name: "G", email: "g@umu.se" } },
      error: undefined,
    } as never);
    const qc = new QueryClient();
    qc.setQueryData(["profiles"], { profiles: [{ id: 3, name: "Mine" }] });
    expect(stale(qc, ["profiles"])).toBe(false);
    const { result } = renderHook(() => useTransferProfileOwner(3), { wrapper: wrapper(qc) });
    await result.current.mutateAsync({ email: "g@umu.se", keep_read_access: false } as never);
    expect(stale(qc, ["profiles"])).toBe(true);
  });

  it("a transfer leaves the dashboard's profile facet stale", async () => {
    vi.spyOn(clientMod.api, "PUT").mockResolvedValue({
      data: { outgoing_lost_read: true, owner: { user_id: 8, name: "G", email: "g@umu.se" } },
      error: undefined,
    } as never);
    const qc = new QueryClient();
    qc.setQueryData(CALLS_KEY, { facets: { profiles: [{ id: 3, name: "Mine" }] } });
    expect(stale(qc, CALLS_KEY)).toBe(false);
    const { result } = renderHook(() => useTransferProfileOwner(3), { wrapper: wrapper(qc) });
    await result.current.mutateAsync({ email: "g@umu.se", keep_read_access: false } as never);
    expect(stale(qc, CALLS_KEY)).toBe(true);
  });

  it("creating a profile leaves /settings' digest subscription list stale", async () => {
    // ["me-settings"] carries `profiles` — the checkbox list the weekly digest
    // subscribes to. A profile created from /profiles never appeared there
    // inside App.tsx's 20 s staleTime, so it could not be subscribed to.
    vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 9 }, error: undefined } as never);
    const qc = new QueryClient();
    qc.setQueryData(["me-settings"], { profiles: [] });
    expect(stale(qc, ["me-settings"])).toBe(false);
    const { result } = renderHook(() => useCreateProfile(), { wrapper: wrapper(qc) });
    await result.current.mutateAsync({ name: "Genomics" } as never);
    expect(stale(qc, ["me-settings"])).toBe(true);
  });

  it("updating a profile leaves the dashboard stale, so re-scoring is not picked up", async () => {
    // An edit re-ranks the profile server-side. ["calls"] carries both the
    // renamed facet entry and `ranking.pending`, which is what drives the
    // "Scoring N calls…" banner and its 5 s poll: without this the user sees
    // neither the new name nor that anything is being re-scored.
    vi.spyOn(clientMod.api, "PUT").mockResolvedValue({ data: { id: 3 }, error: undefined } as never);
    const qc = new QueryClient();
    qc.setQueryData(CALLS_KEY, { facets: { profiles: [{ id: 3, name: "Old" }] } });
    expect(stale(qc, CALLS_KEY)).toBe(false);
    const { result } = renderHook(() => useUpdateProfile(3), { wrapper: wrapper(qc) });
    await result.current.mutateAsync({ name: "Renamed" } as never);
    expect(stale(qc, CALLS_KEY)).toBe(true);
  });
});
