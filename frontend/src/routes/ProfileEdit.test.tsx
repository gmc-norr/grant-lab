import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { ProfileEdit } from "./ProfileEdit";

afterEach(() => { vi.restoreAllMocks(); });

function Probe() { return <span data-testid="loc">{useLocation().pathname}</span>; }

const BASE = {
  id: 5, name: "Lab", kind: "organizational_unit", level: "group", description: "Old desc.",
  budget_currency: "SEK", areas: ["oncology"], budget_min: 100, budget_max: 200, trl_min: 3, trl_max: 7,
  must_include: [], hard_exclusions: [], org_type: "university_hospital", capabilities: ["biobank"],
  patient_populations: [], scale: null as string | null, career_stage: null as string | null,
  role: null as string | null, years_since_phd: null as number | null,
  applicant_types: [], funding_instruments: ["project_grant"], funder_exclusions: [], country: "SE",
  host_institution: "UmU", can_edit: true, bootstrap_status: null as string | null,
  bootstrap_pubmed_status: null as string | null, publications_fetched: null as number | null,
};

function renderEdit(payload = BASE) {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: payload, error: undefined } as never);
  const qc = new QueryClient();
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/profiles/5/edit"]}>
          <Routes>
            <Route path="/profiles/:id/edit" element={<ProfileEdit />} />
            <Route path="/profiles/:id" element={<Probe />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

it("pre-fills from the read-model and saves, navigating to detail", async () => {
  const put = vi.spyOn(clientMod.api, "PUT").mockResolvedValue({ data: { id: 5 }, error: undefined } as never);
  renderEdit();
  const desc = await screen.findByLabelText(/description/i);
  expect(desc).toHaveValue("Old desc.");
  fireEvent.change(desc, { target: { value: "New desc." } });
  fireEvent.click(screen.getByRole("button", { name: /save and re-rank/i }));
  await waitFor(() => expect(put).toHaveBeenCalledWith("/api/profiles/{profile_id}",
    expect.objectContaining({ params: { path: { profile_id: 5 } }, body: expect.objectContaining({ description: "New desc." }) })));
  await waitFor(() => expect(screen.getByTestId("loc")).toHaveTextContent("/profiles/5"));
});

it("disables the form and shows the banner while drafting", async () => {
  renderEdit({ ...BASE, bootstrap_status: "running" });
  const desc = await screen.findByLabelText(/description/i);
  expect(desc).toBeDisabled();
  expect(screen.getByText(/drafting/i)).toBeInTheDocument();
});

it("disables the form when the viewer cannot edit", async () => {
  renderEdit({ ...BASE, can_edit: false });
  const desc = await screen.findByLabelText(/description/i);
  expect(desc).toBeDisabled();
});
