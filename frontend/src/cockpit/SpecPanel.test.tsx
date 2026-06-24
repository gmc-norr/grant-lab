import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { SpecPanel } from "./SpecPanel";

afterEach(() => {
  vi.restoreAllMocks();
});

function renderPanel() {
  const qc = new QueryClient();
  render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <SpecPanel id="ws-1" />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

const EMPTY = {
  data: {
    head: "a".repeat(40),
    can_edit: true,
    locked: false,
    locked_at: null,
    locked_by: null,
    can_manage_lock: true,
    sections: [{ index: 0, heading: "", text: "", sha: "x" }],
  },
  error: undefined,
};
const SEEDED = {
  data: {
    head: "b".repeat(40),
    can_edit: true,
    locked: false,
    locked_at: null,
    locked_by: null,
    can_manage_lock: true,
    sections: [
      { index: 0, heading: "", text: "", sha: "p" },
      { index: 1, heading: "Aims", text: "## Aims\n\n", sha: "a" },
    ],
  },
  error: undefined,
};

it("empty spec shows the skeleton CTA, then init reveals sections", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue(EMPTY as never);
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue(SEEDED as never);
  renderPanel();
  const cta = await screen.findByRole("button", { name: /Start spec from skeleton/i });
  // After init's onSuccess invalidates ['spec', id], the refetch returns the seeded spec.
  get.mockResolvedValue(SEEDED as never);
  fireEvent.click(cta);
  await waitFor(() => expect(post).toHaveBeenCalled());
  await screen.findByText(/Aims/);
});

it("a non-empty spec renders the editor with a 'Full spec' tab", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(SEEDED as never);
  renderPanel();
  await screen.findByRole("tab", { name: /full spec/i });
});

const LOCKED = {
  data: {
    head: "c".repeat(40),
    can_edit: false,
    locked: true,
    locked_at: "2026-06-20T10:00:00Z",
    locked_by: "owner@ki.se",
    can_manage_lock: true,
    sections: [
      { index: 0, heading: "", text: "", sha: "p" },
      { index: 1, heading: "Aims", text: "## Aims\nbody\n", sha: "a" },
    ],
  },
  error: undefined,
};

it("a locked spec shows the lock banner and an Unlock control for the owner", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(LOCKED as never);
  renderPanel();
  await screen.findByText(/Spec locked by owner@ki.se/i);
  expect(screen.getByRole("button", { name: /unlock spec/i })).toBeInTheDocument();
});

it("an owner of an unlocked, non-empty spec sees a Lock control", async () => {
  const unlocked = { data: { ...SEEDED.data, locked: false, can_manage_lock: true }, error: undefined };
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(unlocked as never);
  renderPanel();
  await screen.findByRole("button", { name: /lock spec/i });
});

it("a member does not see any lock toggle", async () => {
  const memberView = { data: { ...SEEDED.data, locked: false, can_manage_lock: false }, error: undefined };
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(memberView as never);
  renderPanel();
  await screen.findByRole("tab", { name: /full spec/i });
  expect(screen.queryByRole("button", { name: /lock spec/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /unlock spec/i })).not.toBeInTheDocument();
});

it("the empty-spec view shows no lock control", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(EMPTY as never);
  renderPanel();
  await screen.findByRole("button", { name: /start spec from skeleton/i });
  expect(screen.queryByRole("button", { name: /lock spec/i })).not.toBeInTheDocument();
});
