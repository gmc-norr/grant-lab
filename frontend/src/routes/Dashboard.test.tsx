import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { Dashboard } from "./Dashboard";

// Surfaces the live URL search so a test can assert WHEN (not just whether) it changes.
function LocationProbe() {
  const loc = useLocation();
  return <output data-testid="search">{loc.search}</output>;
}

function dashData(effectiveProfileId: number | null) {
  return {
    data: {
      items: [],
      summary: { open_count: 0, high_score: 0, soon_deadline: 0 },
      pagination: { page: 1, limit: 50, has_prev: false, has_next: false, total: 0 },
      ranking: { pending: 0, failed: 0 },
      effective: { profile_id: effectiveProfileId },
      facets: {
        sources: [],
        profiles: [
          { id: 7, name: "G" },
          { id: 9, name: "H" },
        ],
      },
    },
    error: undefined,
    response: new Response(),
  };
}

function renderAt(url: string) {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>
        <Dashboard />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

it("deep link is read-only: the select reflects the SERVER-resolved profile, no POST on load", async () => {
  // Bare load whose stored default resolves to profile 7 server-side (effective.profile_id).
  const GET = vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData(7) as never);
  const POST = vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { ok: true },
    error: undefined,
    response: new Response(),
  } as never);

  renderAt("/");
  // Select shows the RESOLVED id (7) — Jinja parity — NOT the (absent) URL param.
  const select = (await screen.findByLabelText(/profile/i)) as HTMLSelectElement;
  expect(select.value).toBe("7");
  await waitFor(() => expect(GET).toHaveBeenCalled());
  expect(POST).not.toHaveBeenCalled(); // a load/deep-link never persists a default
});

it("selecting a profile persists FIRST, then reflects the URL only after the POST resolves", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData(null) as never);
  let resolvePost: (v: unknown) => void = () => {};
  const POST = vi
    .spyOn(clientMod.api, "POST")
    .mockReturnValue(new Promise((res) => (resolvePost = res)) as never);

  renderAt("/");
  const select = (await screen.findByLabelText(/profile/i)) as HTMLSelectElement;
  expect(select.value).toBe(""); // no resolved profile yet
  fireEvent.change(select, { target: { value: "9" } }); // a REAL change (differs from "")

  await waitFor(() =>
    expect(POST).toHaveBeenCalledWith("/api/me/default-profile", { body: { profile_id: 9 } }),
  );
  // URL must NOT update until the POST resolves (persist-first contract).
  expect(screen.getByTestId("search").textContent).not.toContain("profile=9");
  resolvePost({ data: { ok: true }, error: undefined, response: new Response() });
  await waitFor(() =>
    expect(screen.getByTestId("search").textContent).toContain("profile=9"),
  );
});

it("a failed persist leaves the URL unchanged and surfaces an error", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData(null) as never);
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: undefined,
    error: { error: { code: "x", message: "no" } },
    response: new Response(),
  } as never);

  renderAt("/");
  const select = (await screen.findByLabelText(/profile/i)) as HTMLSelectElement;
  fireEvent.change(select, { target: { value: "9" } });

  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/default profile/i));
  expect(screen.getByTestId("search").textContent).not.toContain("profile=9");
});

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

it("the show_closed checkbox reflects the URL: checked with ?show_closed=1", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(itemsBody([]) as never);
  renderAt("/?show_closed=1");
  expect(await screen.findByRole("checkbox")).toBeChecked();
});

it("the show_closed checkbox is unchecked by default", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(itemsBody([]) as never);
  renderAt("/");
  expect(await screen.findByRole("checkbox")).not.toBeChecked();
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
