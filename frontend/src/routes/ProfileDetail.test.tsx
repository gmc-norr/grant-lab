import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
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
  org_type: "university_hospital" as string | null, capabilities: ["biobank"], patient_populations: [],
  scale: null, career_stage: null, role: null, years_since_phd: null, applicant_types: [] as string[],
  funding_instruments: ["project_grant"], funder_exclusions: [], country: "SE" as string | null, host_institution: "UmU",
  can_edit: true, visibility: "private", outgoing_lost_read: false,
  bootstrap_status: null, bootstrap_pubmed_status: null, publications_fetched: null,
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
  expect(screen.getByText("Oncology")).toBeInTheDocument();
  expect(screen.getByText("Biobank")).toBeInTheDocument();
  expect(screen.getByText("Project grant")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /edit/i })).toHaveAttribute("href", "/profiles/5/edit");
});

/** Render the route with GET /api/profiles/{id} refusing under `code`. */
function renderDetailError(code: string) {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: undefined, error: { error: { code, message: "nope" } },
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
}

it("shows not-found on a 404", async () => {
  renderDetailError("not_found");
  // The API answers the SAME 404 for "does not exist" and "not shared with you",
  // so the copy must cover both honestly rather than accuse the reader of a typo.
  expect(
    await screen.findByText("This profile isn't shared with you, or it doesn't exist."),
  ).toBeInTheDocument();
});

it("a non-404 failure says the load failed, NOT that the profile isn't shared", async () => {
  renderDetailError("internal");
  // Only `not_found` carries the sharing meaning. A 500 or a dropped connection
  // reaching the OWNER of the profile would otherwise report their own profile as
  // one they are not allowed to see — and send them hunting a permissions bug
  // that does not exist.
  expect(await screen.findByText("Couldn't load this profile.")).toBeInTheDocument();
  expect(
    screen.queryByText("This profile isn't shared with you, or it doesn't exist."),
  ).toBeNull();
});

it("gate 15: no sharing card when can_edit is false, and the page still renders", async () => {
  renderDetail({ ...DETAIL, can_edit: false });
  // The rest of the page DOES render — so this is not passing on a blank page.
  expect(await screen.findByRole("heading", { name: /Digital Pathology Lab/ })).toBeInTheDocument();
  expect(screen.getByText("Imaging research.")).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: /who can see this/i })).toBeNull();
  expect(screen.queryByRole("radio", { name: /only me and admins/i })).toBeNull();
});

it("mounts the sharing card for an owner", async () => {
  renderDetail();
  expect(await screen.findByRole("heading", { name: /who can see this/i })).toBeInTheDocument();
  expect(screen.getByRole("radio", { name: "Only me and admins" })).toBeChecked();
});

// MINOR 4: pins that ProfileDetail forwards the payload's OWN
// outgoing_lost_read to the transfer card, rather than re-deriving it from
// can_edit (also true for a non-owner admin) -- exactly the bug the
// design note records having shipped once. The negative case below is the
// one that actually distinguishes the two: an `outgoingLostRead={p.can_edit}`
// regression would still pass the positive case (both true here) but would
// wrongly warn in the negative one.
it("passes outgoing_lost_read=true through to the transfer card", async () => {
  renderDetail({ ...DETAIL, can_edit: true, outgoing_lost_read: true });
  expect(await screen.findByRole("heading", { name: /transfer ownership/i })).toBeInTheDocument();
  expect(
    screen.getByText(/you will lose access to this profile once it is transferred/i),
  ).toBeInTheDocument();
});

it("does NOT warn when outgoing_lost_read is false, even though can_edit is true", async () => {
  renderDetail({ ...DETAIL, can_edit: true, outgoing_lost_read: false });
  expect(await screen.findByRole("heading", { name: /transfer ownership/i })).toBeInTheDocument();
  expect(
    screen.queryByText(/you will lose access to this profile once it is transferred/i),
  ).toBeNull();
});

// --- H3: eligibility facts empty => no call can be checked for eligibility ---
//
// The model answers `eligibility: "unclear"` for every call because
// `_eligibility_block` in ranking/prompt.py renders "(none specified)" for a
// profile whose `structured` carries no org/geo/eligibility facts. Until
// 2026-11-10 `ranking/aggregate.py` capped such a call at 50, so "top matches"
// (>=70), the digest top bucket (>=60) and every corridor board (min_score 60)
// were permanently empty; this warning was the fix, and the cap rule was kept
// (human decision, 2026-10-31). That decision was reversed on 2026-11-10 (spec
// 2026-11-10-score-rule-topic-only-design.md): the cap is gone and the warning
// now says only that eligibility can't be checked.
const WARNING =
  "Eligibility is not filled in, so calls can't be checked for eligibility. Fill in organisation type, country and applicant types.";

const NO_ELIGIBILITY = { ...DETAIL, org_type: null, country: null, applicant_types: [] };

it("warns on the detail page when organisation type, country and applicant types are all empty", async () => {
  renderDetail(NO_ELIGIBILITY);
  // findByText matches on CONTENT (exact, whitespace-normalised full string), and
  // the waitFor re-asserts it on the settled render rather than on whatever was
  // mounted first.
  const message = await screen.findByText(WARNING);
  await waitFor(() => expect(message).toHaveTextContent(WARNING));
  expect(message.closest("[role='alert']")).not.toBeNull();
});

it("shows no eligibility warning when one of the three facts is filled", async () => {
  // Country alone. This direction is what stops "always render the alert" from
  // passing the test above.
  renderDetail({ ...NO_ELIGIBILITY, country: "SE" });
  expect(await screen.findByRole("heading", { name: /Digital Pathology Lab/ })).toBeInTheDocument();
  expect(screen.queryByText(WARNING)).toBeNull();
});
