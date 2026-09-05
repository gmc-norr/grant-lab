import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { Dashboard } from "./Dashboard";

// CallRow's Start-drafting link is edition-gated and isMatchEdition is a
// BUILD-TIME constant, so a test that read the real value would pass or fail by
// environment. Mutable holder + GETTER, the pattern from AdminNav.test.tsx:25-34.
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

// Surfaces the live URL search so a test can assert WHEN (not just whether) it changes.
function LocationProbe() {
  const loc = useLocation();
  return <output data-testid="search">{loc.search}</output>;
}

function renderAt(url: string) {
  const qc = new QueryClient();
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[url]}>
          <Dashboard />
          <LocationProbe />
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

// --- row rendering + filter-state reflection (ported from the retired Jinja
// GET-/ tests: deadline label, show_closed checkbox state, pagination preserves
// the toggle). These need non-empty items, so they use a fuller body fixture. ---

const isoIn = (days: number) => new Date(Date.now() + days * 86_400_000).toISOString();

function itemsBody(items: object[], extra: object = {}) {
  return {
    data: {
      items,
      summary: { open_count: 0, high_score: 0, soon_deadline: 0 },
      pagination: { page: 1, limit: 50, has_prev: false, has_next: false, total: items.length },
      ranking: { pending: 0, failed: 0 },
      effective: { profile_id: null, source: null, sort: "newest", show_closed: false },
      facets: { sources: [], profiles: [] },
      ...extra,
    },
    error: undefined,
    response: new Response(),
  };
}

const row = (id: number, title: string, deadline: string | null) => ({
  id,
  title,
  deadline,
  published_at: null,
  source_name: null,
  source_slug: null,
  aggregate_score: null,
});

it("renders a DISTINCT deadline affordance: past -> grey 'Closed', near -> urgent (Jinja-label parity)", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(
    itemsBody([row(1, "Past call", isoIn(-5)), row(2, "Soon call", isoIn(5))]) as never,
  );
  renderAt("/?show_closed=1");
  await screen.findByText("Past call"); // wait for the rows to render
  // Past deadline -> grey "Closed · <date>" state.
  const closed = document.querySelector('[data-deadline-state="closed"]');
  expect(closed).not.toBeNull();
  expect(closed?.textContent).toMatch(/^Closed ·/);
  // Near deadline -> a DISTINCT urgent state (not "closed", no "Closed" prefix).
  const soon = document.querySelector('[data-deadline-state="soon"]');
  expect(soon).not.toBeNull();
  expect(soon?.textContent ?? "").not.toMatch(/Closed/);
});

it("paginating preserves the show_closed toggle in the URL", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(
    itemsBody([], {
      pagination: { page: 1, limit: 50, has_prev: false, has_next: true, total: 100 },
    }) as never,
  );
  renderAt("/?show_closed=1");
  fireEvent.click(await screen.findByRole("button", { name: /next/i }));
  await waitFor(() => {
    const s = screen.getByTestId("search").textContent ?? "";
    expect(s).toContain("show_closed=1"); // toggle carried onto the next page
    expect(s).toContain("page=2");
  });
});

it("threads the dashboard's effective profile into BOTH row links in the full edition", async () => {
  // Profile threading is real behaviour on two links at once: the title anchor
  // pins ?profile= for the reading pane, and Start drafting pins profile_id= for
  // WorkspaceCreate. Asserting one would drop coverage of the other.
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(
    itemsBody([row(1, "Call one", null)], {
      effective: { profile_id: 5, source: null, sort: "newest", show_closed: false },
    }) as never,
  );
  renderAt("/");
  const title = await screen.findByRole("link", { name: "Call one" });
  expect(title.getAttribute("href")).toContain("profile=5");
  const start = screen.getByRole("link", { name: /start drafting/i });
  expect(start.getAttribute("href") ?? "").toContain("/cockpit/new?");
  expect(start.getAttribute("href") ?? "").toContain("profile_id=5");
});

it("under the match edition flag Dashboard threads the profile and emits no /cockpit link", async () => {
  // NOT a statement about the release-1 landing page any more. Task 7 mounted
  // TriageFeed at "/" in router.simple.tsx, so `Dashboard` is the FULL edition's
  // list and is not in the match bundle at all — check_match_bundle.mjs is what
  // pins that. What survives here is narrower and still worth holding: with
  // `isMatch` true this component drops the Start-drafting link and emits no
  // href into the excluded half, so the flag it still reads keeps working. The
  // whole-page check for the page that IS the landing page lives in
  // e2e/match.spec.ts, against the bundle that ships.
  edition.isMatch = true;
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(
    itemsBody([row(1, "Call one", null)], {
      effective: { profile_id: 5, source: null, sort: "newest", show_closed: false },
    }) as never,
  );
  renderAt("/");
  const title = await screen.findByRole("link", { name: "Call one" });
  expect(title.getAttribute("href")).toContain("profile=5");
  expect(screen.queryByRole("link", { name: /start drafting/i })).not.toBeInTheDocument();
  for (const a of screen.getAllByRole("link")) {
    expect(a.getAttribute("href") ?? "").not.toContain("/cockpit");
  }
});

it("a 401 (session expired) redirects to /login instead of dead-ending on an error", async () => {
  // Replace window.location so the full-page nav is observable (jsdom's
  // assign is a warning no-op).
  const orig = window.location;
  const assign = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...orig, pathname: "/", search: "", assign },
  });
  try {
    vi.spyOn(clientMod.api, "GET").mockResolvedValue({
      data: undefined,
      error: { error: { code: "unauthorized", message: "no" } },
      response: new Response(null, { status: 401 }),
    } as never);
    renderAt("/");
    // unwrap sees the 401 and navigates to /login (with a next), rather than
    // letting the dashboard render its generic "Couldn't load calls." dead-end.
    await waitFor(() => expect(assign).toHaveBeenCalled());
    expect(assign.mock.calls[0][0]).toContain("/login");
  } finally {
    Object.defineProperty(window, "location", { configurable: true, value: orig });
  }
});
