import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "./client";
import {
  useAccountSettings,
  useChangePassword,
  useSaveSubscriptions,
  useUpdateName,
} from "./account";

afterEach(() => {
  vi.restoreAllMocks();
});

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

const SETTINGS = {
  name: "Pär", email: "p@ki.se", is_admin: false,
  google_oidc_enabled: false, google_linked: false, has_password: true, profiles: [],
};

it("useAccountSettings GETs /api/me/settings and unwraps", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: SETTINGS, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useAccountSettings(), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data).toEqual(SETTINGS);
  expect(get).toHaveBeenCalledWith("/api/me/settings", {});
});

it("useUpdateName POSTs the name + invalidates ['me-settings']", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { name: "New" }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useUpdateName(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync("New");
  expect(post).toHaveBeenCalledWith("/api/me/name", { body: { name: "New" } });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["me-settings"] });
});

it("useChangePassword POSTs both passwords", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { ok: true }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useChangePassword(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ current_password: "a", new_password: "blonglongpass" });
  expect(post).toHaveBeenCalledWith("/api/me/password", {
    body: { current_password: "a", new_password: "blonglongpass" },
  });
});

it("useSaveSubscriptions PUTs the id set + invalidates ['me-settings']", async () => {
  const put = vi.spyOn(clientMod.api, "PUT").mockResolvedValue({ data: { subscribed: [1] }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useSaveSubscriptions(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync([1]);
  expect(put).toHaveBeenCalledWith("/api/me/subscriptions", { body: { profile_ids: [1] } });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["me-settings"] });
});
