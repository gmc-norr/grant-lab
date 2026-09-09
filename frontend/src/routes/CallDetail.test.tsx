import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { CallDetail } from "./CallDetail";

// /calls/:id is mounted by BOTH routers, so this page renders in both editions and
// the Start-drafting button below it is edition-gated. Mutable holder + GETTER,
// the pattern from AdminNav.test.tsx:25-34.
const edition = vi.hoisted(() => ({ isMatch: false }));
vi.mock("../edition", () => ({
  get isMatchEdition() {
    return edition.isMatch;
  },
  get APP_EDITION() {
    return edition.isMatch ? "match" : "full";
  },
  resolveEdition: (raw: string | undefined) => (raw === "match" ? "match" : "full"),
}));

afterEach(() => { vi.restoreAllMocks(); edition.isMatch = false; });

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
  edition.isMatch = true;
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
  edition.isMatch = true;
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
  edition.isMatch = true;
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED } as never);
  renderDetail("/calls/5");
  expect(await screen.findByRole("button", { name: /^Save$/ })).toBeDisabled();
  expect(screen.getByRole("button", { name: /^Hide$/ })).toBeDisabled();
});

it("MATCH waits for a note acknowledgement before changing the decision", async () => {
  edition.isMatch = true;
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

it("renders the ranking hero, analyst notes, and criterion why-string", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  renderDetail();
  expect(await screen.findByRole("heading", { name: "Genomics Grant" })).toBeInTheDocument();
  expect(screen.getByText("72")).toHaveAttribute("data-band", "strong"); // 72 >= 70
  expect(screen.getByText("Deep summary.")).toBeInTheDocument();
  expect(screen.getByText("Budget & funding")).toBeInTheDocument();
  expect(screen.getByText("Cosine similarity 0.420 (pass threshold 0.30).")).toBeInTheDocument();
});

it("the FULL call page links Start drafting to /cockpit/new with the call + best profile", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  renderDetail();
  expect(await screen.findByRole("heading", { name: "Genomics Grant" })).toBeInTheDocument();
  const href = screen.getByRole("link", { name: /start drafting/i }).getAttribute("href") ?? "";
  expect(href).toContain("/cockpit/new?");
  expect(href).toContain("call_id=5");
  expect(href).toContain("profile_id=7");
});

it("the MATCH call page renders NO Start-drafting control", async () => {
  // An absence assertion on an async page is worthless until the page has
  // actually rendered, so wait for the heading FIRST — otherwise this passes on a
  // page that never loaded at all.
  edition.isMatch = true;
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  renderDetail();
  expect(await screen.findByRole("heading", { name: "Genomics Grant" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: /start drafting/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /start drafting/i })).not.toBeInTheDocument();
  for (const a of screen.queryAllByRole("link")) {
    expect(a.getAttribute("href") ?? "").not.toContain("/cockpit");
  }
});

it("renders the low band for a zero aggregate score", async () => {
  const zero = { ...RANKED, ranking: { ...RANKED.ranking, aggregate_score: 0 } };
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: zero, error: undefined } as never);
  renderDetail();
  expect(await screen.findByText("0")).toHaveAttribute("data-band", "low");
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

it("Hide requires confirm before posting", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { hidden: true }, error: undefined } as never);
  renderDetail();
  // First click: shows confirm step, does NOT fire POST yet.
  fireEvent.click(await screen.findByRole("button", { name: /hide this call/i }));
  expect(post).not.toHaveBeenCalled();
  // Confirm button is now visible.
  expect(screen.getByRole("button", { name: /yes, hide it/i })).toBeInTheDocument();
  // Cancel goes back to the initial state.
  fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
  expect(screen.getByRole("button", { name: /hide this call/i })).toBeInTheDocument();
});

