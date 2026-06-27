import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation } from "react-router-dom";
import { expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { CallsFilters } from "./CallsFilters";

function LocationProbe() {
  const loc = useLocation();
  return <output data-testid="search">{loc.search}</output>;
}

function dashData(over: { profile_id?: number | null; source?: string | null } = {}) {
  return {
    data: {
      items: [],
      summary: { open_count: 0, high_score: 0, soon_deadline: 0 },
      pagination: { page: 1, limit: 50, has_prev: false, has_next: false, total: 0 },
      ranking: { pending: 0, failed: 0 },
      effective: {
        source: over.source ?? null,
        sort: "newest",
        profile_id: over.profile_id ?? null,
        show_closed: false,
      },
      facets: {
        sources: [{ slug: "vinnova", name: "Vinnova" }],
        profiles: [
          { id: 7, name: "Group G" },
          { id: 9, name: "Group H" },
        ],
      },
    },
    error: undefined,
    response: new Response(),
  };
}

it("reflects the server-resolved profile (Jinja parity), not the URL param", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData({ profile_id: 7 }) as never);
  renderWithProviders(<CallsFilters />, { route: "/" });
  const profile = (await screen.findByLabelText("Profile", { selector: "input" })) as HTMLInputElement;
  await waitFor(() => expect(profile).toHaveValue("Group G"));
});

it("selecting a Source writes ?source= and resets the page", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData() as never);
  // pointerEventsCheck: 0 — jsdom does not compute CSS, so Mantine's combobox
  // wrapper gets a false "pointer-events: none" reading; disabling the check
  // lets userEvent fire the click that opens the dropdown.
  const user = userEvent.setup({ pointerEventsCheck: 0 });
  renderWithProviders(
    <>
      <CallsFilters />
      <LocationProbe />
    </>,
    { route: "/?page=3" },
  );
  await user.click(await screen.findByLabelText("Source", { selector: "input" }));
  await user.click(await screen.findByRole("option", { name: "Vinnova", hidden: true }));
  await waitFor(() => {
    const s = screen.getByTestId("search").textContent ?? "";
    expect(s).toContain("source=vinnova");
    expect(s).not.toContain("page=3");
  });
});

it("selecting a Profile persists FIRST: the URL updates only after the POST resolves", async () => {
  // Hold the POST unresolved so the test PROVES the URL doesn't change before
  // persistence (a resolved-immediately mock would pass even if order were wrong).
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData() as never);
  let resolvePost: (v: unknown) => void = () => {};
  const POST = vi
    .spyOn(clientMod.api, "POST")
    .mockReturnValue(new Promise((res) => (resolvePost = res)) as never);
  const user = userEvent.setup({ pointerEventsCheck: 0 });
  renderWithProviders(
    <>
      <CallsFilters />
      <LocationProbe />
    </>,
    { route: "/" },
  );
  await user.click(await screen.findByLabelText("Profile", { selector: "input" }));
  await user.click(await screen.findByRole("option", { name: "Group H", hidden: true }));
  await waitFor(() =>
    expect(POST).toHaveBeenCalledWith("/api/me/default-profile", { body: { profile_id: 9 } }),
  );
  // URL must NOT reflect the selection until the POST resolves (persist-first).
  expect(screen.getByTestId("search").textContent).not.toContain("profile=9");
  resolvePost({ data: { ok: true }, error: undefined, response: new Response() });
  await waitFor(() => expect(screen.getByTestId("search").textContent).toContain("profile=9"));
});

it("a failed persist leaves the URL unchanged and surfaces an error", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData() as never);
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: undefined,
    error: { error: { code: "x", message: "no" } },
    response: new Response(),
  } as never);
  const user = userEvent.setup({ pointerEventsCheck: 0 });
  renderWithProviders(
    <>
      <CallsFilters />
      <LocationProbe />
    </>,
    { route: "/" },
  );
  await user.click(await screen.findByLabelText("Profile", { selector: "input" }));
  await user.click(await screen.findByRole("option", { name: "Group H", hidden: true }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/default profile/i));
  expect(screen.getByTestId("search").textContent).not.toContain("profile=9");
});

it("renders without crashing while the query is loading (facet selects disabled)", async () => {
  // GET never resolves -> data stays undefined (the first-load case).
  vi.spyOn(clientMod.api, "GET").mockReturnValue(new Promise(() => {}) as never);
  renderWithProviders(<CallsFilters />, { route: "/" });
  expect((await screen.findByLabelText("Profile", { selector: "input" })) as HTMLInputElement).toBeDisabled();
  expect((screen.getByLabelText("Source", { selector: "input" })) as HTMLInputElement).toBeDisabled();
  // Sort + Show-closed bind to the URL and stay usable.
  expect(screen.getByLabelText("Sort", { selector: "input" })).not.toBeDisabled();
  expect(screen.getByLabelText("Show closed")).toBeInTheDocument();
});

it("typing in Search writes a debounced ?q= and resets the page", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData({ profile_id: 7 }) as never);
  const user = userEvent.setup({ pointerEventsCheck: 0 });
  renderWithProviders(
    <>
      <CallsFilters />
      <LocationProbe />
    </>,
    { route: "/?page=3" },
  );
  await user.type(await screen.findByLabelText("Search"), "onco");
  await waitFor(() => {
    const s = screen.getByTestId("search").textContent ?? "";
    expect(s).toContain("q=onco");
    expect(s).not.toContain("page=3");
  });
});

it("seeds the Search box from the URL ?q=", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData({ profile_id: 7 }) as never);
  renderWithProviders(<CallsFilters />, { route: "/?q=onco" });
  expect(await screen.findByLabelText("Search")).toHaveValue("onco");
});

it("the score floor is disabled without a profile and enabled with one", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData({ profile_id: null }) as never);
  const { unmount } = renderWithProviders(<CallsFilters />, { route: "/" });
  expect((await screen.findByLabelText("Min score", { selector: "input" })) as HTMLInputElement).toBeDisabled();
  unmount();
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData({ profile_id: 7 }) as never);
  renderWithProviders(<CallsFilters />, { route: "/" });
  expect((await screen.findByLabelText("Min score", { selector: "input" })) as HTMLInputElement).not.toBeDisabled();
});

it("selecting a score band writes ?min_score=", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData({ profile_id: 7 }) as never);
  const user = userEvent.setup({ pointerEventsCheck: 0 });
  renderWithProviders(
    <>
      <CallsFilters />
      <LocationProbe />
    </>,
    { route: "/" },
  );
  await user.click(await screen.findByLabelText("Min score", { selector: "input" }));
  await user.click(await screen.findByRole("option", { name: "60+ (Good)", hidden: true }));
  await waitFor(() => expect(screen.getByTestId("search").textContent).toContain("min_score=60"));
});

it("renders without crashing when /api/calls errors before any data exists", async () => {
  // No-prior-data error path (the other half of the loading/error contract).
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: undefined,
    error: { error: { code: "x", message: "boom" } },
    response: new Response(null, { status: 500 }),
  } as never);
  renderWithProviders(<CallsFilters />, { route: "/" });
  expect((await screen.findByLabelText("Profile", { selector: "input" })) as HTMLInputElement).toBeDisabled();
  expect(screen.getByLabelText("Sort", { selector: "input" })).not.toBeDisabled();
  expect(screen.getByLabelText("Show closed")).toBeInTheDocument();
});
