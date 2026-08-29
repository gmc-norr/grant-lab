import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MantineProvider } from "@mantine/core";
import { render } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";

import { theme } from "../theme/theme";

// The reveal exists to open the DESTINATION's pane. A route with no sidebar of
// its own (Profiles, Settings, Admin) must still hand the rail a working
// callback — gating it on the SOURCE route's sidebar made clicking Calls from
// Settings a no-op, which is the one case the feature is for.
//
// Asserted at the wiring seam: jsdom has no layout engine, so the panel's real
// collapse/expand cannot be observed. Capturing the prop CAN fail, and does.
const seen = vi.hoisted(() => ({ props: [] as Record<string, unknown>[] }));
vi.mock("./ActivityBar", () => ({
  ActivityBar: (props: Record<string, unknown>) => {
    seen.props.push(props);
    return <nav aria-label="Primary" />;
  },
}));

import { AppShell } from "./AppShell";

function renderAt(route: string, withSidebar: boolean) {
  seen.props = [];
  const router = createMemoryRouter(
    [
      {
        element: <AppShell />,
        children: [
          {
            index: true,
            element: <p>BODY</p>,
            ...(withSidebar ? { handle: { Sidebar: () => <p>PANE</p> } } : {}),
          },
        ],
      },
    ],
    { initialEntries: [route] },
  );
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MantineProvider theme={theme} defaultColorScheme="dark">
        <RouterProvider router={router} />
      </MantineProvider>
    </QueryClientProvider>,
  );
  return seen.props[seen.props.length - 1];
}

it("hands the rail a reveal callback even where the route has NO sidebar", () => {
  const props = renderAt("/", false);
  expect(typeof props.onRevealSidebar).toBe("function");
});

it("still withholds the TOGGLE where there is no sidebar to toggle", () => {
  // Toggle is about THIS route's pane, so it stays gated. Reveal is about the
  // next one. Losing that distinction is the easy wrong fix.
  const props = renderAt("/", false);
  expect(props.onToggleSidebar).toBeUndefined();
});

it("supplies both where the route does have a sidebar", () => {
  const props = renderAt("/", true);
  expect(typeof props.onRevealSidebar).toBe("function");
  expect(typeof props.onToggleSidebar).toBe("function");
});
