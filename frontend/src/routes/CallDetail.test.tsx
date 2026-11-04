import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { CallDetail } from "./CallDetail";

afterEach(() => { vi.restoreAllMocks(); });

function LocationProbe() {
  return <span data-testid="loc">{useLocation().pathname}</span>;
}

// Optional `qc`: the decision-counts gate below spies on invalidateQueries,
// which means the test must hold the SAME QueryClient instance the component
// uses rather than one buried inside this helper.
function renderDetail(url = "/calls/5?profile=7", qc = new QueryClient()) {
  return {
    qc,
    ...render(
      <MantineProvider theme={theme} defaultColorScheme="dark">
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={[url]}>
            <Routes>
              <Route path="/calls/:id" element={<><CallDetail /><LocationProbe /></>} />
              <Route path="/" element={<LocationProbe />} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      </MantineProvider>,
    ),
  };
}

const RANKED = {
  call: {
    id: 5, title: "Genomics Grant", source_name: "Vinnova", programme: "Prog",
    external_id: "EX-1", published_at: null, deadline: null, budget_min: null,
    budget_max: null, currency: null, url: "https://x", keywords: ["dna"], summary: "Sum.",
  },
  ranking: {
    aggregate_score: 72, aggregate_reason_label: null, model_label: "Sonnet 4.6",
    rationale: "Strong fit.", embed_similarity: 0.42, escalated: false,
    escalated_from_score: null, analysis_model_label: "Sonnet 4.6", summary: "Deep summary.",
    ranking_failed: false, ranking_failed_at: null,
    applicant_notes: [{ category: "budget", heading: "Budget & funding", text: "Note." }],
  },
  criteria: [
    {
      criterion: "embedding", label: "Topic similarity", rating: "pass", is_topic_fit: false,
      detail: "Cosine similarity 0.420 (pass threshold 0.30).", rationale: null,
      is_reason_code: true, quoted_evidence: null,
    },
  ],
  best_profile_id: 7,
};

it("MATCH shows a saved call's note and lets the reader return it to the inbox", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { ...RANKED, call: { ...RANKED.call, decision_state: "saved", decision_note: "Discuss with the team" } },
  } as never);
  const remove = vi.spyOn(clientMod.api, "DELETE").mockResolvedValue({ data: { state: null } } as never);
  renderDetail();
  expect(await screen.findByText("Saved to this profile")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Discuss with the team" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Return to inbox" }));
  await waitFor(() => expect(remove).toHaveBeenCalledWith("/api/calls/{call_id}/save", {
    params: { path: { call_id: 5 }, query: { profile_id: 7 } },
  }));
  expect(await screen.findByRole("button", { name: "Save" })).toBeEnabled();
});

it("MATCH keeps a failed save visible and retryable", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED } as never);
  const save = vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    error: { error: { code: "internal_error", message: "Service unavailable" } },
  } as never);
  renderDetail();
  await userEvent.click(await screen.findByRole("button", { name: /^Save$/ }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Service unavailable");
  expect(screen.queryByText("Saved to this profile")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /^Save$/ }));
  expect(save).toHaveBeenCalledTimes(2);
});

it("MATCH disables both decisions when no profile is selected", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED } as never);
  renderDetail("/calls/5");
  expect(await screen.findByRole("button", { name: /^Save$/ })).toBeDisabled();
  expect(screen.getByRole("button", { name: /^Hide$/ })).toBeDisabled();
});

it("MATCH waits for a note acknowledgement before changing the decision", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { ...RANKED, call: { ...RANKED.call, decision_state: "saved", decision_note: null } },
  } as never);
  let acknowledge!: (reply: never) => void;
  vi.spyOn(clientMod.api, "PUT").mockImplementation(() => new Promise(resolve => { acknowledge = resolve; }));
  renderDetail();
  await userEvent.click(await screen.findByRole("button", { name: "Add note" }));
  await userEvent.type(screen.getByRole("textbox", { name: /Why did you save/ }), "Discuss with the team{Enter}");
  expect(screen.getByRole("button", { name: /^Hide$/ })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Return to inbox" })).toBeDisabled();
  await act(async () => acknowledge({ data: { decision_note: "Discuss with the team" } } as never));
  expect(screen.getByRole("button", { name: /^Hide$/ })).toBeEnabled();
  expect(screen.getByRole("textbox", { name: /Why did you save/ })).toHaveValue("Discuss with the team");
});

it("the MATCH call page renders NO Start-drafting control", async () => {
  // An absence assertion on an async page is worthless until the page has
  // actually rendered, so wait for the heading FIRST — otherwise this passes on a
  // page that never loaded at all.
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  renderDetail();
  expect(await screen.findByRole("heading", { name: "Genomics Grant" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: /start drafting/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /start drafting/i })).not.toBeInTheDocument();
  for (const a of screen.queryAllByRole("link")) {
    expect(a.getAttribute("href") ?? "").not.toContain("/cockpit");
  }
});

