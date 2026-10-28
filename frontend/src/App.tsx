import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router-dom";
import { router } from "virtual:app-router";

import { is4xx } from "./api/client";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { APP_EDITION } from "./edition";
import { theme } from "./theme/theme";

// Global stale time prevents thundering-herd refetches on tab focus for the
// heavy read-models (calls list, profiles, etc.).  Genuine live pollers
// (problems, turns) set their own explicit refetchInterval and don't rely on
// window-focus refetches anyway.
//
// EXPORTED so a test can read the policy off the client the app actually
// builds, rather than re-stating the predicate and pinning a copy of it
// (src/App.test.tsx). Exactly one client is constructed in this file and that
// test counts the constructor calls, so a second one with its own defaults
// cannot slip in beside it.
export const appQueryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 20_000, // 20 s — data is "fresh" for 20 s; no refetch on focus unless stale
      refetchOnWindowFocus: false,
      // NOT TanStack's default `retry: 3`, which retried 4xx too. Measured by
      // review 2026-10-26: a 500 or a dropped connection on the landing page
      // showed "Loading…" for 7.3 s over 4 requests before saying anything, and
      // `?sort=foo` — a 422 no retry can ever change — took 7.5 s to the same
      // dead end. Two retries is the cap for a failure that MIGHT be transient;
      // a refusal gets none. `failureCount` is 0 on the first failure (see
      // query-core's retryer), so `< 2` is two retries, three requests in all.
      retry: (n, e) => !is4xx(e) && n < 2,
    },
  },
});

export function App() {
  return (
    <ErrorBoundary>
      {/* The --app-* tokens are ours, but Mantine's own components (Menu,
          Button, Badge) read defaultColorScheme, and theme.match.css cannot
          reach them. Leaving it "dark" puts a dark dropdown on a light page. */}
      <MantineProvider theme={theme} defaultColorScheme={APP_EDITION === "match" ? "light" : "dark"}>
        <QueryClientProvider client={appQueryClient}>
          <RouterProvider router={router} />
        </QueryClientProvider>
      </MantineProvider>
    </ErrorBoundary>
  );
}