it("Hide posts after confirm and shows undo affordance", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { hidden: true }, error: undefined } as never);
  // stub DELETE for unhide (not called in this test, but must resolve cleanly)
  vi.spyOn(clientMod.api, "DELETE").mockResolvedValue({ data: { hidden: false }, error: undefined } as never);
  renderDetail();
  fireEvent.click(await screen.findByRole("button", { name: /hide this call/i }));
  fireEvent.click(screen.getByRole("button", { name: /yes, hide it/i }));
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/calls/{call_id}/hide", {
    params: { path: { call_id: 5 }, query: { profile_id: 7 } },
  }));
  // After hide: undo + back-to-calls buttons appear; no auto-navigate.
  expect(await screen.findByRole("button", { name: /undo/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /back to all calls/i })).toBeInTheDocument();
});

it("Undo calls DELETE unhide and reverts to hide button", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { hidden: true }, error: undefined } as never);
  const del = vi
    .spyOn(clientMod.api, "DELETE")
    .mockResolvedValue({ data: { hidden: false }, error: undefined } as never);
  renderDetail();
  fireEvent.click(await screen.findByRole("button", { name: /hide this call/i }));
  fireEvent.click(screen.getByRole("button", { name: /yes, hide it/i }));
  const undoBtn = await screen.findByRole("button", { name: /undo/i });
  fireEvent.click(undoBtn);
  await waitFor(() => expect(del).toHaveBeenCalledWith("/api/calls/{call_id}/hide", {
    params: { path: { call_id: 5 }, query: { profile_id: 7 } },
  }));
  // After undo: back to initial hide button.
  expect(await screen.findByRole("button", { name: /hide this call/i })).toBeInTheDocument();
});

// -------------------------------------------------------------------------
// Gate 14 (D3) - a decision belongs to a profile. `call_decisions.profile_id`
// is NOT NULL, so with no ?profile= in scope there is nowhere to record a hide:
// the control is DISABLED and no POST is made. The full edition's no-profile
// archive losing the ability to hide is one of the two behaviours this task
// deliberately changes.
// -------------------------------------------------------------------------
it("with no ?profile= the hide button is disabled, hints why, and posts nothing", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { hidden: true }, error: undefined } as never);
  renderDetail("/calls/5");
  await screen.findByRole("heading", { name: "Genomics Grant" });
  const btn = screen.getByRole("button", { name: /hide this call/i });
  expect(btn).toBeDisabled();
  expect(screen.getByText("Pick a profile to hide calls.")).toBeInTheDocument();
  fireEvent.click(btn);
  // TanStack defers mutate() to a microtask, so a synchronous
  // not.toHaveBeenCalled() would pass even on a page that DID fire the request.
  // Flush the queue first, then assert.
  await act(async () => { await Promise.resolve(); });
  expect(post).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: /yes, hide it/i })).not.toBeInTheDocument();
});

it("renders deadline_local_date instead of slicing the UTC string", async () => {
  const withDeadline = {
    ...RANKED,
    call: {
      ...RANKED.call,
      deadline: "2026-09-15T22:00:00Z",
      deadline_local_date: "2026-09-16",
    },
  };
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: withDeadline, error: undefined } as never);
  renderDetail();
  await screen.findByRole("heading", { name: "Genomics Grant" });
  // The local date (2026-09-16) must appear, not the UTC slice (2026-09-15).
  expect(screen.getByText("2026-09-16")).toBeInTheDocument();
  expect(screen.queryByText("2026-09-15")).not.toBeInTheDocument();
});

