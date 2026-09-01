import { QueryClient } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { queryWrapper as wrapper } from "../test-utils";
import * as clientMod from "./client";
import { useAuthConfig, useLogin, useLogout, useRegister } from "./auth";

afterEach(() => {
  vi.restoreAllMocks();
});

it("useAuthConfig GETs /api/auth/config", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { google_oidc_enabled: true, allowed_domains: ["ki.se"] }, error: undefined,
  } as never);
  const { result } = renderHook(() => useAuthConfig(), { wrapper: wrapper(new QueryClient()) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(get).toHaveBeenCalledWith("/api/auth/config", {});
});

it("useLogin POSTs credentials + returns the redirect", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { redirect: "/" }, error: undefined, response: new Response(),
  } as never);
  const { result } = renderHook(() => useLogin(), { wrapper: wrapper(new QueryClient()) });
  const out = await result.current.mutateAsync({ email: "a@ki.se", password: "pw-long-enough" });
  expect(out).toEqual({ redirect: "/" });
  expect(post).toHaveBeenCalledWith("/api/auth/login", {
    body: { email: "a@ki.se", password: "pw-long-enough", next: null },
  });
});

it("useLogin throws ApiError on a 400 (unwrap does NOT redirect — 400 != 401)", async () => {
  // A 401 would trigger unwrap's redirectToLogin (window.location) -> loop; a 400
  // must just throw. Mocking a 400 here exercises that path without touching location.
  const redirect = vi.spyOn(clientMod, "redirectToLogin");
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    error: { error: { code: "bad_request", message: "Invalid email or password." } },
    response: new Response(null, { status: 400 }),
  } as never);
  const { result } = renderHook(() => useLogin(), { wrapper: wrapper(new QueryClient()) });
  await expect(
    result.current.mutateAsync({ email: "a@ki.se", password: "bad" }),
  ).rejects.toThrow(/Invalid email or password/);
  expect(redirect).not.toHaveBeenCalled(); // 400 != 401, no redirect loop
});

it("useRegister POSTs email+name", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { ok: true }, error: undefined,
  } as never);
  const { result } = renderHook(() => useRegister(), { wrapper: wrapper(new QueryClient()) });
  await result.current.mutateAsync({ email: "n@ki.se", name: "N" });
  expect(post).toHaveBeenCalledWith("/api/auth/register", { body: { email: "n@ki.se", name: "N" } });
});

it("useLogout POSTs logout", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { ok: true }, error: undefined,
  } as never);
  const { result } = renderHook(() => useLogout(), { wrapper: wrapper(new QueryClient()) });
  await result.current.mutateAsync();
  expect(post).toHaveBeenCalledWith("/api/auth/logout", {});
});
