import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import type { Board } from "../api/boards";
import { theme } from "../theme/theme";
import { BoardsList } from "./BoardsList";

afterEach(() => {
  vi.restoreAllMocks();
});

const BOARDS_PATH = "/api/boards";
const PROFILES_PATH = "/api/profiles";

const PROFILE = { id: 9, name: "Digital Pathology Lab", kind: "organizational_unit", level: null, areas_count: 0, budget_currency: null, description: "" };

const BOARD: Board = {
  id: 1, slug: "abc123", title: "Corridor board", profile_id: 9, orientation: "landscape",
  items_per_page: 8, max_pages: 3, page_seconds: 30, min_score: 60, is_published: true,
  created_by: 1, published_by: 1, published_at: "2026-01-01T00:00:00Z",
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  approved_count: 3, owner_undeliverable: false,
};

function LocationProbe() {
  return <span data-testid="loc">{useLocation().pathname}</span>;
}

function renderList({ boards = [] as Board[], createResult }: { boards?: Board[]; createResult?: unknown } = {}) {
  const get = vi.spyOn(clientMod.api, "GET").mockImplementation((async (path: string) => {
    if (path === BOARDS_PATH) return { data: { boards }, error: undefined };
    if (path === PROFILES_PATH) return { data: { profiles: [PROFILE] }, error: undefined };
    return { data: undefined, error: undefined };
  }) as never);
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue((createResult ?? { data: { ...BOARD, id: 42 }, error: undefined }) as never);
  const qc = new QueryClient();
  render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/boards"]}>
          <Routes>
            <Route path="/boards" element={<><BoardsList /><LocationProbe /></>} />
            <Route path="/boards/:id" element={<LocationProbe />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
  return { get, post };
}

it("renders a row with a link for each board", async () => {
  renderList({ boards: [BOARD] });
  const link = await screen.findByRole("link", { name: /corridor board/i });
  expect(link).toHaveAttribute("href", "/boards/1");
});

it("shows an empty state when there are no boards", async () => {
  renderList({ boards: [] });
  expect(await screen.findByText(/no boards yet/i)).toBeInTheDocument();
});

it("requires a title and a profile before submitting", async () => {
  const user = userEvent.setup();
  const { post } = renderList();
  await user.click(screen.getByRole("button", { name: /create board/i }));
  expect(post).not.toHaveBeenCalled();
});

it("creating a board POSTs the form and navigates to its detail page", async () => {
  const user = userEvent.setup();
  const { post } = renderList();
  await user.type(screen.getByLabelText(/title/i), "New wall");
  await user.selectOptions(screen.getByLabelText(/profile/i), "9");
  await user.click(screen.getByText("Portrait"));
  await user.click(screen.getByRole("button", { name: /create board/i }));
  expect(post).toHaveBeenCalledWith(BOARDS_PATH, {
    body: { title: "New wall", profile_id: 9, orientation: "portrait" },
  });
  expect(await screen.findByTestId("loc")).toHaveTextContent("/boards/42");
});
