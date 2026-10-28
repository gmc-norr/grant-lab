import type { RouteObject } from "react-router-dom";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { expect, it, vi } from "vitest";

// The lazy AdminLayout chunk, made to REJECT the way a 404 after a deploy does.
// Hoisted by vitest above the `./router` import below, which is what lets the
// router's own `lazy(() => import("./routes/AdminLayout"))` pick it up.
const CHUNK_MESSAGE =
  "Failed to fetch dynamically imported module: https://app.invalid/assets/AdminLayout-9f3a1c.js";
vi.mock("./routes/AdminLayout", () => ({
  get AdminLayout(): never {
    throw new Error(CHUNK_MESSAGE);
  },
}));

import { RouteError } from "./components/RouteError";
import { router } from "./router";
import { leafPaths } from "./routeLeafPaths";
import { CallReadingPane } from "./routes/CallReadingPane";
import { SPA_ROUTES } from "./routes.gen";
import { theme } from "./theme/theme";

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

// --- the root errorElement (review 2026-10-26 M-fe-2) ----------------------
//
// There was no errorElement anywhere, so a lazy chunk that 404s after a deploy —
// the normal state of a tab left open across a release — replaced the whole app
// with react-router's developer screen: `mainNav=0 text="Unexpected Application
// Error! Failed to fetch dynamically imported module … 💿 Hey developer 👋 …"`.
// The ErrorBoundary in App.tsx wraps RouterProvider and can never catch those:
// react-router's own RenderErrorBoundary is inside it and swallows them first.
//
// Both halves are pinned. The structural half says the element is DECLARED on
// every top-level route (the three sibling pathless layouts — AppShell,
// PreAuthLayout, BoardLayout — are the roots there are, and a route error under
// any of them must land somewhere). The behavioural half REJECTS A REAL LAZY
// IMPORT and asserts what the member sees.
it("every top-level route declares the RouteError errorElement", () => {
  const tops = router.routes as RouteObject[];
  expect(tops.length).toBeGreaterThan(1);
  for (const r of tops) {
    expect((r.errorElement as ReactElement | undefined)?.type).toBe(RouteError);
  }
});

it("a lazy chunk that 404s renders RouteError with a Reload button, not react-router's screen", async () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  // The chunk hash in the message is what a real deploy produces, and the
  // getter is what makes the dynamic import REJECT rather than render a stub:
  // router.tsx's `import(...).then((m) => ({ default: m.AdminLayout }))` reads
  // the name, so the throw happens inside the import chain.
  const r = createMemoryRouter(router.routes as RouteObject[], { initialEntries: ["/admin"] });
  render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <RouterProvider router={r} />
      </QueryClientProvider>
    </MantineProvider>,
  );
  expect(await screen.findByRole("button", { name: /reload/i })).toBeInTheDocument();
  await waitFor(() =>
    expect(screen.queryByText(/Unexpected Application Error/i)).not.toBeInTheDocument(),
  );
  expect(screen.getByRole("alert")).toHaveTextContent(/went wrong|couldn.t load/i);
});
