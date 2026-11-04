import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it } from "vitest";

import { AdminNav } from "./AdminNav";

// The sections router.tsx mounts under /admin, in LINKS order. Written out rather
// than imported from AdminNav's LINKS, which would assert a relation with itself.
const MOUNTED_SECTIONS = ["/admin", "/admin/golden-set", "/admin/users", "/admin/users/pending"];

function hrefs() {
  return screen.getAllByRole("link").map((a) => a.getAttribute("href") ?? "");
}

it("renders exactly the four admin sections, in order", () => {
  render(<MemoryRouter><AdminNav /></MemoryRouter>);
  expect(hrefs()).toEqual(MOUNTED_SECTIONS); // equality, not "none of": an empty nav must not pass
  for (const label of ["Operational health", "Golden set", "Users", "Pending"]) {
    expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
  }
  for (const label of ["Drafting", "Deleted workspaces"]) {
    expect(screen.queryByRole("link", { name: label })).not.toBeInTheDocument();
  }
});

// The coherence gate: every link AdminNav renders is a route the router mounts.
it("every admin link is a route the router mounts", async () => {
  const { router } = await import("../router");
  const { leafPaths } = await import("../routeLeafPaths");
  const mounted = new Set(leafPaths(router.routes as never));
  render(
    <MemoryRouter initialEntries={["/admin"]}>
      <AdminNav />
    </MemoryRouter>,
  );
  const links = hrefs();
  expect(links.length).toBeGreaterThan(0);
  expect(links.filter((h) => !mounted.has(h))).toEqual([]);
  // The dynamic import counts against the timeout: 1.5 s alone, over 5 s in a loaded run (2026-10-05).
}, 20_000);
