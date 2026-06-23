import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { expect, it } from "vitest";

import { theme } from "../theme/theme";
import { AppShell } from "./AppShell";

function renderShellAt(route: string) {
  const router = createMemoryRouter(
    [
      {
        element: <AppShell />,
        children: [{ index: true, element: <p>ROUTED BODY</p> }],
      },
    ],
    { initialEntries: [route] },
  );
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <RouterProvider router={router} />
    </MantineProvider>,
  );
}

it("renders the chrome regions and the routed outlet content", () => {
  renderShellAt("/");
  // "Funding calls" is the per-route title (unique). NB: "Call Manager" appears
  // in BOTH the title bar brand and the status bar, so don't getByText it here.
  expect(screen.getByText("Funding calls")).toBeInTheDocument(); // title bar
  expect(screen.getByLabelText("Primary")).toBeInTheDocument(); // activity bar
  expect(screen.getByLabelText("Status bar")).toBeInTheDocument(); // status bar
  expect(screen.getByText("ROUTED BODY")).toBeInTheDocument(); // <Outlet/>
});
