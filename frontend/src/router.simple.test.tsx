import type { RouteObject } from "react-router-dom";
import { expect, it } from "vitest";

import { simpleRouter } from "./router.simple";
import { SPA_ROUTES } from "./routes.gen";

// Same leaf walk as router.test.tsx: a pathless layout contributes nothing
// itself, its children inherit the parent base, an index route resolves to the
// parent base.
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
  "/admin",
  "/admin/users",
  "/admin/users/pending",
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
  const concrete = leafPaths(simpleRouter.routes as RouteObject[]).filter(
    (p) => !p.includes("*"),
  );
  expect(new Set(concrete)).toEqual(new Set(RELEASE_1_ROUTES));
});

it("the simple router mounts no excluded path", () => {
  const concrete = leafPaths(simpleRouter.routes as RouteObject[]);
  const leaked = concrete.filter((p) =>
    EXCLUDED_PREFIXES.some((x) => p === x || p.startsWith(x + "/")),
  );
  expect(leaked).toEqual([]);
});

it("every release-1 route is owned by the shared SPA manifest", () => {
  // The server decides which paths get index.html from routes.gen.ts. A
  // release-1 route the manifest does not own would 404 on a hard refresh.
  const owned = (p: string) =>
    SPA_ROUTES.some((r) => (r === "/" ? p === "/" : p === r || p.startsWith(r + "/")));
  const unowned = RELEASE_1_ROUTES.filter((p) => !owned(p));
  expect(unowned).toEqual([]);
});
