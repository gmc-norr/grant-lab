import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderOptions } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";

import { theme } from "./theme/theme";

type Options = { route?: string } & Omit<RenderOptions, "wrapper">;

// Single source of truth for rendering components that need the app providers.
// MantineProvider is forced to the dark scheme to match production. Components
// using useLocation / NavLink get a MemoryRouter at `route` (default "/").
export function renderWithProviders(ui: ReactElement, opts: Options = {}) {
  const { route = "/", ...rest } = opts;
  const qc = new QueryClient();
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
