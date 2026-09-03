import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { ProfileDetail } from "./ProfileDetail";

afterEach(() => {
  vi.restoreAllMocks();
});

const DETAIL = {
  id: 5, name: "Digital Pathology Lab", kind: "organizational_unit", level: "group",
  description: "Imaging research.", budget_currency: "SEK",
  areas: ["oncology"], budget_min: 100, budget_max: 200, trl_min: 3, trl_max: 7,
  must_include: ["pediatric"], hard_exclusions: [],
  org_type: "university_hospital", capabilities: ["biobank"], patient_populations: [],
  scale: null, career_stage: null, role: null, years_since_phd: null, applicant_types: [],
  funding_instruments: ["project_grant"], funder_exclusions: [], country: "SE", host_institution: "UmU",
  can_edit: true, bootstrap_status: null, bootstrap_pubmed_status: null, publications_fetched: null,
};

function renderDetail(payload = DETAIL) {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: payload, error: undefined } as never);
  const qc = new QueryClient();
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/profiles/5"]}>
          <Routes><Route path="/profiles/:id" element={<ProfileDetail />} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

it("renders the description, rubric, and namespaced fields", async () => {
  renderDetail();
  expect(await screen.findByRole("heading", { name: /Digital Pathology Lab/ })).toBeInTheDocument();
  expect(screen.getByText("Imaging research.")).toBeInTheDocument();
  expect(screen.getByText("oncology")).toBeInTheDocument();
  expect(screen.getByText("biobank")).toBeInTheDocument();
  expect(screen.getByText("project_grant")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /edit/i })).toHaveAttribute("href", "/profiles/5/edit");
});

it("shows not-found on a 404", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: undefined, error: { error: { code: "not_found", message: "profile not found" } },
  } as never);
  const qc = new QueryClient();
  render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/profiles/5"]}>
          <Routes><Route path="/profiles/:id" element={<ProfileDetail />} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
  expect(await screen.findByText(/not found/i)).toBeInTheDocument();
});
