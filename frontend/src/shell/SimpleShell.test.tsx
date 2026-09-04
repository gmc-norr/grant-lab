import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

// The Admin destination is filtered on `capabilities.admin` — the same field
// RequireAdmin.tsx:13 reads — so exercising both branches needs a session in
// hand. Mocking the session module with a mutable holder is this repo's
// established pattern for that (CollabEditor.identity.test.tsx:9). The factory
// only CLOSES OVER `sessionData`; it dereferences it when `useSession()` runs
// during a render, long after the const initialises, so the hoisted `vi.mock`
// never trips the TDZ.
// FakeSession is a local PARTIAL: it is not checked against the generated
// /api/me type, and the mock returns no isLoading/isError. Shape drift is
// caught by tsc over SimpleShell.tsx (tsconfig include: ["src"], build is
// `tsc && vite build`), not by this mock — do not read it as shape coverage.
type FakeSession = { user: { email: string }; capabilities: { admin: boolean } };
const sessionData: { value: FakeSession | undefined } = { value: undefined };
vi.mock("../api/session", () => ({ useSession: () => ({ data: sessionData.value }) }));

// useLogout is a real react-query mutation over api.POST — calling it would hit
// the network. Mocked so the assertion is about WIRING (the menu item calls the
// logout mutation), which is the thing that was missing. Hoisted for the same
// reason as sessionData above.
const logout = vi.hoisted(() => ({ mutate: vi.fn(), isPending: false }));
// jsdom refuses a plain assignment to window.location.assign, so redefine it.
const assign = vi.fn();
Object.defineProperty(window, "location", {
  configurable: true,
  value: { ...window.location, assign },
});
vi.mock("../api/auth", () => ({ useLogout: () => logout }));

import { SimpleShell } from "./SimpleShell";
import { theme } from "../theme/theme";

const ADMIN: FakeSession = { user: { email: "admin@umu.se" }, capabilities: { admin: true } };
const MEMBER: FakeSession = { user: { email: "p@umu.se" }, capabilities: { admin: false } };

function renderAt(path: string) {
  const router = createMemoryRouter(
    [
      {
        element: <SimpleShell />,
        children: [
          { index: true, element: <p>calls page</p> },
          { path: "profiles", element: <p>profiles page</p> },
          // Stands in for a real release-1 route: every one of them renders its
          // own <main>. The nesting assertion above needs a child that does.
          {
            path: "page-with-its-own-main",
            element: <main>a real page</main>,
          },
        ],
      },
    ],
    { initialEntries: [path] },
  );
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={new QueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </MantineProvider>,
  );
}

