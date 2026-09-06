import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import type { Board, QueueCall } from "../api/boards";
import { theme } from "../theme/theme";
import { BoardQueue } from "./BoardQueue";

afterEach(() => {
  vi.restoreAllMocks();
});

const QUEUE_PATH = "/api/boards/{board_id}/queue";
const ITEMS_PATH = "/api/boards/{board_id}/items";

const BOARD: Board = {
  id: 1,
  slug: "abc123",
  title: "Corridor board",
  profile_id: 9,
  orientation: "landscape",
  items_per_page: 8,
  max_pages: 3,
  page_seconds: 30,
  min_score: 60,
  is_published: false,
  created_by: 1,
  published_by: null,
  published_at: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  approved_count: 2,
  owner_undeliverable: false,
};

function call(overrides: Partial<QueueCall>): QueueCall {
  return {
    call_id: 1,
    title: "Call",
    url: "https://example.test/call",
    deadline: null,
    source_name: "Vinnova",
    score: 50,
    already_approved: false,
    ...overrides,
  };
}

function renderQueue({
  items = [] as QueueCall[],
  waiting_count = items.filter((i) => !i.already_approved).length,
  board = BOARD,
  approveResult,
}: {
  items?: QueueCall[];
  waiting_count?: number;
  board?: Board;
  approveResult?: unknown;
} = {}) {
  const get = vi.spyOn(clientMod.api, "GET").mockImplementation((async (path: string) =>
    path === QUEUE_PATH ? { data: { items, waiting_count }, error: undefined } : { data: undefined, error: undefined }) as never);
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue((approveResult ?? { data: { items: [] }, error: undefined }) as never);
  const qc = new QueryClient();
  render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <BoardQueue board={board} />
      </QueryClientProvider>
    </MantineProvider>,
  );
  return { get, post };
}

it("orders candidates by score, highest first", async () => {
  renderQueue({
    items: [
      call({ call_id: 1, title: "Low", score: 40 }),
      call({ call_id: 2, title: "High", score: 90 }),
      call({ call_id: 3, title: "Mid", score: 60 }),
    ],
  });
  const titles = (await screen.findAllByRole("checkbox")).map(
    (el) => el.closest("label")?.textContent ?? "",
  );
  const order = ["High", "Mid", "Low"].map((t) => titles.findIndex((x) => x.includes(t)));
  expect(order).toEqual([...order].sort((a, b) => a - b));
});

it("shows the waiting count", async () => {
  renderQueue({ items: [call({ call_id: 1 })], waiting_count: 12 });
  expect(await screen.findByText(/12/)).toBeInTheDocument();
  expect(screen.getByText(/waiting/i)).toBeInTheDocument();
});

it("approve-everything-at-or-above-N approves the whole band in one request", async () => {
  const user = userEvent.setup();
  const { post } = renderQueue({ items: [call({ call_id: 1, score: 70 })] });
  await screen.findByText(/waiting/i);
  const input = screen.getByLabelText(/approve everything at or above/i);
  await user.clear(input);
  await user.type(input, "75");
  await user.click(screen.getByRole("button", { name: /approve all/i }));
  expect(post).toHaveBeenCalledTimes(1);
  expect(post).toHaveBeenCalledWith(ITEMS_PATH, {
    params: { path: { board_id: 1 } },
    body: { min_score: 75 },
  });
});

it("shows already-approved candidates as approved rather than hiding them", async () => {
  renderQueue({
    items: [
      call({ call_id: 1, title: "Already up", already_approved: true, score: 80 }),
      call({ call_id: 2, title: "Waiting one", already_approved: false, score: 70 }),
    ],
    waiting_count: 1,
  });
  expect(await screen.findByText("Already up")).toBeInTheDocument();
  expect(screen.getByText("Waiting one")).toBeInTheDocument();
  // Not selectable a second time -- but visible, not hidden.
  const row = screen.getByText("Already up").closest("[data-field='queue-row']");
  expect(row).not.toBeNull();
  expect(within(row as HTMLElement).getByText(/approved/i)).toBeInTheDocument();
  expect(within(row as HTMLElement).queryByRole("checkbox")).toBeNull();
});

