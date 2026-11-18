import { readFileSync } from "node:fs";

import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import type { RouteObject } from "react-router-dom";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";

// The lazy AdminLayout chunk, made to REJECT the way a 404 after a deploy does.
const CHUNK_MESSAGE =
  "Failed to fetch dynamically imported module: https://app.invalid/assets/AdminLayout-9f3a1c.js";
vi.mock("./routes/AdminLayout", () => ({
  get AdminLayout(): never {
    throw new Error(CHUNK_MESSAGE);
  },
}));

import { RouteError } from "./components/RouteError";
import { leafPaths } from "./routeLeafPaths";
import { router } from "./router";
import { ROUTER_PROVIDER_PROPS } from "./routerProviderProps";
import { SPA_ROUTES } from "./routes.gen";
import { theme } from "./theme/theme";

// The route set, written out. This is an ALLOWLIST, not a subset check: a subset
// assertion would pass a router that mounted extra pages. Adding a line here is
// the reviewed act.
const RELEASE_1_ROUTES = [
  "/",
  "/calls/:id",
  "/me",
  "/profiles",
  "/profiles/new",
  "/profiles/:id",
  "/profiles/:id/edit",
  "/boards",
  "/boards/:id",
  "/admin",
  "/admin/users",
  "/admin/users/pending",
  "/admin/golden-set",
  "/b/:slug",
  "/login",
  "/register",
  "/auth/pending",
  "/auth/no-access",
  "/auth/error",
];

// Prefixes of the removed proposal workspace. The exact-equality test above
// already holds them out; this names them, so a red run says what came back.
const EXCLUDED_PREFIXES = [
  "/cockpit",
  "/admin/drafting",
  "/admin/deleted-workspaces",
];

it("the router mounts exactly the release-1 route set", () => {
  // Exclude ONLY the in-app soft-404 leaf, by exact value. A `!p.includes("*")`
  // filter here would drop every splat leaf, and a splat is the natural way to
  // mount a subtree: measured, `{ path: "cockpit/*" }` and `{ path:
  // "workspaces/*" }` both passed this test under that filter, the second
  // passing test 2 as well. Excluding a whole class of path from an "exact"
  // allowlist is how a subset check gets reintroduced by accident.
  const concrete = leafPaths(router.routes as RouteObject[]).filter((p) => p !== "/*");
  // Sorted arrays, not Sets: a Set collapses duplicates, so a route mounted
  // twice would compare equal to one mounted once.
  expect([...concrete].sort()).toEqual([...RELEASE_1_ROUTES].sort());
});

it("the router mounts no excluded path", () => {
  const concrete = leafPaths(router.routes as RouteObject[]);
  const leaked = concrete.filter((p) =>
    EXCLUDED_PREFIXES.some((x) => p === x || p.startsWith(x + "/")),
  );
  expect(leaked).toEqual([]);
});

it("every route the router mounts is owned by the shared SPA manifest", () => {
  // The server decides which paths get index.html from routes.gen.ts. A
  // release-1 route the manifest does not own would 404 on a hard refresh.
  //
  // This walks the ROUTER, not RELEASE_1_ROUTES: reading the constant would
  // assert a relation between two constants and be load-bearing only through
  // test 1. Mirrors spa_routes.py:39-44 — prefix ownership with "/" owning
  // only itself.
  const owned = (p: string) =>
    SPA_ROUTES.some((r) => (r === "/" ? p === "/" : p === r || p.startsWith(r + "/")));
  const unowned = leafPaths(router.routes as RouteObject[])
    .filter((p) => p !== "/*")
    .filter((p) => !owned(p));
  expect(unowned).toEqual([]);
});

// --- the root errorElement (review 2026-10-26 M-fe-2) ----------------------
it("every top-level route declares the RouteError errorElement", () => {
  const tops = router.routes as RouteObject[];
  expect(tops.length).toBeGreaterThan(1);
  for (const r of tops) {
    expect((r.errorElement as ReactElement | undefined)?.type).toBe(RouteError);
  }
});

