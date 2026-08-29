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

it("on the editor route Workspaces is NOT active — the band says where you are", () => {
  renderWithProviders(<ActivityBar />, { route: "/cockpit/abc" });
  expect(screen.getByRole("link", { name: "Workspaces" })).not.toHaveAttribute(
    "aria-current",
    "page",
  );
  expect(screen.getByRole("link", { name: "Versions" })).toBeInTheDocument();
});

it("inside a cockpit the band is present and Calls is not active", () => {
  renderWithProviders(<ActivityBar />, { route: "/cockpit/abc" });
  expect(screen.getByRole("link", { name: "Outline" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Calls" })).not.toHaveAttribute(
    "aria-current",
    "page",
  );
});

it("References is a per-workspace view, never a top-level destination", () => {
  renderWithProviders(<ActivityBar />, { route: "/" });
  expect(screen.queryByRole("link", { name: /references/i })).toBeNull();
});

it("shows no workspace band on the create route", () => {
  // All five and the divider, for the same reason as the /cockpit twin below:
  // asserting one label passes on an implementation that renders the other four.
  renderWithProviders(<ActivityBar />, { route: "/cockpit/new" });
  for (const label of ["Outline", "Versions", "References", "Criteria", "Members"]) {
    expect(screen.queryByRole("link", { name: label })).toBeNull();
  }
  expect(screen.queryByTestId("activity-divider")).toBeNull();
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

it("clicking the ACTIVE workspace icon toggles the pane and keeps the query", () => {
  // VS Code behaviour, previously carried by the Current-workspace item. Without
  // it, the only way to collapse the pane from the rail disappears with that
  // item — and a plain navigation would drop ?tab and ?sec.
  const onToggleSidebar = vi.fn();
  renderWithProviders(
    <>
      <ActivityBar onToggleSidebar={onToggleSidebar} />
      <LocationEcho />
    </>,
    { route: "/cockpit/abc?tab=spec&sec=2&view=versions" },
  );
  fireEvent.click(screen.getByRole("link", { name: "Versions" }));
  expect(onToggleSidebar).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId("loc").textContent).toBe(
    "/cockpit/abc?tab=spec&sec=2&view=versions",
  );
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

it("shows no workspace band when no workspace is open", () => {
  // All five, and the divider. Checking two of them passes on an
  // implementation that still renders the other three.
  renderWithProviders(<ActivityBar />, { route: "/cockpit" });
  for (const label of ["Outline", "Versions", "References", "Criteria", "Members"]) {
    expect(screen.queryByRole("link", { name: label })).toBeNull();
  }
  expect(screen.queryByTestId("activity-divider")).toBeNull();
  expect(screen.getByRole("link", { name: "Workspaces" })).toBeInTheDocument();
});

it("shows all five workspace icons when a workspace is open", () => {
  renderWithProviders(<ActivityBar />, { route: "/cockpit/ws-1?tab=draft" });
  for (const label of ["Outline", "Versions", "References", "Criteria", "Members"]) {
    expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
  }
});

it("marks EXACTLY ONE workspace icon active, and it is the matching one", () => {
  // Asserting only that the expected icon is active passes on an implementation
  // that lights all five.
  for (const view of ["outline", "versions", "references", "criteria", "members"]) {
    const { unmount } = renderWithProviders(<ActivityBar />, {
      route: `/cockpit/ws-1?view=${view}`,
    });
    const active = screen
      .getAllByRole("link")
      .filter((el) => el.getAttribute("aria-current") === "page");
    expect(active).toHaveLength(1);
    expect(active[0].getAttribute("aria-label")?.toLowerCase()).toBe(view);
    unmount();
  }
});

it("a workspace icon keeps every other parameter in its href", () => {
  renderWithProviders(<ActivityBar />, {
    route: "/cockpit/ws-1?tab=scorecard&sec=3&artifact=spec&a=aaaaaaa&b=bbbbbbb&layout=unified",
  });
  const href = screen.getByRole("link", { name: "Versions" }).getAttribute("href") ?? "";
  expect(href.startsWith("/cockpit/ws-1")).toBe(true);
  const out = new URLSearchParams(href.split("?")[1] ?? "");
  expect(out.get("view")).toBe("versions");
  expect(out.get("tab")).toBe("scorecard");
  expect(out.get("sec")).toBe("3");
  expect(out.get("artifact")).toBe("spec");
  expect(out.get("a")).toBe("aaaaaaa");
  expect(out.get("b")).toBe("bbbbbbb");
  expect(out.get("layout")).toBe("unified");
});

it("separates the two bands with a divider", () => {
  renderWithProviders(<ActivityBar />, { route: "/cockpit/ws-1" });
  expect(screen.getByTestId("activity-divider")).toBeInTheDocument();
});

it("no longer renders the Current workspace item", () => {
  renderWithProviders(<ActivityBar />, { route: "/cockpit/ws-1" });
  expect(screen.queryByRole("link", { name: "Current workspace" })).toBeNull();
});

it("clicking an INACTIVE workspace icon reveals a collapsed pane and still navigates", () => {
  // The pane can be collapsed while the rail still shows five icons. Without a
  // reveal, clicking a different view moved the highlight and rewrote ?view but
  // showed nothing — a control that looks like it worked and did not. VS Code
  // opens the sidebar on any rail click.
  const onToggleSidebar = vi.fn();
  const onRevealSidebar = vi.fn();
  renderWithProviders(
    <>
      <ActivityBar onToggleSidebar={onToggleSidebar} onRevealSidebar={onRevealSidebar} />
      <LocationEcho />
    </>,
    { route: "/cockpit/abc?tab=spec&sec=2&view=versions" },
  );
  fireEvent.click(screen.getByRole("link", { name: "Criteria" }));
  expect(onRevealSidebar).toHaveBeenCalledTimes(1);
  // Reveal is NOT a toggle: an already-open pane must stay open.
  expect(onToggleSidebar).not.toHaveBeenCalled();
  // And the navigation must still happen — reveal does not preventDefault.
  const url = new URLSearchParams(
    screen.getByTestId("loc").textContent?.split("?")[1] ?? "",
  );
  expect(url.get("view")).toBe("criteria");
  expect(url.get("tab")).toBe("spec");
  expect(url.get("sec")).toBe("2");
});

it("clicking the ACTIVE workspace icon toggles and does NOT reveal", () => {
  // Otherwise collapsing would immediately re-expand.
  const onToggleSidebar = vi.fn();
  const onRevealSidebar = vi.fn();
  renderWithProviders(
    <ActivityBar onToggleSidebar={onToggleSidebar} onRevealSidebar={onRevealSidebar} />,
    { route: "/cockpit/abc?view=versions" },
  );
  fireEvent.click(screen.getByRole("link", { name: "Versions" }));
  expect(onToggleSidebar).toHaveBeenCalledTimes(1);
  expect(onRevealSidebar).not.toHaveBeenCalled();
});
