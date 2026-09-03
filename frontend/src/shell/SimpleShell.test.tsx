import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
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
  });

  it("renders the child route inside a main landmark", () => {
    renderAt("/");
    expect(screen.getByText("calls page")).toBeInTheDocument();
    expect(screen.getByRole("main")).toBeInTheDocument();
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
});
