import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { ProfileEdit } from "./ProfileEdit";

// The MATCH edition, which every other unit test does not exercise: edition.ts
// falls back to "full" and vitest sets no VITE_APP_EDITION, so in the rest of
// the suite ProfileOptions is a bare fragment and the hazard this placement
// exists to avoid cannot fire.
vi.mock("../edition", () => ({
  get isMatchEdition() {
    return true;
  },
}));

afterEach(() => {
  vi.restoreAllMocks();
});

const BARE = {
  id: 5,
  name: "Lab",
  kind: "organizational_unit",
  level: "group",
  description: "Old desc.",
  budget_currency: "SEK",
  areas: ["oncology"],
  budget_min: null,
  budget_max: null,
  trl_min: null,
  trl_max: null,
  must_include: [],
  hard_exclusions: [],
  // The three facts the ranker reads, all blank -- so the warning renders.
  org_type: null as string | null,
  country: null as string | null,
  applicant_types: [] as string[],
  capabilities: [],
  patient_populations: [],
  scale: null as string | null,
  career_stage: null as string | null,
  role: null as string | null,
  years_since_phd: null as number | null,
  funding_instruments: [],
  funder_exclusions: [],
  host_institution: null as string | null,
  can_edit: true,
  bootstrap_status: null as string | null,
  bootstrap_pubmed_status: null as string | null,
  publications_fetched: null as number | null,
};

it("keeps the eligibility warning OUTSIDE the match edition's collapsed <details>", async () => {
  // This is the single design decision the whole task rests on, and nothing
  // else pins it. In the match edition ProfileOptions wraps its children in
  // `<details class="match-options">`, which ships CLOSED -- so an alert placed
  // beside StructuredFields would be invisible in exactly the edition where the
  // capped-at-50 defect bites. The full-edition tests cannot see that (there
  // ProfileOptions is a fragment) and e2e:match cannot either (its profile
  // fixture fills the fields, so the alert never renders).
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: BARE, error: undefined } as never);
  const { container } = render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MantineProvider theme={theme}>
        <MemoryRouter initialEntries={["/profiles/5/edit"]}>
          <Routes>
            <Route path="/profiles/:id/edit" element={<ProfileEdit />} />
          </Routes>
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>,
  );

  // Wait for the alert itself, then assert on its ancestry -- `findBy*` waits
  // for presence, so the placement assertion has to come after it settles.
  await waitFor(() => {
    expect(container.querySelector('[data-field="eligibility-empty"]')).not.toBeNull();
  });
  const alert = container.querySelector('[data-field="eligibility-empty"]');
  expect(alert?.closest("details")).toBeNull();

  // And the <details> really is present on this render, so the assertion above
  // is not vacuously true just because the match wrapper never mounted.
  expect(container.querySelector("details.match-options")).not.toBeNull();
});

it("puts the eligibility FIELDS outside the fold too, right under the warning", async () => {
  // UX review 2026-10-31: the warning said "Fill in organisation type, country
  // and applicant types" while those fields sat in the closed <details>.
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: BARE, error: undefined } as never);
  const { container } = render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MantineProvider theme={theme}>
        <MemoryRouter initialEntries={["/profiles/5/edit"]}>
          <Routes>
            <Route path="/profiles/:id/edit" element={<ProfileEdit />} />
          </Routes>
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>,
  );
  await waitFor(() => expect(container.querySelector('[data-field="eligibility-facts"]')).not.toBeNull());
  const facts = container.querySelector('[data-field="eligibility-facts"]') as HTMLElement;
  expect(facts.closest("details")).toBeNull();
  expect(facts.querySelector("select")).not.toBeNull(); // organisation type, for an org unit
  // And the fold no longer repeats them.
  const fold = container.querySelector("details") as HTMLElement;
  expect(fold.textContent).not.toContain("Organization type");
  expect(fold.textContent).not.toContain("Applying as");
});
