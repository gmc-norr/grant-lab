import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import type { AdminGoldenSet as AdminGoldenSetOut } from "../api/adminGoldenSet";
import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { AdminGoldenSet } from "./AdminGoldenSet";

afterEach(() => {
  vi.restoreAllMocks();
});

// TYPED, not inferred. An untyped literal here is the same hole an `as CallItem`
// cast opened one file over: a field added to CandidateOut would be missing from
// this mock with no tsc error, and the component would read undefined. Measured
// 2026-09-07: with the annotation and `user_notes` removed, tsc names it --
// `TS2741: Property 'user_notes' is missing`. Without it, silence.
const GS: AdminGoldenSetOut = {
  triples: [
    { id: 7, call_id: 3, profile_id: 4, expected_score: 65, note: "x", call_title: "C1", profile_name: "P" },
  ],
  candidates: [
    // `user_notes` is REQUIRED on CandidateOut (it defaults to [] server-side,
    // which is a default for the WRITER, not a licence for the reader). This
    // mock is typed `unknown`, so tsc will not tell you when it stops
    // resembling the wire -- a fixture missing the field renders `undefined`
    // and the row throws on `.length`.
    { call_id: 9, profile_id: 4, call_title: "Cand", profile_name: "P", aggregate_score: 20, embed_similarity: 0.71, reason: "escalation_disagreement", user_notes: [] },
  ],
};

function mockGs(data: unknown = GS) {
  return vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data, error: undefined } as never);
}

it("renders triples and candidates", async () => {
  mockGs();
  renderWithProviders(<AdminGoldenSet />);
  expect(await screen.findByText("C1")).toBeInTheDocument();
  expect(screen.getByText("Cand")).toBeInTheDocument();
  expect(screen.getByText("escalation_disagreement")).toBeInTheDocument();
});

it("deletes a triple", async () => {
  mockGs();
  const del = vi.spyOn(clientMod.api, "DELETE").mockResolvedValue({ data: undefined, error: undefined } as never);
  renderWithProviders(<AdminGoldenSet />);
  await userEvent.click(await screen.findByRole("button", { name: /delete/i }));
  await waitFor(() =>
    expect(del).toHaveBeenCalledWith("/api/admin/golden-set/{triple_id}", { params: { path: { triple_id: 7 } } }),
  );
});

it("adds a triple from the manual form", async () => {
  mockGs();
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 1 }, error: undefined } as never);
  renderWithProviders(<AdminGoldenSet />);
  await userEvent.type(await screen.findByPlaceholderText("call_id"), "3");
  await userEvent.type(screen.getByPlaceholderText("profile_id"), "4");
  await userEvent.type(screen.getByPlaceholderText(/expected score/i), "65");
  await userEvent.click(screen.getByRole("button", { name: /^add$/i }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith("/api/admin/golden-set", {
      body: { call_id: 3, profile_id: 4, expected_score: 65, note: undefined },
    }),
  );
});

it("adds a candidate inline with its score", async () => {
  mockGs();
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 2 }, error: undefined } as never);
  renderWithProviders(<AdminGoldenSet />);
  await userEvent.type(await screen.findByPlaceholderText("0–100"), "80");
  await userEvent.click(screen.getByRole("button", { name: /add candidate/i }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith("/api/admin/golden-set", {
      body: { call_id: 9, profile_id: 4, expected_score: 80, note: undefined },
    }),
  );
});

// The server's rejection, as unwrap() receives it from openapi-fetch.
function rejected(status: number, message: string) {
  return { data: undefined, error: { error: { code: "bad_request", message } }, response: { status } } as never;
}

it("a rejected manual add says why and keeps what was typed", async () => {
  mockGs();
  vi.spyOn(clientMod.api, "POST").mockResolvedValue(rejected(400, "unknown call or profile"));
  renderWithProviders(<AdminGoldenSet />);
  await userEvent.type(await screen.findByPlaceholderText("call_id"), "999999");
  await userEvent.type(screen.getByPlaceholderText("profile_id"), "4");
  await userEvent.type(screen.getByPlaceholderText(/expected score/i), "80");
  await userEvent.click(screen.getByRole("button", { name: /^add$/i }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't add the triple: unknown call or profile");
  expect(screen.getByPlaceholderText("call_id")).toHaveValue(999999);
});

it("a rejected candidate add says why in that row", async () => {
  mockGs();
  vi.spyOn(clientMod.api, "POST").mockResolvedValue(rejected(400, "unknown call or profile"));
  renderWithProviders(<AdminGoldenSet />);
  await userEvent.type(await screen.findByPlaceholderText("0–100"), "80");
  await userEvent.click(screen.getByRole("button", { name: /add candidate/i }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't add the candidate: unknown call or profile");
});

it("a failed delete says why", async () => {
  mockGs();
  vi.spyOn(clientMod.api, "DELETE").mockResolvedValue(rejected(404, "triple not found"));
  renderWithProviders(<AdminGoldenSet />);
  await userEvent.click(await screen.findByRole("button", { name: /delete/i }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't delete the triple: triple not found");
});

// -------------------------------------------------------------------------
// Gate 17 (D3, D7) — the human's reason, beside the score they disagreed with.
// The whole point of collecting a note is that a curator building the golden
// set can see WHY a researcher dismissed a call the ranker scored highly. The
// mining query already carries them (Task 4); until they are rendered the
// feature has no consumer at all.
//
// UNATTRIBUTED, and there is nothing to leave out: the endpoint sends bare
// strings, with no user and no timestamp to render even by accident (D3).
// -------------------------------------------------------------------------
function withNotes(user_notes: string[]) {
  return {
    ...GS,
    candidates: [{ ...GS.candidates[0], user_notes }],
  };
}

it("renders every note the candidate carries, beside its mining reason", async () => {
  mockGs(withNotes(["Wrong disease area entirely.", "Deadline already gone."]));
  renderWithProviders(<AdminGoldenSet />);
  expect(await screen.findByText("Wrong disease area entirely.")).toBeInTheDocument();
  expect(screen.getByText("Deadline already gone.")).toBeInTheDocument();
  // In the SAME cell as the reason: a note two columns away from the signal it
  // explains is a second table, not an annotation.
  const reason = screen.getByText("escalation_disagreement");
  const cell = reason.closest("td");
  expect(cell).not.toBeNull();
  expect(cell?.textContent).toContain("Wrong disease area entirely.");
});

it("renders no notes element at all when the candidate has none", async () => {
  mockGs(withNotes([]));
  const { container } = renderWithProviders(<AdminGoldenSet />);
  // Wait for the table first: an absence assertion on a page still loading
  // passes for free.
  expect(await screen.findByText("Cand")).toBeInTheDocument();
  expect(container.querySelector("[data-user-notes]")).toBeNull();
});

it("attaches no author and no date to a note (D3)", async () => {
  // The disclosure the researcher was shown says their name is not attached.
  // The mined row carries no user_id and no timestamp, so there is nothing to
  // render -- this pins that the row does not invent one from the candidate's
  // own fields either.
  mockGs(withNotes(["Not our field."]));
  const { container } = renderWithProviders(<AdminGoldenSet />);
  await screen.findByText("Not our field.");
  const notes = container.querySelector("[data-user-notes]");
  expect(notes).not.toBeNull();
  expect(notes?.textContent).toBe("Not our field.");
});
