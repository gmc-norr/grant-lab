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
import { simpleRouter } from "./router.simple";
import { SPA_ROUTES } from "./routes.gen";
import { theme } from "./theme/theme";

// The release-1 route set, written out. This is an ALLOWLIST, not a subset
// check: routes.gen.ts already contains "/cockpit", so a subset assertion would
// happily pass a router that mounted the whole cockpit. Adding a line here is
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
  // Mounted 2026-09-07: the golden set is the ranking system's own evaluation
  // surface, not proposal assist, and match is the only edition that ships.
  "/admin/golden-set",
  "/b/:slug",
  "/login",
  "/register",
  "/auth/pending",
  "/auth/no-access",
  "/auth/error",
];

// Prefixes the match edition must never mount. Spec §3 "explicitly out of
// release 1". /admin/golden-set left this list on 2026-09-07 (spec §10) — it is
// in RELEASE_1_ROUTES above instead, so the exact-equality test is what holds it.
const EXCLUDED_PREFIXES = [
  "/cockpit",
  "/admin/drafting",
  "/admin/deleted-workspaces",
];

it("the simple router mounts exactly the release-1 route set", () => {
  // Exclude ONLY the in-app soft-404 leaf, by exact value. A `!p.includes("*")`
  // filter here would drop every splat leaf, and a splat is the natural way to
  // mount a subtree: measured, `{ path: "cockpit/*" }` and `{ path:
  // "workspaces/*" }` both passed this test under that filter, the second
  // passing test 2 as well. Excluding a whole class of path from an "exact"
  // allowlist is how a subset check gets reintroduced by accident.
  const concrete = leafPaths(simpleRouter.routes as RouteObject[]).filter((p) => p !== "/*");
  // Sorted arrays, not Sets: a Set collapses duplicates, so a route mounted
  // twice would compare equal to one mounted once.
  expect([...concrete].sort()).toEqual([...RELEASE_1_ROUTES].sort());
});

it("the simple router mounts no excluded path", () => {
  const concrete = leafPaths(simpleRouter.routes as RouteObject[]);
  const leaked = concrete.filter((p) =>
    EXCLUDED_PREFIXES.some((x) => p === x || p.startsWith(x + "/")),
  );
  expect(leaked).toEqual([]);
});

it("every route the simple router mounts is owned by the shared SPA manifest", () => {
  // The server decides which paths get index.html from routes.gen.ts. A
  // release-1 route the manifest does not own would 404 on a hard refresh.
  //
  // This walks the ROUTER, not RELEASE_1_ROUTES: reading the constant would
  // assert a relation between two constants and be load-bearing only through
  // test 1. Mirrors spa_routes.py:39-44 — prefix ownership with "/" owning
  // only itself.
  const owned = (p: string) =>
    SPA_ROUTES.some((r) => (r === "/" ? p === "/" : p === r || p.startsWith(r + "/")));
  const unowned = leafPaths(simpleRouter.routes as RouteObject[])
    .filter((p) => p !== "/*")
    .filter((p) => !owned(p));
  expect(unowned).toEqual([]);
});

// --- the root errorElement (review 2026-10-26 M-fe-2) ----------------------
//
// The match edition needs its own copy: the two routers share no structure —
// three sibling pathless layout routes each, built independently, with
// different shells — so an errorElement added to one is simply absent from the
// other, and the match edition is the one that ships.
it("every top-level route of the match router declares the RouteError errorElement", () => {
  const tops = simpleRouter.routes as RouteObject[];
  expect(tops.length).toBeGreaterThan(1);
  for (const r of tops) {
    expect((r.errorElement as ReactElement | undefined)?.type).toBe(RouteError);
  }
});

it("a lazy chunk that 404s renders RouteError with a Reload button, not react-router's screen", async () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  const r = createMemoryRouter(simpleRouter.routes as RouteObject[], {
    initialEntries: ["/admin"],
  });
  render(
    <MantineProvider theme={theme} defaultColorScheme="light">
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
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