it("refuses to submit a selection that would exceed capacity, naming the overflow", async () => {
  const user = userEvent.setup();
  const overflowMessage =
    "approving 1 more would put 25 on a board that holds 24 — drop 1 from this selection, or remove 1 already-approved first";
  const { post } = renderQueue({
    items: [call({ call_id: 1, title: "One more", score: 70 })],
    approveResult: {
      data: undefined,
      error: { error: { code: "conflict", message: overflowMessage } },
    },
  });
  await user.click(await screen.findByRole("checkbox", { name: /one more/i }));
  await user.click(screen.getByRole("button", { name: /approve selected/i }));
  expect(post).toHaveBeenCalledTimes(1);
  // The exact server message, numbers intact -- not swapped for a generic one.
  expect(await screen.findByText(overflowMessage)).toBeInTheDocument();
});

it("the approve-at-or-above field clamps to 0-100 and says so in its helper text", async () => {
  // Mantine's NumberInput does not expose min/max as DOM attributes (it is a
  // type="text" input under the hood), so the only real pin is the CLAMPING
  // behaviour itself -- typing past either edge and blurring.
  const user = userEvent.setup();
  renderQueue();
  const input = (await screen.findByLabelText(
    /approve everything at or above/i,
  )) as HTMLInputElement;
  expect(screen.getByText("0–100")).toBeInTheDocument();
  await user.clear(input);
  await user.type(input, "500");
  await user.tab();
  expect(input).toHaveValue("100");
  await user.clear(input);
  await user.type(input, "-5");
  await user.tab();
  expect(input).toHaveValue("0");
});

it("clearing the score field disables the approve-band button instead of arming a 0-threshold approval", async () => {
  const user = userEvent.setup();
  const { post } = renderQueue({ items: [call({ call_id: 1, score: 70 })] });
  const input = await screen.findByLabelText(/approve everything at or above/i);
  await user.clear(input);
  const button = screen.getByRole("button", { name: /approve all/i });
  expect(button).toBeDisabled();
  await user.click(button);
  // MINOR 6: Number("") is 0 and Number.isFinite(0) is true, so an unguarded
  // implementation would post min_score: 0 here -- a whole-queue approval
  // with no confirmation. Nothing may be posted while the field is empty.
  expect(post).not.toHaveBeenCalled();
});

it("clears the selection after approve-at-or-above succeeds, not just after approve-selected", async () => {
  const user = userEvent.setup();
  renderQueue({ items: [call({ call_id: 1, title: "Selectable", score: 70 })] });
  await user.click(await screen.findByRole("checkbox", { name: /selectable/i }));
  expect(screen.getByRole("button", { name: /approve selected \(1\)/i })).toBeInTheDocument();
  const input = screen.getByLabelText(/approve everything at or above/i);
  await user.clear(input);
  await user.type(input, "50");
  await user.click(screen.getByRole("button", { name: /^approve all/i }));
  // MINOR 7: without an onSuccess clearing `selected`, the checked row's id
  // stays in the set after the refetch drops its checkbox, and "Approve
  // selected" keeps showing a stale (1) even though nothing is checked.
  await screen.findByRole("button", { name: /approve selected \(0\)/i });
});

it("shows remaining capacity from the board's stored items_per_page", async () => {
  const portraitBoard: Board = { ...BOARD, orientation: "portrait", items_per_page: 4, max_pages: 3, approved_count: 5 };
  renderQueue({ items: [], board: portraitBoard });
  // cap = 4 * 3 = 12, not the landscape default of 24.
  expect(await screen.findByText(/12/)).toBeInTheDocument();
  expect(screen.queryByText(/24/)).toBeNull();
});
