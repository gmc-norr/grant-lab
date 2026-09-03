import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { render } from "@testing-library/react";
import { AdminNav } from "./AdminNav";

// AdminNav filters three destinations out in the match edition, because
// router.simple.tsx does not mount /admin/drafting, /admin/golden-set or
// /admin/deleted-workspaces. noCockpitLinks.test.tsx EXEMPTs AdminNav.tsx — a
// source scan cannot tell a gated string literal from an ungated one — so THESE
// TESTS ARE THE ONLY THING PINNING THAT GATING. Both branches are covered on
// purpose: the match case would pass on a component that deleted the three links
// outright, and the full case is what catches that.
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
const MATCH_HIDDEN = ["/admin/drafting", "/admin/golden-set", "/admin/deleted-workspaces"];
const RELEASE_1_VISIBLE = ["/admin", "/admin/users", "/admin/users/pending"];

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
  // three links unconditionally — i.e. broke the full product — would still leave
  // the match test green.
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
  for (const label of ["Drafting", "Golden set", "Deleted workspaces"]) {
    expect(screen.queryByRole("link", { name: label })).not.toBeInTheDocument();
  }
  // ...and the ones release 1 DOES mount are still reachable.
  for (const label of ["Operational health", "Users", "Pending"]) {
    expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
  }
});