describe("SimpleShell", () => {
  beforeEach(() => {
    sessionData.value = MEMBER;
    logout.mutate.mockClear();
    assign.mockClear();
    logout.isPending = false;
  });

  it("renders the child route and adds NO landmark of its own", () => {
    // The shell used to wrap <Outlet/> in <main>, and this test asserted
    // getByRole("main") against a harness child (<p>calls page</p>) that has no
    // landmark of its own — so it passed on the nesting and would have thrown
    // ("found multiple elements") against any REAL release-1 route element,
    // every one of which renders its own <main> (Dashboard.tsx:18,
    // CallDetail.tsx:31, AdminUsers.tsx:83, ...). The child below carries one,
    // so exactly-one is what is pinned: re-wrapping the Outlet fails here.
    renderAt("/page-with-its-own-main");
    expect(screen.getByText("a real page")).toBeInTheDocument();
    expect(screen.getAllByRole("main")).toHaveLength(1);
    expect(screen.getByRole("main")).toHaveTextContent("a real page");
  });

  it("offers the three release-1 destinations as nav links to an admin", () => {
    sessionData.value = ADMIN;
    renderAt("/");
    const nav = screen.getByRole("navigation", { name: /main/i });
    for (const label of ["Calls", "Profiles", "Admin"]) {
      expect(within(nav).getByRole("link", { name: label })).toBeInTheDocument();
    }
  });

  it("hides the Admin destination from a non-admin", () => {
    sessionData.value = MEMBER;
    renderAt("/");
    const nav = screen.getByRole("navigation", { name: /main/i });
    expect(within(nav).getByRole("link", { name: "Calls" })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Profiles" })).toBeInTheDocument();
    // A non-admin must not be offered a destination that 403s.
    expect(screen.queryByRole("link", { name: "Admin" })).not.toBeInTheDocument();
  });

  it("marks the active destination with aria-current", () => {
    renderAt("/profiles");
    expect(screen.getByRole("link", { name: "Profiles" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("link", { name: "Calls" })).not.toHaveAttribute("aria-current");
  });

  it("renders none of the cockpit shell furniture", () => {
    renderAt("/");
    // The AppShell landmarks that release 1 deliberately drops.
    // Assert on what each component ACTUALLY exposes. `queryByRole("status")`
    // would be vacuous: StatusBar renders `<footer aria-label="Status bar">`
    // (StatusBar.tsx:125), whose implicit role is contentinfo — never `status` —
    // so that query returns null whether or not StatusBar is mounted.
    expect(screen.queryByLabelText("Primary")).not.toBeInTheDocument(); // ActivityBar
    expect(screen.queryByLabelText("Status bar")).not.toBeInTheDocument(); // StatusBar
    expect(screen.queryByLabelText(/resize/i)).not.toBeInTheDocument(); // panel handles
  });

  it("names the account menu with the email it displays", () => {
    // WCAG 2.5.3 label-in-name: the accessible name must CONTAIN the visible
    // text, or a voice-control user cannot say what they see. A bare
    // aria-label="Account menu" would replace the email outright.
    renderAt("/");
    const account = screen.getByRole("button", { name: /account menu for p@umu\.se/i });
    expect(account).toHaveTextContent("p@umu.se");
  });

  it("keeps the brand as a link home", () => {
    renderAt("/profiles");
    expect(screen.getByRole("link", { name: "Call Match" })).toHaveAttribute("href", "/");
  });

  it("offers Sign out in the account menu and it invokes the logout mutation", async () => {
    // Release-1 CRITICAL: TitleBar.tsx is the full edition's only sign-out
    // control and check_match_bundle.mjs excludes it from the match graph, so
    // without this item a shared workstation has no way to end a session.
    renderAt("/");
    fireEvent.click(screen.getByRole("button", { name: /account menu for p@umu\.se/i }));
    // findBy, not getBy: Mantine's Menu.Dropdown mounts through a transition.
    const item = await screen.findByRole("menuitem", { name: "Sign out" });
    expect(item).toBeInTheDocument();
    expect(logout.mutate).not.toHaveBeenCalled();
    fireEvent.click(item);
    expect(logout.mutate).toHaveBeenCalledTimes(1);

    // And it must NAVIGATE on success. Asserting only that mutate was called
    // leaves the onSuccess handler free to be dropped — and that handler is what
    // discards every cached query along with the document. A sign-out that
    // clears the cookie and leaves the previous user's calls, profiles and admin
    // data on screen is the failure this shell exists to avoid on a shared
    // workstation. Driving the real second argument is what makes it a gate.
    const [, opts] = logout.mutate.mock.calls[0] as [
      undefined,
      { onSuccess?: () => void } | undefined,
    ];
    expect(opts?.onSuccess).toBeTypeOf("function");
    opts!.onSuccess!();
    expect(assign).toHaveBeenCalledWith("/login");
  });

  it("disables Sign out while the logout request is in flight", async () => {
    // Pins the second half of TitleBar.tsx:76's call shape — without it a
    // double-click fires two logout POSTs.
    logout.isPending = true;
    renderAt("/");
    fireEvent.click(screen.getByRole("button", { name: /account menu for p@umu\.se/i }));
    expect(await screen.findByRole("menuitem", { name: "Sign out" })).toHaveAttribute(
      "data-disabled",
      "true",
    );
  });
});
