import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router-dom";

import { ErrorBoundary } from "./components/ErrorBoundary";
import { router } from "./router";
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
      <MantineProvider theme={theme} defaultColorScheme="dark">
        <QueryClientProvider client={qc}>
          <RouterProvider router={router} />
        </QueryClientProvider>
      </MantineProvider>
    </ErrorBoundary>
  );
}
