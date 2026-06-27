import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { AdminGoldenSet } from "./AdminGoldenSet";

afterEach(() => {
  vi.restoreAllMocks();
});

const GS = {
  triples: [
    { id: 7, call_id: 3, profile_id: 4, expected_score: 65, note: "x", call_title: "C1", profile_name: "P" },
  ],
  candidates: [
    { call_id: 9, profile_id: 4, call_title: "Cand", profile_name: "P", aggregate_score: 20, embed_similarity: 0.71, reason: "escalation_disagreement" },
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
