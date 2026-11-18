import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderOptions } from "@testing-library/react";
import { createContext, type ReactElement, type ReactNode, useContext, useState } from "react";
import { createMemoryRouter, MemoryRouter, RouterProvider } from "react-router-dom";

import { ROUTER_PROVIDER_PROPS } from "./routerProviderProps";
import { theme } from "./theme/theme";

// `queryClient`: a test that spies on invalidateQueries must hold the SAME
// client the component uses, so the instance is injectable rather than buried
// in here. Defaulted, so every existing caller is unchanged.
type Options = { route?: string; queryClient?: QueryClient; dataRouter?: boolean } & Omit<RenderOptions, "wrapper">;

// `dataRouter: true` renders under a DATA router (createMemoryRouter +
// RouterProvider) instead of <MemoryRouter>. Production runs createBrowserRouter,
// which is the data kind: its navigations are asynchronous and a second one
// interrupts the first, and <MemoryRouter>'s do neither. A test about the order
// navigations land in has to be able to ask for the real kind.
//
// It is given the SAME provider props as the app's (ROUTER_PROVIDER_PROPS), so a
// change to how the app's router hands React a navigation changes it here too.
//
// The router is created once, so the route element cannot close over `children`
// (a rerender would keep showing the first ones); it reads them from context.
const DataRouterChildren = createContext<ReactNode>(null);
function DataRouterSlot() {
  return useContext(DataRouterChildren);
}
function DataRouter({ route, children }: { route: string; children: ReactNode }) {
  const [router] = useState(() =>
    createMemoryRouter([{ path: "*", element: <DataRouterSlot /> }], { initialEntries: [route] }),
  );
  return (
    <DataRouterChildren.Provider value={children}>
      <RouterProvider router={router} {...ROUTER_PROVIDER_PROPS} />
    </DataRouterChildren.Provider>
  );
}

export function queryWrapper(queryClient = new QueryClient()) {
  return function QueryWrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

// Single source of truth for rendering components that need the app providers.
// MantineProvider is forced to the dark scheme to match production. Components
// using useLocation / NavLink get a MemoryRouter at `route` (default "/").
export function renderWithProviders(ui: ReactElement, opts: Options = {}) {
  const { route = "/", queryClient, dataRouter = false, ...rest } = opts;
  const qc = queryClient ?? new QueryClient();
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <MantineProvider theme={theme} defaultColorScheme="dark">
        <QueryClientProvider client={qc}>
          {dataRouter ? (
            <DataRouter route={route}>{children}</DataRouter>
          ) : (
            <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
          )}
        </QueryClientProvider>
      </MantineProvider>
    );
  }
  return render(ui, { wrapper: Wrapper, ...rest });
}
