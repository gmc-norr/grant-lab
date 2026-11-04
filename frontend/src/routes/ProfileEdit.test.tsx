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
  must_include: [], hard_exclusions: [], org_type: "university_hospital" as string | null, capabilities: ["biobank"],
  patient_populations: [], scale: null as string | null, career_stage: null as string | null,
  role: null as string | null, years_since_phd: null as number | null,
  applicant_types: [] as string[], funding_instruments: ["project_grant"], funder_exclusions: [],
  country: "SE" as string | null,
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

/** Same mount as renderEdit, but with GET /api/profiles/{id} refusing under `code`. */
function renderEditError(code: string) {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: undefined, error: { error: { code, message: "nope" } },
  } as never);
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

it("a 404 on the edit route says the profile isn't shared, not 'not found'", async () => {
  renderEditError("not_found");
  // /profiles/{id}/edit inherits the detail route's ambiguous 404: a colleague
  // following an old EDIT link is the realistic reader, and "Profile not found."
  // accused them of a typo.
  expect(
    await screen.findByText("This profile isn't shared with you, or it doesn't exist."),
  ).toBeInTheDocument();
});

it("a non-404 failure on the edit route says the load failed, not that it isn't shared", async () => {
  renderEditError("internal");
  expect(await screen.findByText("Couldn't load this profile.")).toBeInTheDocument();
  expect(
    screen.queryByText("This profile isn't shared with you, or it doesn't exist."),
  ).toBeNull();
});

it("pre-fills from the read-model and saves, navigating to detail", async () => {
  const put = vi.spyOn(clientMod.api, "PUT").mockResolvedValue({ data: { id: 5 }, error: undefined } as never);
  renderEdit();
  const desc = await screen.findByLabelText(/description/i);
  expect(desc).toHaveValue("Old desc.");
  fireEvent.change(desc, { target: { value: "New desc." } });
  fireEvent.click(screen.getByRole("button", { name: /save preferences/i }));
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

it("disables the form when the viewer cannot edit, and says why", async () => {
  renderEdit({ ...BASE, can_edit: false });
  const desc = await screen.findByLabelText(/description/i);
  expect(desc).toBeDisabled();
  expect(
    screen.getByText("View only. This profile can be edited by its owner or an administrator."),
  ).toBeInTheDocument();
});

it("says nothing about read-only access to someone who can edit", async () => {
  renderEdit(BASE);
  await screen.findByLabelText(/description/i);
  expect(screen.queryByText(/only its owner or an admin can change it/)).toBeNull();
});

// --- H3: eligibility facts empty => every call is capped at 50 --------------
//
// The editor is where the fix lives: `ranking/aggregate.py` caps the aggregate
// at 50 for any call the model calls `eligibility: "unclear"`, and it calls
// every call unclear while `_eligibility_block` (ranking/prompt.py) has nothing
// to render but "(none specified)". Nothing in the UI said so, so "top matches",
// the digest top bucket and the corridor boards were silently always empty.
const WARNING =
  "Eligibility is not filled in. Calls can't be judged eligible, so no call can score above 50 and none will reach 'top matches' or the board. Fill in organisation type, country and applicant types.";

const NO_ELIGIBILITY = { ...BASE, org_type: null, country: null, applicant_types: [] };

it("warns in the editor when organisation type, country and applicant types are all empty", async () => {
  renderEdit(NO_ELIGIBILITY);
  const message = await screen.findByText(WARNING);
  await waitFor(() => expect(message).toHaveTextContent(WARNING));
  expect(message.closest("[role='alert']")).not.toBeNull();
});

it("shows no editor warning when one of the three facts is filled", async () => {
  // Applicant types alone, filled server-side. Without this direction, an alert
  // that rendered unconditionally would pass the test above.
  renderEdit({ ...NO_ELIGIBILITY, applicant_types: ["solo_pi"] });
  expect(await screen.findByLabelText(/description/i)).toHaveValue("Old desc.");
  expect(screen.queryByText(WARNING)).toBeNull();
});

it("drops the warning as soon as a country is typed, before saving", async () => {
  // The predicate reads the FORM state, not the server payload: a warning that
  // only cleared after a save would keep nagging the person who just fixed it.
  renderEdit(NO_ELIGIBILITY);
  expect(await screen.findByText(WARNING)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText(/^country/i), { target: { value: "SE" } });
  await waitFor(() => expect(screen.queryByText(WARNING)).toBeNull());
});
