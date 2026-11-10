import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { ProfileNew } from "./ProfileNew";

// The create form asks the three facts the ranker's eligibility check reads --
// organisation type, country, applicant types -- up front (UX review 2026-10-31);
// folded, they were skipped and no call could be checked for eligibility.
// Everything else stays folded.

afterEach(() => {
  vi.restoreAllMocks();
});

function renderNew() {
  return render(
    <MantineProvider theme={theme}>
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/profiles/new"]}>
          <Routes>
            <Route path="/profiles/new" element={<ProfileNew />} />
            <Route path="/profiles/:id/edit" element={<span>edit page</span>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

const folded = (el: HTMLElement) => el.closest("details") !== null;

it("asks the eligibility questions up front and folds only the optional ones", () => {
  renderNew();
  // getAll: Mantine's MultiSelect labels both its search input and its hidden
  // value input, so "Applying as" names two elements -- both must be up front.
  for (const label of ["Organization type", "Country (e.g. SE)", "Applying as"]) {
    for (const el of screen.getAllByLabelText(label)) {
      expect(folded(el), `${label} is folded`).toBe(false);
    }
  }
  for (const label of ["Capabilities", "Host institution", "Funder exclusions"]) {
    expect(folded(screen.getByLabelText(label)), `${label} is not folded`).toBe(true);
  }
  // Each question once: the folded part must not repeat the three.
  expect(screen.getAllByLabelText("Country (e.g. SE)")).toHaveLength(1);
  expect(screen.getByText("Additional preferences (optional)")).toBeInTheDocument();
});

it("asks who the profile is for in plain words, and drops organisation type for 'Me'", () => {
  renderNew();
  const forWhom = screen.getByLabelText("Profile type");
  expect([...forWhom.querySelectorAll("option")].map((o) => o.textContent)).toEqual([
    "Research group",
    "Individual researcher",
  ]);
  fireEvent.change(forWhom, { target: { value: "individual" } });
  expect(screen.queryByLabelText("Organization type")).toBeNull();
  expect(folded(screen.getByLabelText("Country (e.g. SE)"))).toBe(false);
});

it("sends the up-front answers with the new profile", async () => {
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { id: 42 }, error: undefined } as never);
  renderNew();
  fireEvent.change(screen.getByLabelText(/profile name/i), { target: { value: "New Lab" } });
  fireEvent.change(screen.getByLabelText("Country (e.g. SE)"), { target: { value: "SE" } });
  fireEvent.change(screen.getByLabelText("Organization type"), {
    target: { value: "university_hospital" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Create profile" }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/api/profiles",
      expect.objectContaining({
        body: expect.objectContaining({ country: "SE", org_type: "university_hospital" }),
      }),
    ),
  );
});