it("no route does asynchronous work the router would wait for before landing a navigation", () => {
  // routerProviderProps.ts: with transitions off, a navigation started by a
  // click or a change is committed before the next event — but only because the
  // router finishes it INSIDE that event. A `loader`, a route-level `lazy` or
  // middleware makes it finish after an await instead, and the page is behind
  // the address again (the flaky match-e2e of 2026-11-18). If you need one of
  // these, measure the throttled browser run again before deleting this test.
  // (React.lazy COMPONENTS, like the admin area's, are not this: they suspend
  // while rendering, after the navigation has landed. `action` is listed because
  // a submission navigation waits the same way; nothing submits today.)
  const offenders: string[] = [];
  const visited: string[] = [];
  const walk = (routes: RouteObject[], trail: string) => {
    for (const route of routes) {
      const here = `${trail}/${route.path ?? (route.index ? "(index)" : "(layout)")}`;
      visited.push(here);
      for (const key of ["loader", "action", "lazy", "middleware", "unstable_middleware"] as const) {
        if ((route as Record<string, unknown>)[key] != null) offenders.push(`${here}: ${key}`);
      }
      if (route.children) walk(route.children, here);
    }
  };
  walk(router.routes, "");
  // The positive control: the walk reached the feed AND the deepest subtree
  // there is. A walk that stopped at the top level would report no offenders.
  expect(visited).toContain("/(layout)/(index)");
  expect(visited).toContain("/(layout)/admin/users/pending");
  expect(offenders).toEqual([]);

  // The same late landing can be switched on for the WHOLE router, with options
  // the route tree does not show (react-router 7.18.2: each adds an await before
  // the navigation completes). router.tsx passes none; this is a scan of its
  // text, with `createBrowserRouter(` as the positive control.
  const src = readFileSync("src/router.tsx", "utf-8");
  expect(src).toContain("createBrowserRouter(");
  expect(src).not.toMatch(/\b(dataStrategy|getContext|patchRoutesOnNavigation)\b/);
});

it("a lazy chunk that 404s renders RouteError with a Reload button, not react-router's screen", async () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  const r = createMemoryRouter(router.routes as RouteObject[], {
    initialEntries: ["/admin"],
  });
  render(
    <MantineProvider theme={theme} defaultColorScheme="light">
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <RouterProvider router={r} {...ROUTER_PROVIDER_PROPS} />
      </QueryClientProvider>
    </MantineProvider>,
  );
  expect(await screen.findByRole("button", { name: /reload/i })).toBeInTheDocument();
  await waitFor(() =>
    expect(screen.queryByText(/Unexpected Application Error/i)).not.toBeInTheDocument(),
  );
  expect(screen.getByRole("alert")).toHaveTextContent(/went wrong|couldn.t load/i);
});

it("every SPA_ROUTES entry owns at least one mounted page (no stale manifest entry)", () => {
  // The other direction of the test above. A manifest entry with no page behind it
  // makes the server answer index.html, then the in-app soft 404, where FastAPI's
  // real JSON 404 belongs. That is what /cockpit became once the workspace left.
  const leaves = leafPaths(router.routes as RouteObject[]).filter((p) => p !== "/*");
  const owns = (r: string, p: string) => (r === "/" ? p === "/" : p === r || p.startsWith(r + "/"));
  expect(SPA_ROUTES.filter((r) => !leaves.some((p) => owns(r, p)))).toEqual([]);
});

it("/login renders the pre-auth Login, not the shell's soft 404", () => {
  const r = createMemoryRouter(router.routes as RouteObject[], { initialEntries: ["/login"] });
  render(
    <MantineProvider theme={theme} defaultColorScheme="light">
      <QueryClientProvider client={new QueryClient()}>
        <RouterProvider router={r} {...ROUTER_PROVIDER_PROPS} />
      </QueryClientProvider>
    </MantineProvider>,
  );
  expect(screen.getByRole("heading", { name: /sign in/i })).toBeInTheDocument();
  expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();
});
