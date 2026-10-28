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
      // dead end. ONE retry is the cap for a failure that MIGHT be transient; a
      // refusal gets none.
      //
      // WHY `< 1` AND NOT `< 2` (changed 2026-10-28). `n` is query-core's
      // `failureCount`, which is 0 on the first failure and is incremented only
      // AFTER this predicate answers (@tanstack/query-core@5.101.0
      // src/retryer.ts: `let failureCount = 0` :80, `retry(failureCount, error)`
      // :178-179, the increment at :187). So `< 2` was two retries — THREE
      // requests — while the acceptance criterion for this policy was "a 500 on
      // the feed shows QueryError with Retry within 2 requests". The criterion
      // is the requirement stated as an outcome; `< 2` was arithmetic for it,
      // and the arithmetic was the half that was wrong. Two requests is also the
      // more conservative behaviour: less load on a single-instance app that is
      // already failing, and an error screen in ~1 s instead of ~3 s for the
      // member watching it. App.test.tsx counts the REQUESTS on a real 500, so
      // the number cannot drift back without reddening.
      retry: (n, e) => !is4xx(e) && n < 1,
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
