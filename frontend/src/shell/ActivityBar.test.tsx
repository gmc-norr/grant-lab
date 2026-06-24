import { screen } from "@testing-library/react";
import { expect, it } from "vitest";

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

it("Workspaces, Profiles and Settings deep-link to their Jinja pages", () => {
  renderWithProviders(<ActivityBar />, { route: "/" });
  expect(screen.getByRole("link", { name: "Workspaces" })).toHaveAttribute(
    "href",
    "/workspaces",
  );
  expect(screen.getByRole("link", { name: "Profiles" })).toHaveAttribute(
    "href",
    "/profiles",
  );
  expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute(
    "href",
    "/me",
  );
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