it("a not-found error shows the not-found alert", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: undefined,
    error: { error: { code: "not_found", message: "call not found" } },
  } as never);
  renderDetail();
  await screen.findByRole("alert");
  // Proves the notFound branch fires: unwrap throws ApiError with .code = "not_found",
  // which the component reads directly off the error object (cast is correct).
  expect(screen.getByRole("alert").textContent).toBe("Call not found.");
});

it("renders deadline_local_date instead of slicing the UTC string", async () => {
  // The date must stay in the FUTURE relative to the real clock. This fixture
  // used to be 2026-09-15/16, which made the test a time bomb: once that day
  // passed the page began rendering "Closed 2026-09-16" and the exact-text
  // match broke, five days later, in a test about timezone slicing that has
  // nothing to do with expiry. 2099 keeps the only property under test — the
  // UTC instant (22:00Z on the 15th) falls on the 16th in local time, so the
  // page must print the local date, not `deadline.slice(0, 10)`.
  const withDeadline = {
    ...RANKED,
    call: {
      ...RANKED.call,
      deadline: "2099-09-15T22:00:00Z",
      deadline_local_date: "2099-09-16",
    },
  };
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: withDeadline, error: undefined } as never);
  renderDetail();
  await screen.findByRole("heading", { name: "Genomics Grant" });
  // The local date (2099-09-16) must appear, not the UTC slice (2099-09-15).
  expect(screen.getByText("2099-09-16")).toBeInTheDocument();
  expect(screen.queryByText("2099-09-15")).not.toBeInTheDocument();
});

it("the back link returns to the feed the reader arrived from, view and all (D11)", async () => {
  // B31c killer. `to="/"` drops the triage view and the profile: a reader who
  // opened a call from Hidden lands back in the Inbox of no profile in
  // particular, which is not where they were.
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  renderDetail("/calls/5?profile=7&view=hidden");
  await screen.findByRole("heading", { name: "Genomics Grant" });
  expect(screen.getByRole("link", { name: /all calls/i })).toHaveAttribute(
    "href",
    "/?profile=7&view=hidden",
  );
});

it("the back link is a bare / when the reader arrived with no search at all", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  renderDetail("/calls/5");
  await screen.findByRole("heading", { name: "Genomics Grant" });
  expect(screen.getByRole("link", { name: /all calls/i })).toHaveAttribute("href", "/");
});

// --- no ?profile= at all: fall back to the reader's own default --------------
// UX pass 2026-10-31: a call opened from a bookmark or a typed address showed no
// score, no match reasons and greyed-out Save/Hide, though the calls list makes
// exactly this fallback server-side.

function meWithDefault(defaultProfileId: number | null) {
  return {
    data: {
      capabilities: {},
      user: { id: 1, email: "a@umu.se", name: null, status: "active", default_profile_id: defaultProfileId },
    },
  };
}

function profilesRequested(get: { mock: { calls: unknown[][] } }): (string | undefined)[] {
  return get.mock.calls
    .filter((c) => c[0] === "/api/calls/{call_id}")
    .map((c) => (c[1] as { params: { query: { profile?: string } } }).params.query.profile);
}

it("MATCH with no ?profile= uses the reader's default profile, so Save works", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockImplementation((async (path: string) =>
    path === "/api/me" ? meWithDefault(7) : { data: RANKED }) as never);
  renderDetail("/calls/5");
  await waitFor(() => expect(profilesRequested(get)).toContain("7"));
  await waitFor(() => expect(screen.getByRole("button", { name: /^Save$/ })).toBeEnabled());
});

it("an explicit empty ?profile= is left alone: no fallback, decisions stay off", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockImplementation((async (path: string) =>
    path === "/api/me" ? meWithDefault(7) : { data: RANKED }) as never);
  renderDetail("/calls/5?profile=");
  expect(await screen.findByRole("button", { name: /^Save$/ })).toBeDisabled();
  await waitFor(() => expect(get.mock.calls.some((c) => c[0] === "/api/me")).toBe(true));
  expect(profilesRequested(get)).not.toContain("7");
});

it("with no default profile the page stays as it was: decisions off, nothing invented", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockImplementation((async (path: string) =>
    path === "/api/me" ? meWithDefault(null) : { data: RANKED }) as never);
  renderDetail("/calls/5");
  expect(await screen.findByRole("button", { name: /^Save$/ })).toBeDisabled();
  await waitFor(() => expect(get.mock.calls.some((c) => c[0] === "/api/me")).toBe(true));
  expect(profilesRequested(get).every((p) => p === undefined)).toBe(true);
});
