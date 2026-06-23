import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router-dom";

import { ErrorBoundary } from "./components/ErrorBoundary";
import { router } from "./router";
import { theme } from "./theme/theme";

const qc = new QueryClient({ defaultOptions: { queries: { refetchOnWindowFocus: true } } });

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
