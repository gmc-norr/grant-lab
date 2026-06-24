import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import type { RouteObject } from "react-router-dom";
import { expect, it } from "vitest";

import { theme } from "../theme/theme";
import { AppShell } from "./AppShell";

function renderShellAt(route: string) {
  const router = createMemoryRouter(
    [
      {
        element: <AppShell />,
        children: [{ index: true, element: <p>ROUTED BODY</p> }],
      },
    ],
    { initialEntries: [route] },
  );
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MantineProvider theme={theme} defaultColorScheme="dark">
        <RouterProvider router={router} />
      </MantineProvider>
    </QueryClientProvider>,
  );
}

it("renders the chrome regions and the routed outlet content", () => {
  renderShellAt("/");
  // "Funding calls" is the per-route title (unique). NB: "Call Manager" appears
  // in BOTH the title bar brand and the status bar, so don't getByText it here.
  expect(screen.getByText("Funding calls")).toBeInTheDocument(); // title bar
  expect(screen.getByLabelText("Primary")).toBeInTheDocument(); // activity bar
  expect(screen.getByLabelText("Status bar")).toBeInTheDocument(); // status bar
  expect(screen.getByText("ROUTED BODY")).toBeInTheDocument(); // <Outlet/>
});

function renderShellWithRoutes(route: string, children: RouteObject[]) {
  const router = createMemoryRouter([{ element: <AppShell />, children }], {
    initialEntries: [route],
  });
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MantineProvider theme={theme} defaultColorScheme="dark">
        <RouterProvider router={router} />
      </MantineProvider>
    </QueryClientProvider>,
  );
}

it("renders the sidebar region when the active route declares handle.Sidebar", () => {
  function TestSidebar() {
    return <p>SIDEBAR CONTENT</p>;
  }
  renderShellWithRoutes("/", [
    { index: true, element: <p>BODY</p>, handle: { Sidebar: TestSidebar } },
  ]);
  expect(screen.getByLabelText("Sidebar")).toBeInTheDocument();
  expect(screen.getByText("SIDEBAR CONTENT")).toBeInTheDocument();
});

it("omits the sidebar region when no matched route declares one", () => {
  renderShellWithRoutes("/", [{ index: true, element: <p>BODY</p> }]);
  expect(screen.queryByLabelText("Sidebar")).toBeNull();
});

it("renders the right-pane region when the active route declares handle.RightPane", () => {
  renderShellWithRoutes("/", [
    {
      index: true,
      element: <p>BODY</p>,
      handle: { RightPane: () => <aside aria-label="Chat" /> },
    },
  ]);
  expect(screen.getByRole("complementary", { name: /chat/i })).toBeInTheDocument();
});
