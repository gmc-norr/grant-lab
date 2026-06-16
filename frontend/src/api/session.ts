import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";

// Current session (GET /api/me). A 401 means the cookie is gone/expired → do a
// full-page nav to the server-rendered /login (not a client route). retry:false
// so an unauthenticated load redirects immediately instead of retrying.
export function useSession() {
  return useQuery({
    queryKey: ["me"],
    queryFn: async () => {
      const res = await api.GET("/api/me", {});
      if (res.response.status === 401) {
        window.location.assign("/login");
        throw new Error("unauthenticated");
      }
      return unwrap(res);
    },
    retry: false,
    staleTime: 60_000,
  });
}