// -------------------------------------------------------------------------
// Gate 15 — the triage rail sits on another screen, and this page's decisions
// move it. With App.tsx's staleTime of 20 s, a decision made here would
// otherwise leave the feed's ["decision-counts"] query reading its pre-decision
// numbers for up to twenty seconds — measured. `useHideCall`/`useUnhideCall`
// (api/calls.ts) therefore invalidate BOTH keys in their own onSuccess, and
// this page adds nothing per call: a mutate-level callback is dropped when the
// component unmounts before the request lands, and navigating away right after
// an undo is exactly what a reader does. So what is asserted here is the HOOK's
// contract, observed through this page.
// -------------------------------------------------------------------------
it("Undo also invalidates the rail's decision-counts, not only the calls list", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { hidden: true }, error: undefined } as never);
  vi.spyOn(clientMod.api, "DELETE").mockResolvedValue({ data: { hidden: false }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
  renderDetail("/calls/5?profile=7", qc);
  fireEvent.click(await screen.findByRole("button", { name: /hide this call/i }));
  fireEvent.click(screen.getByRole("button", { name: /yes, hide it/i }));
  const undoBtn = await screen.findByRole("button", { name: /undo/i });
  // Discard whatever the HIDE step already invalidated, so what follows is a
  // statement about UNDO alone rather than about the pair of them.
  invalidateSpy.mockClear();
  fireEvent.click(undoBtn);
  await waitFor(() => expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["decision-counts"] }));
  // The calls list is still invalidated too — this is additive, not instead-of.
  expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["calls"] });
});

it("Hide invalidates the rail's decision-counts as well", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { hidden: true }, error: undefined } as never);
  vi.spyOn(clientMod.api, "DELETE").mockResolvedValue({ data: { hidden: false }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
  renderDetail("/calls/5?profile=7", qc);
  fireEvent.click(await screen.findByRole("button", { name: /hide this call/i }));
  invalidateSpy.mockClear();
  fireEvent.click(screen.getByRole("button", { name: /yes, hide it/i }));
  await waitFor(() => expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["decision-counts"] }));
  expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["calls"] });
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

// -------------------------------------------------------------------------
// Gate 16 (detail-page half) — a hide made HERE is a decision like any other,
// and D2 asks for its reason at the moment it is made. The strip is mounted
// beside the Undo, on the {id, profileId} this page already holds in state for
// exactly the same reason the strip holds its own target: the URL can change
// under a late write, and the note must land on the decision it was about.
// -------------------------------------------------------------------------
// The note the server already holds, on the same call. Re-hiding an
// already-hidden call is `hidden -> hidden`, which the upsert's CASE PRESERVES
// the note through -- so this is the state the page is really in when a reader
// arrives from the Hidden view (the card title links straight here) and presses
// "Hide this call".
const HIDDEN_WITH_NOTE = {
  ...RANKED,
  call: { ...RANKED.call, decision_note: "not eligible — materials science" },
};

async function openTheBoxOverAnExistingNote() {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: HIDDEN_WITH_NOTE, error: undefined } as never);
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { hidden: true }, error: undefined } as never);
  const put = vi
    .spyOn(clientMod.api, "PUT")
    .mockResolvedValue({ data: { decision_note: null }, error: undefined } as never);
  renderDetail("/calls/5?profile=7");
  fireEvent.click(await screen.findByRole("button", { name: /hide this call/i }));
  fireEvent.click(screen.getByRole("button", { name: /yes, hide it/i }));
  return { put, box: await screen.findByRole("textbox") };
}

it("shows the note the decision already carries instead of an empty box", async () => {
  const { box } = await openTheBoxOverAnExistingNote();
  expect(box).toHaveValue("not eligible — materials science");
});

it("does not rewrite the note it was seeded with when the box is committed unchanged", async () => {
  // The seed has to reach `committed.current`, not merely the visible value. If
  // it only reached the value, an Enter or a blur over untouched text would PUT
  // the same string back -- harmless here, but it is the same ref that decides
  // whether an IGNORED box is safe, and the ignored case is the common one.
  const { put, box } = await openTheBoxOverAnExistingNote();
  await userEvent.type(box, "{Enter}");
  fireEvent.blur(box);
  await act(async () => { await Promise.resolve(); });
  expect(put).not.toHaveBeenCalled();
});

it("carries the existing note into the write when the reader adds to it", async () => {
  // THE DATA-LOSS GATE. Unseeded, this box is empty over a stored reason, and
  // the first thing typed into it replaces that reason wholesale -- unseen,
  // because the page never showed it. Here the PUT must carry BOTH halves.
  const { put, box } = await openTheBoxOverAnExistingNote();
  await userEvent.type(box, " — asked the PI{Enter}");
  await waitFor(() =>
    expect(put).toHaveBeenCalledWith("/api/calls/{call_id}/note", {
      params: { path: { call_id: 5 }, query: { profile_id: 7 } },
      body: {
        note: "not eligible — materials science — asked the PI",
        expected_state: "hidden",
      },
    }),
  );
});

