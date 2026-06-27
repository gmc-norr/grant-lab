import { useMutation, useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";

export function useAuthConfig() {
  // PUBLIC endpoint — returns 200 logged-out, so it never 401-loops. This is the
  // ONLY /api call the pre-auth pages make; it is NOT an authenticated call.
  return useQuery({
    queryKey: ["auth-config"],
    queryFn: async () => unwrap(await api.GET("/api/auth/config", {})),
    retry: false,
    staleTime: Infinity,
  });
}

export function useLogin() {
  return useMutation({
    mutationFn: async (vars: { email: string; password: string; next?: string | null }) =>
      unwrap(
        await api.POST("/api/auth/login", {
          body: { email: vars.email, password: vars.password, next: vars.next ?? null },
        }),
      ),
  });
}

export function useRegister() {
  return useMutation({
    mutationFn: async (vars: { email: string; name?: string | null }) =>
      unwrap(await api.POST("/api/auth/register", { body: { email: vars.email, name: vars.name ?? null } })),
  });
}

export function useLogout() {
  return useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/auth/logout", {})),
  });
}
