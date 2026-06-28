import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MantineProvider } from "@mantine/core";
import { fireEvent, render, screen } from "@testing-library/react";
import { RouterProvider, createMemoryRouter, useNavigate } from "react-router-dom";
import type { RouteObject } from "react-router-dom";
import { useEffect } from "react";
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

it("REMOUNTS the cockpit subtree (main + right pane) on a workspace-id change but not on a query-only change (Defect 3)", () => {
  // The cockpit right pane (CockpitChat) holds per-turn local state (jobId/streamed)
  // and the main subtree holds bottomTab/openPeek etc. A param-only nav /cockpit/A ->
  // /cockpit/B keeps the layout mounted; without a per-id key those instances would
  // reconcile and bleed A's state into B (SSE targeting the wrong workspace). Keying by
  // the cockpit id forces a fresh mount on id change. A query-string change must NOT
  // remount (it's the same workspace — section/tab navigation).
  let paneMounts = 0;
  let mainMounts = 0;
  function PaneProbe() {
    useEffect(() => {
      paneMounts += 1;
    }, []);
    return <aside aria-label="Chat" />;
  }
  function MainProbe() {
    useEffect(() => {
      mainMounts += 1;
    }, []);
    return (
      <>
        <p>COCKPIT BODY</p>
        <Nav />
      </>
    );
  }
  function Nav() {
    const navigate = useNavigate();
    return (
      <>
        <button type="button" onClick={() => navigate("/cockpit/ws-b")}>
          to-b
        </button>
        <button type="button" onClick={() => navigate("/cockpit/ws-a?sec=2")}>
          same-ws-query
        </button>
      </>
    );
  }
  const router = createMemoryRouter(
    [
      {
        element: <AppShell />,
        children: [
          {
            path: "cockpit/:id",
            element: <MainProbe />,
            handle: { RightPane: PaneProbe },
          },
        ],
      },
    ],
    { initialEntries: ["/cockpit/ws-a"] },
  );
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MantineProvider theme={theme} defaultColorScheme="dark">
        <RouterProvider router={router} />
      </MantineProvider>
    </QueryClientProvider>,
  );
  expect(paneMounts).toBe(1);
  expect(mainMounts).toBe(1);

  // A query-only navigation on the SAME workspace must NOT remount (same key).
  fireEvent.click(screen.getByRole("button", { name: /same-ws-query/i }));
  expect(paneMounts).toBe(1);
  expect(mainMounts).toBe(1);

  // Switching workspace id MUST remount both halves (fresh local state).
  fireEvent.click(screen.getByRole("button", { name: /to-b/i }));
  expect(paneMounts).toBe(2);
  expect(mainMounts).toBe(2);
});

it("renders a resize handle beside the sidebar when a sidebar is present", () => {
  function TestSidebar() {
    return <p>SIDEBAR CONTENT</p>;
  }
  renderShellWithRoutes("/", [
    { index: true, element: <p>BODY</p>, handle: { Sidebar: TestSidebar } },
  ]);
  expect(screen.getByRole("separator", { name: "Resize sidebar" })).toBeInTheDocument();
});

it("renders a resize handle beside the right pane when one is present", () => {
  renderShellWithRoutes("/", [
    {
      index: true,
      element: <p>BODY</p>,
      handle: { RightPane: () => <aside aria-label="Chat" /> },
    },
  ]);
  expect(screen.getByRole("separator", { name: "Resize chat" })).toBeInTheDocument();
});

it("renders no resize handle when neither a sidebar nor a right pane is present", () => {
  renderShellAt("/");
  expect(screen.queryByRole("separator")).toBeNull();
});