it("opens empty when the call carries no note, rather than rendering a null", async () => {
  // The NEGATIVE control for the three above: `decision_note: null` is what a
  // fresh hide returns, and it must produce an empty box -- not the string
  // "null", and not the previous call's text.
  const noNote = { ...RANKED, call: { ...RANKED.call, decision_note: null } };
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: noNote, error: undefined } as never);
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { hidden: true }, error: undefined } as never);
  renderDetail("/calls/5?profile=7");
  fireEvent.click(await screen.findByRole("button", { name: /hide this call/i }));
  fireEvent.click(screen.getByRole("button", { name: /yes, hide it/i }));
  expect(await screen.findByRole("textbox")).toHaveValue("");
});

it("offers no note box before anything has been hidden", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  renderDetail();
  await screen.findByRole("heading", { name: "Genomics Grant" });
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
});

it("asks why beside the Undo, and the note reaches the endpoint with the hidden state", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { hidden: true }, error: undefined } as never);
  vi.spyOn(clientMod.api, "DELETE").mockResolvedValue({ data: { hidden: false }, error: undefined } as never);
  const put = vi
    .spyOn(clientMod.api, "PUT")
    .mockResolvedValue({ data: { decision_note: "not eligible" }, error: undefined } as never);
  renderDetail("/calls/5?profile=7");
  fireEvent.click(await screen.findByRole("button", { name: /hide this call/i }));
  fireEvent.click(screen.getByRole("button", { name: /yes, hide it/i }));
  // The Undo is the anchor: the box is beside it, in the same section, and only
  // after the POST has actually succeeded (D10 — never optimistically).
  const undoBtn = await screen.findByRole("button", { name: /undo/i });
  const box = await screen.findByRole("textbox");
  // BESIDE the Undo, in the same region -- not appended somewhere further down
  // the page. `closest("section")` off the box would find the strip's OWN
  // <section>, so the anchor is the labelled region both must share.
  const region = undoBtn.closest('section[aria-label="Hide this call"]');
  expect(region).not.toBeNull();
  expect(region?.contains(box)).toBe(true);
  expect(screen.getByText(/administrator can read/i)).toBeInTheDocument();
  await userEvent.type(box, "not eligible{Enter}");
  await waitFor(() =>
    expect(put).toHaveBeenCalledWith("/api/calls/{call_id}/note", {
      params: { path: { call_id: 5 }, query: { profile_id: 7 } },
      body: { note: "not eligible", expected_state: "hidden" },
    }),
  );
});

// NAMED FOR WHAT IT PROVES, not for the line that inspired it. `setNoteOpen(false)`
// in the Undo handler is correct defence-in-depth, and this test cannot see it:
// deleting that line leaves all of this file green (measured 2026-09-07), because
// Undo clears `hidden`, and the box lives inside the `hidden !== null` branch that
// then unmounts wholesale. What IS pinned is the user-visible guarantee -- no box
// soliciting a reason for a decision that no longer exists (D4) -- which is the
// property that would actually break if the box were moved out of that branch.
it("leaves no box soliciting a reason once the hide is undone (D4)", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { hidden: true }, error: undefined } as never);
  vi.spyOn(clientMod.api, "DELETE").mockResolvedValue({ data: { hidden: false }, error: undefined } as never);
  vi.spyOn(clientMod.api, "PUT").mockResolvedValue({ data: { decision_note: null }, error: undefined } as never);
  renderDetail("/calls/5?profile=7");
  fireEvent.click(await screen.findByRole("button", { name: /hide this call/i }));
  fireEvent.click(screen.getByRole("button", { name: /yes, hide it/i }));
  await screen.findByRole("textbox");
  fireEvent.click(await screen.findByRole("button", { name: /undo/i }));
  await screen.findByRole("button", { name: /hide this call/i });
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
});

