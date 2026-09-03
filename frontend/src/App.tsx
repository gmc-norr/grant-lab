import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router-dom";
import { router } from "virtual:app-router";

import { ErrorBoundary } from "./components/ErrorBoundary";
import { APP_EDITION } from "./edition";
import { theme } from "./theme/theme";

// Global stale time prevents thundering-herd refetches on tab focus for the
// heavy read-models (calls list, profiles, etc.).  Genuine live pollers
// (problems, turns) set their own explicit refetchInterval and don't rely on
// window-focus refetches anyway.
const qc = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 20_000, // 20 s — data is "fresh" for 20 s; no refetch on focus unless stale
      refetchOnWindowFocus: false,
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
        <QueryClientProvider client={qc}>
          <RouterProvider router={router} />
        </QueryClientProvider>
      </MantineProvider>
    </ErrorBoundary>
  );
}
