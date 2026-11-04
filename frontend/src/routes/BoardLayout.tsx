import { Outlet } from "react-router-dom";

// THIRD top-level pathless layout, sibling to SimpleShell and PreAuthLayout
// (router.tsx). Spec §6 asked for this page "under PreAuthLayout" — but that
// layout is a centred 352px card (`maw="22rem"`, PreAuthLayout.tsx:17) with a
// BrandMark, and eight 170px QR cards do not fit inside it. §6's actual
// requirement is "auth-free, no new auth architecture", which this layout
// satisfies the same way PreAuthLayout does: a second pathless route with no
// IDE shell. Only the layout's NAME is dropped (decisions note, "One spec
// instruction that is not implementable as written").
//
// Full-bleed, no shell, NO useSession: this is an unattended corridor screen
// nobody is standing at to dismiss a redirect, so it must never make an
// authenticated /api call (the same anti-401-loop invariant PreAuthLayout
// documents). The public board fetch (api/publicBoard.ts) reads
// `response.status` directly instead of going through `unwrap()`, which is
// the load-bearing reason this layout carries no auth of any kind either.
export function BoardLayout() {
  return <Outlet />;
}