// --- PD-31b: THE RELOAD CASE -------------------------------------------------
// Every test above reaches the hidden state by CLICKING. That is the one path
// the page could already handle, because the click is what filled its session
// state. A reader who refreshes, follows a bookmark, or clicks a Hidden card's
// title arrives with no session state at all -- and the page rendered "Hide
// this call" over an already-hidden call, offered no Undo, and had nowhere to
// put the `decision_note` it was being handed.
const HIDDEN_ON_ARRIVAL = {
  ...RANKED,
  call: {
    ...RANKED.call,
    decision_state: "hidden" as const,
    decision_note: "not eligible — materials science",
  },
};

function arriveAt(data: unknown) {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data, error: undefined } as never);
  renderDetail("/calls/5?profile=7");
}

it("knows the call is already hidden on arrival, with no prior click", async () => {
  arriveAt(HIDDEN_ON_ARRIVAL);
  expect(await screen.findByRole("button", { name: /undo/i })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /hide this call/i })).not.toBeInTheDocument();
});

it("shows the note it already carries, on arrival, without being asked", async () => {
  arriveAt(HIDDEN_ON_ARRIVAL);
  expect(await screen.findByText("not eligible — materials science")).toBeInTheDocument();
});

it("offers Add note on arrival at a hidden call that has none", async () => {
  // D12: the strip is deliberately ignorable, so MOST hidden calls arrive here
  // with no note. Offering the editor only where a note already exists makes
  // "add a note later" unreachable on this page.
  arriveAt({ ...HIDDEN_ON_ARRIVAL, call: { ...HIDDEN_ON_ARRIVAL.call, decision_note: null } });
  expect(await screen.findByRole("button", { name: /add note/i })).toBeInTheDocument();
});

it("an undecided call still offers Hide, not Undo", async () => {
  // THE NEGATIVE CONTROL. Without it, a component that simply always rendered
  // Undo would pass all three tests above.
  arriveAt({ ...RANKED, call: { ...RANKED.call, decision_state: null, decision_note: null } });
  expect(await screen.findByRole("button", { name: /hide this call/i })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /undo/i })).not.toBeInTheDocument();
});

it("Undo after arrival wins over the state the server sent", async () => {
  // The server's answer is the SEED, not the truth for the rest of the session:
  // this page's query key is ["call", id, profile], which invalidateQueries
  // (["calls"]) does not prefix-match, so nothing refetches after the DELETE.
  // Deferring to `call.decision_state` forever would put the Undo straight back.
  arriveAt(HIDDEN_ON_ARRIVAL);
  vi.spyOn(clientMod.api, "DELETE").mockResolvedValue({ data: { hidden: false }, error: undefined } as never);
  fireEvent.click(await screen.findByRole("button", { name: /undo/i }));
  expect(await screen.findByRole("button", { name: /hide this call/i })).toBeInTheDocument();
});

it("shows the note it just saved, not the one the page loaded with", async () => {
  // `useSetDecisionNote` invalidates ["calls"]; this page's key is
  // ["call", id, profile], which that does not prefix-match -- so
  // `call.decision_note` stays at its page-load value for the whole session.
  // Without a local override, closing the editor puts the OLD text back on
  // screen and the writer believes their edit was lost.
  arriveAt(HIDDEN_ON_ARRIVAL);
  vi.spyOn(clientMod.api, "PUT").mockResolvedValue({
    data: { decision_note: "budget far too small" }, error: undefined,
  } as never);
  fireEvent.click(await screen.findByRole("button", { name: /not eligible/i }));
  const box = await screen.findByRole("textbox");
  await userEvent.clear(box);
  await userEvent.type(box, "budget far too small{Enter}");
  await act(async () => { await Promise.resolve(); });
  fireEvent.keyDown(box, { key: "Escape" });
  expect(await screen.findByText("budget far too small")).toBeInTheDocument();
  expect(screen.queryByText("not eligible — materials science")).not.toBeInTheDocument();
});
