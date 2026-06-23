import type { RouteObject } from "react-router-dom";
import { expect, it } from "vitest";

import { router } from "./router";
import { SPA_ROUTES } from "./routes.gen";

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
// SPA_ROUTES, reducing each ":param" path to its static ownership prefix
// ("/cockpit/:id" -> "/cockpit") the way is_spa_route matches subtrees.
it("router concrete paths match the SPA_ROUTES manifest (drift guard)", () => {
  const concrete = leafPaths(router.routes as RouteObject[])
    .filter((p) => !p.includes("*"))
    .map((p) => {
      const i = p.indexOf("/:");
      return i === -1 ? p : p.slice(0, i) || "/";
    });
  expect(new Set(concrete)).toEqual(new Set(SPA_ROUTES));
});
