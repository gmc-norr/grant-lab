import type { RouteObject } from "react-router-dom";

// Collect every concrete LEAF page path a router can match, as an absolute
// path. A pathless layout route (no `path`) contributes nothing itself; its
// children inherit the parent base. An index route resolves to the parent base.
//
// Shared by router.test.tsx (full edition) and router.simple.test.tsx (match
// edition) — it is the foundation both drift guards stand on, and two
// byte-identical copies would drift independently. A fix to the walk must
// apply to both gates or one of them silently stops meaning what it says.
export function leafPaths(routes: RouteObject[], base = ""): string[] {
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
