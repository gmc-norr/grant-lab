import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation } from "react-router-dom";
import { expect, it } from "vitest";

import { renderWithProviders } from "../test-utils";
import { WorkspacesFilters } from "./WorkspacesFilters";

function LocationProbe() {
  const loc = useLocation();
  return <output data-testid="search">{loc.search}</output>;
}

it("selecting a status writes ?status to the URL", async () => {
  const user = userEvent.setup({ pointerEventsCheck: 0 });
  renderWithProviders(
    <>
      <WorkspacesFilters />
      <LocationProbe />
    </>,
    { route: "/cockpit" },
  );
  await user.click(screen.getByLabelText("Status", { selector: "input" }));
  await user.click(await screen.findByRole("option", { name: "Submitted", hidden: true }));
  await waitFor(() =>
    expect(screen.getByTestId("search").textContent).toContain("status=submitted"),
  );
});
