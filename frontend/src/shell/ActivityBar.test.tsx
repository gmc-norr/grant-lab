import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useLocation } from "react-router-dom";

import * as clientMod from "../api/client";

import { renderWithProviders } from "../test-utils";
import { ActivityBar } from "./ActivityBar";

it("Calls is an SPA link to / and is active on the dashboard route", () => {
  renderWithProviders(<ActivityBar />, { route: "/" });
  const calls = screen.getByRole("link", { name: "Calls" });
  expect(calls).toHaveAttribute("href", "/");
  // NavLink sets aria-current="page" when active (robust; no reliance on
  // hashed CSS-module class names).
  expect(calls).toHaveAttribute("aria-current", "page");
});

it("Workspaces is an SPA link to the cockpit front door", () => {
  renderWithProviders(<ActivityBar />, { route: "/" });
  expect(screen.getByRole("link", { name: "Workspaces" })).toHaveAttribute("href", "/cockpit");
});

it("Profiles and Settings route in-app (SPA nav links)", () => {
  renderWithProviders(<ActivityBar />, { route: "/" });
  expect(screen.getByRole("link", { name: "Profiles" })).toHaveAttribute("href", "/profiles");
  expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/me");
});

it("Workspaces is the active nav item on the cockpit list and create routes", () => {
  renderWithProviders(<ActivityBar />, { route: "/cockpit" });
  expect(screen.getByRole("link", { name: "Workspaces" })).toHaveAttribute("aria-current", "page");
});

it("on the editor route the Current-workspace chip is active, Workspaces is not", () => {
  renderWithProviders(<ActivityBar />, { route: "/cockpit/abc" });
  expect(screen.getByRole("link", { name: "Current workspace" })).toHaveAttribute("aria-current", "page");
  expect(screen.getByRole("link", { name: "Workspaces" })).not.toHaveAttribute("aria-current", "page");
});

it("hides the current-workspace item outside a cockpit", () => {
  renderWithProviders(<ActivityBar />, { route: "/" });
  expect(screen.queryByRole("link", { name: "Current workspace" })).toBeNull();
});

// Separate test: a second render in the same test would leave two ActivityBars
// mounted (RTL only cleans up between tests), so `getByRole("link", { name:
// "Calls" })` would throw on the duplicate. Each test does a single render.
it("shows the current-workspace item inside a cockpit and deactivates Calls", () => {
  renderWithProviders(<ActivityBar />, { route: "/cockpit/abc" });
  const ws = screen.getByRole("link", { name: "Current workspace" });
  expect(ws).toHaveAttribute("href", "/cockpit/abc");
  // Calls must not be active while in a cockpit.
  expect(screen.getByRole("link", { name: "Calls" })).not.toHaveAttribute(
    "aria-current",
    "page",
  );
});

it("has no References destination", () => {
  renderWithProviders(<ActivityBar />, { route: "/" });
  expect(screen.queryByRole("link", { name: /references/i })).toBeNull();
});

it("hides the current-workspace item on the cockpit list route", () => {
  renderWithProviders(<ActivityBar />, { route: "/cockpit" });
  expect(screen.queryByRole("link", { name: "Current workspace" })).toBeNull();
});

it("hides the current-workspace item on the create route", () => {
  renderWithProviders(<ActivityBar />, { route: "/cockpit/new" });
  expect(screen.queryByRole("link", { name: "Current workspace" })).toBeNull();
});

afterEach(() => {
  vi.restoreAllMocks();
});

function mockMe(admin: boolean) {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { user: { id: 1, email: "a@ki.se", status: "active", default_profile_id: null }, capabilities: { admin } },
    error: undefined,
  } as never);
}

it("shows the Admin entry for an admin", async () => {
  mockMe(true);
  renderWithProviders(<ActivityBar />);
  await waitFor(() => expect(screen.getByRole("link", { name: "Admin" })).toBeInTheDocument());
});

it("hides the Admin entry for a non-admin", async () => {
  mockMe(false);
  renderWithProviders(<ActivityBar />);
  // Settings always renders; wait for it, then assert Admin is absent.
  await waitFor(() => expect(screen.getByRole("link", { name: "Settings" })).toBeInTheDocument());
  expect(screen.queryByRole("link", { name: "Admin" })).not.toBeInTheDocument();
});

function LocationEcho() {
  const loc = useLocation();
  return <output data-testid="loc">{loc.pathname + loc.search}</output>;
}

it("active nav item toggles the sidebar instead of navigating (preserves query state)", () => {
  const onToggleSidebar = vi.fn();
  renderWithProviders(
    <>
      <ActivityBar onToggleSidebar={onToggleSidebar} />
      <LocationEcho />
    </>,
    { route: "/cockpit/abc?tab=spec&sec=2" },
  );
  // "Current workspace" is the active item on the editor route.
  fireEvent.click(screen.getByRole("link", { name: "Current workspace" }));
  expect(onToggleSidebar).toHaveBeenCalledTimes(1);
  // Navigation was prevented → the ?tab/?sec query is NOT dropped.
  expect(screen.getByTestId("loc").textContent).toBe("/cockpit/abc?tab=spec&sec=2");
});

it("an inactive nav item still navigates and does not toggle the sidebar", () => {
  const onToggleSidebar = vi.fn();
  renderWithProviders(
    <>
      <ActivityBar onToggleSidebar={onToggleSidebar} />
      <LocationEcho />
    </>,
    { route: "/" },
  );
  // On "/", Workspaces is INACTIVE → clicking it navigates to /cockpit.
  fireEvent.click(screen.getByRole("link", { name: "Workspaces" }));
  expect(onToggleSidebar).not.toHaveBeenCalled();
  expect(screen.getByTestId("loc").textContent).toBe("/cockpit");
});
