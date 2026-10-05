import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { render } from "@testing-library/react";
import { AdminNav } from "./AdminNav";

// AdminNav filters two destinations out in the match edition, because
// router.simple.tsx does not mount /admin/drafting or /admin/deleted-workspaces.
// It was three until 2026-09-07, when the router began mounting
// /admin/golden-set — the ranking system's own evaluation surface, in the only
// edition that ships. noCockpitLinks.test.tsx EXEMPTs AdminNav.tsx — a source
// scan cannot tell a gated string literal from an ungated one — so THESE TESTS
// ARE THE ONLY THING PINNING THAT GATING. Both branches are covered on purpose:
// the match case would pass on a component that deleted the two links outright,
// and the full case is what catches that.
//
// isMatchEdition is a BUILD-TIME constant (edition.ts reads import.meta.env at
// module scope) and vitest pins no `test.env`, so the real value is whatever
// VITE_APP_EDITION the developer's shell carries — a test that read it would
// pass or fail by environment. So the module is mocked.
//
// The getter is load-bearing: AdminNav reads isMatchEdition during render and
// vitest's SSR transform rewrites that read to a namespace property access, so
// flipping `edition.isMatch` between tests is observed. A plain value would be
// frozen at import time and the match test would silently run the full branch.
const edition = vi.hoisted(() => ({ isMatch: false }));
vi.mock("../edition", () => ({
  get isMatchEdition() {
    return edition.isMatch;
  },
  get APP_EDITION() {
    return edition.isMatch ? "match" : "full";
  },
  resolveEdition: (raw: string | undefined) => (raw === "match" ? "match" : "full"),
}));

afterEach(() => {
  edition.isMatch = false;
});

// Written out rather than imported from AdminNav's own MATCH_EXCLUDED: importing
// it would assert a relation between the component and itself, and emptying that
// constant would keep this file green. Same reason router.simple.test.tsx writes
// its route allowlist out by hand.
const MATCH_HIDDEN = ["/admin/drafting", "/admin/deleted-workspaces"];
// In LINKS order, which is where golden-set sits (second, ahead of Users) — not
// the order that reads nicely. The assertion below is exact equality, so the
// order is part of the claim.
const RELEASE_1_VISIBLE = [
  "/admin",
  "/admin/golden-set",
  "/admin/users",
  "/admin/users/pending",
];

function hrefs() {
  return screen.getAllByRole("link").map((a) => a.getAttribute("href") ?? "");
}

it("renders all admin section links", () => {
  render(<MemoryRouter><AdminNav /></MemoryRouter>);
  for (const label of ["Operational health", "Drafting", "Golden set", "Users", "Pending"]) {
    expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
  }
  expect(screen.getByRole("link", { name: "Operational health" })).toHaveAttribute("href", "/admin");
  expect(screen.getByRole("link", { name: "Golden set" })).toHaveAttribute("href", "/admin/golden-set");
});

it("the FULL edition renders all six destinations, in order", () => {
  // The control for the match test below. Without it, a filter that dropped the
  // gated links unconditionally — i.e. broke the full product — would still leave
  // the match test green. All six still render here: /admin/golden-set being
  // mounted in match too does not remove it from the full edition.
  edition.isMatch = false;
  render(<MemoryRouter><AdminNav /></MemoryRouter>);
  expect(hrefs()).toEqual([
    "/admin",
    "/admin/drafting",
    "/admin/golden-set",
    "/admin/users",
    "/admin/users/pending",
    "/admin/deleted-workspaces",
  ]);
});

it("the MATCH edition omits every destination the simple router does not mount", () => {
  edition.isMatch = true;
  render(<MemoryRouter><AdminNav /></MemoryRouter>);
  const rendered = hrefs();
  // Exact equality, not a "none of" check: a "none of" assertion passes on a nav
  // that renders nothing at all, which is a different bug with the same symptom.
  expect(rendered).toEqual(RELEASE_1_VISIBLE);
  for (const gone of MATCH_HIDDEN) expect(rendered).not.toContain(gone);
  for (const label of ["Drafting", "Deleted workspaces"]) {
    expect(screen.queryByRole("link", { name: label })).not.toBeInTheDocument();
  }
  // ...and the ones release 1 DOES mount are still reachable. "Golden set" is
  // here rather than in the absent list as of 2026-09-07: a nav that kept
  // filtering it would render a route the match router now mounts as a dead
  // section, and the equality above alone would not say which link went missing.
  for (const label of ["Operational health", "Golden set", "Users", "Pending"]) {
    expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
  }
});

// The coherence gate. Three hand-maintained copies of the release-1 admin route
// set exist — router.simple.tsx's mounted routes, AdminNav's MATCH_EXCLUDED, and
// noCockpitLinks.test.tsx's EXCLUDED_PREFIXES — and nothing asserted they agree.
// This is the case that couples the two halves of the 2026-09-07 golden-set
// move: lifting the nav exclusion without mounting the route reddens HERE.
// The concrete failure that leaves open: if router.simple.tsx later stops
// mounting /admin/users/pending, AdminNav keeps linking to it in the match
// edition, the link resolves to the in-app soft 404, and every gate stays green
// because each one is individually consistent.
//
// This walks the actual match router rather than any copy of the list.
it("every admin link the match edition renders is a route the match router mounts", async () => {
  edition.isMatch = true;
  const { simpleRouter } = await import("../router.simple");
  const { leafPaths } = await import("../routeLeafPaths");
  const mounted = new Set(leafPaths(simpleRouter.routes as never));

  render(
    <MemoryRouter initialEntries={["/admin"]}>
      <AdminNav />
    </MemoryRouter>,
  );
  const hrefs = screen
    .getAllByRole("link")
    .map((a) => a.getAttribute("href"))
    .filter((h): h is string => h !== null);

  expect(hrefs.length).toBeGreaterThan(0); // a nav rendering nothing must not pass
  expect(hrefs.filter((h) => !mounted.has(h))).toEqual([]);
  // The dynamic import of the whole match route table counts against this
  // test's timeout (a static import would not). 1.5 s alone, but over the 5 s
  // default in a loaded full run (2026-10-05, failed on 01502d7b and after).
}, 20_000);
