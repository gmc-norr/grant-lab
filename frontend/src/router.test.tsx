import type { RouteObject } from "react-router-dom";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { router } from "./router";
import { CallReadingPane } from "./routes/CallReadingPane";
import { SPA_ROUTES } from "./routes.gen";
import { theme } from "./theme/theme";

// Collect every concrete LEAF page path the router can match, as an absolute
// path. A pathless layout route (no `path`) contributes nothing itself; its
// children inherit the parent base. An index route resolves to the parent base.
function leafPaths(routes: RouteObject[], base = ""): string[] {
  const out: string[] = [];
  for (const r of routes) {
    const here =
      r.path == null
        ? base
        : r.path.startsWith("/")
          ? r.path
          : `${base.replace(/\/$/, "")}/${r.path}`;
    if (r.children?.length) out.push(...leafPaths(r.children, here));
    else out.push(r.index ? base || "/" : here);
  }
  return out;
}

// router.tsx is the only HAND-maintained consumer of the route-ownership
// manifest. Pin its concrete page paths (excluding the in-app "*" soft-404) to
// SPA_ROUTES by collapsing each concrete leaf to the manifest entry that OWNS it
// (the way is_spa_route matches a subtree: "/cockpit" owns "/cockpit/new" and
// "/cockpit/:id"; "/" owns only itself).
function toOwningPrefix(p: string): string {
  const owner = SPA_ROUTES.filter((r) =>
    r === "/" ? p === "/" : p === r || p.startsWith(r + "/"),
  ).sort((a, b) => b.length - a.length)[0];
  return owner ?? p; // unowned -> stays itself -> set mismatch -> guard fails
}

it("router concrete paths are all owned by the SPA_ROUTES manifest (drift guard)", () => {
  const concrete = leafPaths(router.routes as RouteObject[])
    .filter((p) => !p.includes("*"))
    .map(toOwningPrefix);
  expect(new Set(concrete)).toEqual(new Set(SPA_ROUTES));
});

it("the / index (calls) route exposes the reading pane via its handle", () => {
  const shell = (router.routes as RouteObject[]).find((r) => r.children?.some((c) => c.index));
  const index = shell?.children?.find((c) => c.index);
  const handle = index?.handle as { RightPane?: unknown; rightPaneLabel?: string } | undefined;
  expect(handle?.RightPane).toBe(CallReadingPane);
  expect(handle?.rightPaneLabel).toBe("reading pane");
});

it("/login renders the pre-auth Login, not the AppShell soft-404", () => {
  const r = createMemoryRouter(router.routes, { initialEntries: ["/login"] });
  render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={new QueryClient()}>
        <RouterProvider router={r} />
      </QueryClientProvider>
    </MantineProvider>,
  );
  expect(screen.getByRole("heading", { name: /sign in/i })).toBeInTheDocument();
  expect(screen.queryByLabelText("Primary")).not.toBeInTheDocument(); // no ActivityBar
});
