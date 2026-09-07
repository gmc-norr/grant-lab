import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import type { Board, BoardItem } from "../api/boards";
import { theme } from "../theme/theme";
import { BoardDetail } from "./BoardDetail";

afterEach(() => {
  vi.restoreAllMocks();
});

const BOARD_PATH = "/api/boards/{board_id}";
const SETTINGS_PATH = "/api/boards/{board_id}";
const PUBLISH_PATH = "/api/boards/{board_id}/publish";
const ITEMS_PATH = "/api/boards/{board_id}/items";
const ITEM_DELETE_PATH = "/api/boards/{board_id}/items/{call_id}";

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

function mockMe(admin: boolean) {
  return {
    data: {
      user: { id: 1, email: "a@umu.se", status: "active", default_profile_id: null },
      capabilities: { admin },
    },
    error: undefined,
  };
}

function renderDetail({
  admin = false,
  board = BOARD,
  boardError,
  approvedItems = [] as BoardItem[],
  putResult,
  publishResult,
  reorderResult,
}: {
  admin?: boolean;
  board?: Board;
  boardError?: { status: number; code: string; message: string };
  approvedItems?: BoardItem[];
  putResult?: unknown;
  publishResult?: unknown;
  reorderResult?: unknown;
} = {}) {
  const get = vi.spyOn(clientMod.api, "GET").mockImplementation((async (path: string) => {
    if (path === "/api/me") return mockMe(admin);
    if (path === BOARD_PATH) {
      if (boardError) {
        return {
          data: undefined,
          error: { error: { code: boardError.code, message: boardError.message } },
          response: { status: boardError.status },
        };
      }
      return { data: board, error: undefined };
    }
    if (path === "/api/boards/{board_id}/queue") {
      return { data: { items: [], waiting_count: 0 }, error: undefined };
    }
    if (path === ITEMS_PATH) return { data: { items: approvedItems }, error: undefined };
    return { data: undefined, error: undefined };
  }) as never);
  const put = vi.spyOn(clientMod.api, "PUT").mockImplementation((async (path: string) => {
    if (path === PUBLISH_PATH) return publishResult ?? { data: { ...board, is_published: true }, error: undefined };
    if (path === ITEMS_PATH) return reorderResult ?? { data: { items: approvedItems }, error: undefined };
    return putResult ?? { data: board, error: undefined };
  }) as never);
  const del = vi.spyOn(clientMod.api, "DELETE").mockResolvedValue({ data: undefined, error: undefined } as never);
  const qc = new QueryClient();
  render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/boards/1"]}>
          <Routes>
            <Route path="/boards/:id" element={<BoardDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
  return { get, put, del };
}

it("a non-admin sees no publish control", async () => {
  renderDetail({ admin: false });
  await screen.findByText("Corridor board");
  // `hidden: true` searches the FULL dom tree, ignoring the accessibility-tree
  // visibility filter role queries apply by default -- Gate B (see the brief):
  // a plain `queryByRole("button", ...)` returns null for BOTH "never
  // rendered" and "rendered with display:none", so it cannot tell a real
  // absence from a control merely hidden with CSS while the server-side gate
  // (Task 6) does the actual work. This query does not get that pass for free.
  expect(screen.queryByRole("button", { name: /publish/i, hidden: true })).toBeNull();
});

it("an admin sees a publish control", async () => {
  renderDetail({ admin: true });
  await screen.findByText("Corridor board");
  expect(screen.getByRole("button", { name: /^publish$/i })).toBeInTheDocument();
});

it("the publish confirmation names what becomes public", async () => {
  const user = userEvent.setup();
  renderDetail({ admin: true, board: { ...BOARD, approved_count: 5 } });
  await screen.findByText("Corridor board");
  await user.click(screen.getByRole("button", { name: /^publish$/i }));
  // The confirmation quotes the CURRENT title and the count that goes public --
  // not a fixed sentence that would say the same thing for every board.
  const confirmation = document.querySelector('[data-field="publish-confirm"]') as HTMLElement;
  expect(confirmation).toHaveTextContent(/corridor board/i);
  expect(confirmation).toHaveTextContent(/5/);
  expect(confirmation).toHaveTextContent(/no login|anyone with the link|public/i);
});

it("shows that the owner cannot be notified when owner_undeliverable is true", async () => {
  renderDetail({ board: { ...BOARD, owner_undeliverable: true } });
  expect(await screen.findByText(/can.t be notified|cannot be notified|won.t be notified/i)).toBeInTheDocument();
});

it("does not show the owner-undeliverable banner when the owner IS reachable", async () => {
  renderDetail({ board: { ...BOARD, owner_undeliverable: false } });
  await screen.findByText("Corridor board");
  expect(screen.queryByText(/can.t be notified|cannot be notified|won.t be notified/i)).toBeNull();
});

it("a board the caller can list but not edit shows a permission message, not a crash", async () => {
  renderDetail({ boardError: { status: 403, code: "forbidden", message: "not authorized" } });
  expect(await screen.findByRole("alert")).toHaveTextContent(/permission|authorized|manage/i);
});

it("an unknown or unreadable board shows not-found messaging", async () => {
  renderDetail({ boardError: { status: 404, code: "not_found", message: "board not found" } });
  expect(await screen.findByRole("alert")).toHaveTextContent(/doesn.t exist|not found/i);
});

it("editing settings saves the whole form, omitting items_per_page since it was never touched", async () => {
  const user = userEvent.setup();
  const { put } = renderDetail();
  await screen.findByText("Corridor board");
  const titleInput = screen.getByLabelText(/title/i);
  await user.clear(titleInput);
  await user.type(titleInput, "New title");
  await user.click(screen.getByRole("button", { name: /save settings/i }));
  await waitFor(() =>
    expect(put).toHaveBeenCalledWith(SETTINGS_PATH, {
      params: { path: { board_id: 1 } },
      body: {
        title: "New title",
        orientation: "landscape",
        min_score: 60,
        page_seconds: 30,
      },
    }),
  );
});

it("IMPORTANT 2: flipping orientation without touching items_per_page omits it from the saved body", async () => {
  // The server treats an EXPLICIT items_per_page as an override that wins
  // over its own orientation-driven default (boards.py:348-353). A landscape
  // board (8/page) flipped to portrait without touching this field must let
  // the server derive the new page size -- sending the stale seeded value
  // would save "portrait" with items_per_page still 8, four implicit rows
  // overflowing the fixed-height corridor wall.
  const user = userEvent.setup();
  const { put } = renderDetail();
  await screen.findByText("Corridor board");
  // Mantine's SegmentedControl hides its radio input from a11y queries in
  // this Mantine version -- click the visible label, this codebase's
  // established pattern (ProfileVisibilityCard.test.tsx, BoardsList.test.tsx).
  await user.click(screen.getByText("Portrait"));
  await user.click(screen.getByRole("button", { name: /save settings/i }));
  await waitFor(() => expect(put).toHaveBeenCalled());
  const [, callArgs] = put.mock.calls[0] as [string, { body: Record<string, unknown> }];
  expect(callArgs.body).not.toHaveProperty("items_per_page");
  expect(callArgs.body).toMatchObject({ orientation: "portrait" });
});

it("IMPORTANT 2: explicitly changing items_per_page still sends it", async () => {
  const user = userEvent.setup();
  const { put } = renderDetail();
  await screen.findByText("Corridor board");
  const itemsInput = screen.getByLabelText(/items per page/i);
  await user.clear(itemsInput);
  await user.type(itemsInput, "5");
  await user.click(screen.getByRole("button", { name: /save settings/i }));
  await waitFor(() =>
    expect(put).toHaveBeenCalledWith(SETTINGS_PATH, {
      params: { path: { board_id: 1 } },
      body: {
        title: "Corridor board",
        orientation: "landscape",
        min_score: 60,
        page_seconds: 30,
        items_per_page: 5,
      },
    }),
  );
});

it("MINOR 5: the items-per-page and page-seconds fields clamp to their bounds and say so", async () => {
  const user = userEvent.setup();
  renderDetail();
  await screen.findByText("Corridor board");
  expect(screen.getByText("1–64")).toBeInTheDocument();
  expect(screen.getByText("0–100")).toBeInTheDocument();
  expect(screen.getByText("1–3600")).toBeInTheDocument();

  const itemsInput = screen.getByLabelText(/items per page/i) as HTMLInputElement;
  await user.clear(itemsInput);
  await user.type(itemsInput, "500");
  await user.tab();
  expect(itemsInput).toHaveValue("64");

  const secondsInput = screen.getByLabelText(/seconds per page/i) as HTMLInputElement;
  await user.clear(secondsInput);
  await user.type(secondsInput, "99999");
  await user.tab();
  expect(secondsInput).toHaveValue("3600");
});

it("a capacity-shrinking settings save shows the 409 message with its numbers intact", async () => {
  const user = userEvent.setup();
  const overflowMessage = "this board has 20 approved calls; the new settings hold 4 — remove 16 first";
  renderDetail({
    putResult: { data: undefined, error: { error: { code: "conflict", message: overflowMessage } } },
  });
  await screen.findByText("Corridor board");
  await user.click(screen.getByRole("button", { name: /save settings/i }));
  expect(await screen.findByText(overflowMessage)).toBeInTheDocument();
});

it("removing an approved item calls DELETE for that call", async () => {
  const user = userEvent.setup();
  const { del } = renderDetail({
    approvedItems: [
      {
        call_id: 42,
        title: "Approved call",
        url: "https://example.test",
        deadline: null,
        source_name: "Vinnova",
        position: 0,
        approved_by: null,
        approved_at: "2026-01-01T00:00:00Z",
      },
    ],
  });
  await screen.findByText("Approved call");
  await user.click(screen.getByRole("button", { name: /remove/i }));
  await waitFor(() =>
    expect(del).toHaveBeenCalledWith(ITEM_DELETE_PATH, {
      params: { path: { board_id: 1, call_id: 42 } },
    }),
  );
});

const THREE_ITEMS: BoardItem[] = [
  {
    call_id: 1, title: "First", url: "https://example.test/1", deadline: null,
    source_name: "Vinnova", position: 0, approved_by: 1, approved_at: "2026-01-01T00:00:00Z",
  },
  {
    call_id: 2, title: "Second", url: "https://example.test/2", deadline: null,
    source_name: "Vinnova", position: 1, approved_by: 1, approved_at: "2026-01-01T00:00:00Z",
  },
  {
    call_id: 3, title: "Third", url: "https://example.test/3", deadline: null,
    source_name: "Vinnova", position: 2, approved_by: 1, approved_at: "2026-01-01T00:00:00Z",
  },
];

it("IMPORTANT 3: moving an item up sends the full reordered list, not a single moved id", async () => {
  // PUT /api/boards/{id}/items is all-or-nothing (schema.gen.ts): it validates
  // duplicates, unknowns and omissions against the COMPLETE set before the
  // first write, so a caller sending only the moved id would be refused.
  const user = userEvent.setup();
  const { put } = renderDetail({ approvedItems: THREE_ITEMS });
  await screen.findByText("Second");
  await user.click(screen.getByRole("button", { name: /move second up/i }));
  await waitFor(() =>
    expect(put).toHaveBeenCalledWith(ITEMS_PATH, {
      params: { path: { board_id: 1 } },
      body: { call_ids: [2, 1, 3] },
    }),
  );
});

it("IMPORTANT 3: the first item cannot move up and the last cannot move down", async () => {
  const { put } = renderDetail({ approvedItems: THREE_ITEMS });
  await screen.findByText("First");
  expect(screen.getByRole("button", { name: /move first up/i })).toBeDisabled();
  expect(screen.getByRole("button", { name: /move third down/i })).toBeDisabled();
  // And the interior buttons that SHOULD work are not disabled by the same rule.
  expect(screen.getByRole("button", { name: /move first down/i })).not.toBeDisabled();
  expect(screen.getByRole("button", { name: /move third up/i })).not.toBeDisabled();
  expect(put).not.toHaveBeenCalled();
});
