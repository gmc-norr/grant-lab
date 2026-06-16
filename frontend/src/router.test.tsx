import { expect, it } from "vitest";

import { router } from "./router";
import { SPA_ROUTES } from "./routes.gen";

// router.tsx is the 5th consumer of the route-ownership manifest (alongside
// spa_routes.py, routes.gen.ts, dev-proxy.ts, spa_serving.py) but is the only
// HAND-maintained one — the gen_routes CI drift gate doesn't cover it. This
// pins it: the router's CONCRETE page paths (excluding the in-app "*" soft-404)
// must equal SPA_ROUTES, so a manifest change that isn't mirrored here fails CI.
it("router concrete paths match the SPA_ROUTES manifest (drift guard)", () => {
  const concrete = router.routes
    .map((r) => r.path)
    .filter((p): p is string => !!p && p !== "*");
  expect(new Set(concrete)).toEqual(new Set(SPA_ROUTES));
});
