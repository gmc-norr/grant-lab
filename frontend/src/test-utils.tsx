import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderOptions } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";

import { theme } from "./theme/theme";

// `queryClient`: a test that spies on invalidateQueries must hold the SAME
// client the component uses, so the instance is injectable rather than buried
// in here. Defaulted, so every existing caller is unchanged.
type Options = { route?: string; queryClient?: QueryClient } & Omit<RenderOptions, "wrapper">;

export function queryWrapper(queryClient = new QueryClient()) {
  return function QueryWrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

// Single source of truth for rendering components that need the app providers.
// MantineProvider is forced to the dark scheme to match production. Components
// using useLocation / NavLink get a MemoryRouter at `route` (default "/").
export function renderWithProviders(ui: ReactElement, opts: Options = {}) {
  const { route = "/", queryClient, ...rest } = opts;
  const qc = queryClient ?? new QueryClient();
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <MantineProvider theme={theme} defaultColorScheme="dark">
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
        </QueryClientProvider>
      </MantineProvider>
    );
  }
  return render(ui, { wrapper: Wrapper, ...rest });
}
