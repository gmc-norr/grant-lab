import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";

// Current session (GET /api/me). A 401 (cookie gone/expired) is handled by
// `unwrap`, which does the full-page nav to the server-rendered /login (with a
// `next`) and throws — the single 401 chokepoint shared by every API call.
// retry:false so an unauthenticated load redirects immediately, not after retries.
export function useSession() {
  return useQuery({
    queryKey: ["me"],
    queryFn: async () => unwrap(await api.GET("/api/me", {})),
    retry: false,
    staleTime: 60_000,
  });
}
