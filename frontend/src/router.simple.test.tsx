import type { RouteObject } from "react-router-dom";
import { expect, it } from "vitest";

import { leafPaths } from "./routeLeafPaths";
import { simpleRouter } from "./router.simple";
import { SPA_ROUTES } from "./routes.gen";

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
  "/b/:slug",
  "/login",
  "/register",
  "/auth/pending",
  "/auth/no-access",
  "/auth/error",
];

// Prefixes the match edition must never mount. Spec §3 "explicitly out of
// release 1".
const EXCLUDED_PREFIXES = [
  "/cockpit",
  "/admin/drafting",
  "/admin/golden-set",
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
