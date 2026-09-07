import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import type { AutoPreview, Board, BoardItem } from "../api/boards";
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
const AUTO_PREVIEW_PATH = "/api/boards/{board_id}/auto-preview";

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
  auto_publish: false,
  is_published: false,
  created_by: 1,
  published_by: null,
  published_at: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  approved_count: 2,
  owner_undeliverable: false,
};

const AUTO_PREVIEW: AutoPreview = {
  qualifying: 142,
  displayed: 24,
  truncated: 118,
  pages: 3,
  loop_seconds: 90,
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
  autoPreview = AUTO_PREVIEW,
}: {
  admin?: boolean;
  board?: Board;
  boardError?: { status: number; code: string; message: string };
  approvedItems?: BoardItem[];
  putResult?: unknown;
  publishResult?: unknown;
  reorderResult?: unknown;
  autoPreview?: AutoPreview;
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
    if (path === AUTO_PREVIEW_PATH) return { data: autoPreview, error: undefined };
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
        auto_publish: false,
        max_pages: 3,
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
        auto_publish: false,
        max_pages: 3,
        items_per_page: 5,
      },
    }),
  );
});

// The bound text a field actually ANNOUNCES, read through its own
// `aria-describedby` rather than found loose in the document. Two fields now
// legitimately carry "1–64" (items per page, and maximum pages), so a bare
// `getByText("1–64")` is ambiguous -- and it was always the weaker check: it
// passed as long as the string existed SOMEWHERE, including on a different
// input entirely.
function descriptionOf(label: RegExp): string | null | undefined {
  const id = screen.getByLabelText(label).getAttribute("aria-describedby");
  return id ? document.getElementById(id)?.textContent : null;
}

it("MINOR 5: the items-per-page and page-seconds fields clamp to their bounds and say so", async () => {
  const user = userEvent.setup();
  renderDetail();
  await screen.findByText("Corridor board");
  expect(descriptionOf(/items per page/i)).toBe("1–64");
  expect(descriptionOf(/minimum score/i)).toBe("0–100");
  expect(descriptionOf(/seconds per page/i)).toBe("1–3600");
  expect(descriptionOf(/maximum pages/i)).toBe("1–64");

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

it("turning the auto-publish switch on sends auto_publish: true", async () => {
  const user = userEvent.setup();
  const { put } = renderDetail();
  await screen.findByText("Corridor board");
  await user.click(screen.getByRole("checkbox", { name: /fill this board automatically/i }));
  await user.click(screen.getByRole("button", { name: /save settings/i }));
  await waitFor(() => expect(put).toHaveBeenCalled());
  const [, callArgs] = put.mock.calls[0] as [string, { body: Record<string, unknown> }];
  expect(callArgs.body).toMatchObject({ auto_publish: true });
});

it("the maximum-pages input sends its value — the only lever that fixes a truncated auto board", async () => {
  // Step 3's whole justification for exposing max_pages is that an admin who
  // cannot raise it cannot fix a board the preview shows as truncated. A UI
  // that renders the truncation and offers no way to act on it ships half the
  // feature, so this asserts the value REACHES the body, not merely that the
  // control exists.
  const user = userEvent.setup();
  const { put } = renderDetail();
  await screen.findByText("Corridor board");
  const maxPagesInput = screen.getByLabelText(/maximum pages/i);
  await user.clear(maxPagesInput);
  await user.type(maxPagesInput, "18");
  await user.click(screen.getByRole("button", { name: /save settings/i }));
  await waitFor(() => expect(put).toHaveBeenCalled());
  const [, callArgs] = put.mock.calls[0] as [string, { body: Record<string, unknown> }];
  expect(callArgs.body).toMatchObject({ max_pages: 18 });
});

it("the maximum-pages input clamps to the API's own 1–64 bounds", async () => {
  // Mirrors the server's ge=1/le=64 so the control cannot send a 422.
  const user = userEvent.setup();
  renderDetail();
  await screen.findByText("Corridor board");
  const maxPagesInput = screen.getByLabelText(/maximum pages/i) as HTMLInputElement;
  await user.clear(maxPagesInput);
  await user.type(maxPagesInput, "900");
  await user.tab();
  expect(maxPagesInput).toHaveValue("64");
});

it("the preview line shows all four numbers, so the cap is never silent", async () => {
  // The load-bearing assertion is `not shown`: a preview rendering only the
  // displayed count would hide exactly the truncation it exists to surface.
  const user = userEvent.setup();
  renderDetail();
  await screen.findByText("Corridor board");
  await user.click(screen.getByRole("checkbox", { name: /fill this board automatically/i }));
  await waitFor(() => {
    const line = document.querySelector('[data-field="auto-preview"]') as HTMLElement;
    expect(line).toHaveTextContent("142 qualify");
    expect(line).toHaveTextContent("24 shown");
    expect(line).toHaveTextContent("118 not shown");
    expect(line).toHaveTextContent("3 pages");
    expect(line).toHaveTextContent("90s loop");
  });
});

it("no preview line is shown while the switch is off", async () => {
  // Guards the guard above: if the line rendered unconditionally, the
  // four-number assertion would pass without the checkbox doing anything.
  renderDetail();
  await screen.findByText("Corridor board");
  expect(document.querySelector('[data-field="auto-preview"]')).toBeNull();
});

it("IMPORTANT 1: saving settings refetches the preview, so it shows the numbers AFTER the save", async () => {
  // useUpdateBoard.onSuccess invalidates ["board", id] and ["boards"] but,
  // before this fix, not ["board-auto-preview", id] -- the key useAutoPreview
  // actually uses (boards.ts). Nothing else in the app invalidates it either,
  // so an admin who raises Maximum pages specifically to fix the truncation
  // this line reports would keep seeing the PRE-save numbers for the rest of
  // the session (App.tsx's 20s staleTime, no refetchOnWindowFocus, and
  // SettingsForm mounts unkeyed under BoardDetail's `key={board.data.id}`).
  //
  // The GET mock below answers the auto-preview path differently before and
  // after the PUT resolves, so this asserts the line's CONTENT changes, not
  // merely that a refetch fired -- `findByText` would only wait for the
  // (always-mounted-once-the-switch-is-on) element to appear, which races the
  // stale render and can pass against the OLD numbers.
  const user = userEvent.setup();
  let saved = false;
  const AFTER_SAVE: AutoPreview = { qualifying: 142, displayed: 48, truncated: 94, pages: 6, loop_seconds: 180 };
  vi.spyOn(clientMod.api, "GET").mockImplementation((async (path: string) => {
    if (path === "/api/me") return mockMe(false);
    if (path === BOARD_PATH) return { data: BOARD, error: undefined };
    if (path === "/api/boards/{board_id}/queue") return { data: { items: [], waiting_count: 0 }, error: undefined };
    if (path === ITEMS_PATH) return { data: { items: [] }, error: undefined };
    if (path === AUTO_PREVIEW_PATH) return { data: saved ? AFTER_SAVE : AUTO_PREVIEW, error: undefined };
    return { data: undefined, error: undefined };
  }) as never);
  vi.spyOn(clientMod.api, "PUT").mockImplementation((async (path: string) => {
    if (path === SETTINGS_PATH) {
      saved = true;
      return { data: { ...BOARD, auto_publish: true, max_pages: 6 }, error: undefined };
    }
    return { data: BOARD, error: undefined };
  }) as never);
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
  await screen.findByText("Corridor board");
  await user.click(screen.getByRole("checkbox", { name: /fill this board automatically/i }));
  await waitFor(() => {
    const line = document.querySelector('[data-field="auto-preview"]') as HTMLElement;
    expect(line).toHaveTextContent("118 not shown");
  });
  await user.click(screen.getByRole("button", { name: /save settings/i }));
  await waitFor(() => {
    const line = document.querySelector('[data-field="auto-preview"]') as HTMLElement;
    expect(line).toHaveTextContent("94 not shown");
  });
});

it("turning the switch on warns that the existing cards will be replaced", async () => {
  const user = userEvent.setup();
  renderDetail({ board: { ...BOARD, approved_count: 7 } });
  await screen.findByText("Corridor board");
  await user.click(screen.getByRole("checkbox", { name: /fill this board automatically/i }));
  const warning = await screen.findByText(/replaces the 7 cards/i);
  expect(warning).toBeInTheDocument();
});

it("a board that is ALREADY auto-publishing gets no replacement warning", async () => {
  // The warning is about the transition, not the state: re-saving settings on
  // a board that has been auto-publishing for weeks replaces nothing a human
  // put there.
  renderDetail({ board: { ...BOARD, auto_publish: true, approved_count: 7 } });
  await screen.findByText("Corridor board");
  expect(screen.queryByText(/replaces the 7 cards/i)).toBeNull();
});

it("the auto-fill switch does not borrow the publish control's verb", async () => {
  // IMPORTANT 4. Two elements apart, PublishControl's button says Publish and
  // its confirmation says "It becomes visible to anyone with the link". The
  // two acts are different -- auto-publish FILLS a board, it never makes one
  // world-readable -- and this is the one place the spec insists they stay
  // visibly distinct. Rendered as ADMIN so the real publish button is on the
  // page at the same time: the point is that the two controls read
  // differently side by side, which a non-admin render cannot show.
  renderDetail({ admin: true });
  await screen.findByText("Corridor board");
  const auto = screen.getByRole("checkbox", { name: /fill this board automatically/i });
  expect(auto).toBeInTheDocument();
  const autoLabel = auto.closest(".mantine-Checkbox-root")?.textContent ?? "";
  expect(autoLabel).not.toMatch(/publish/i);
  expect(screen.getByRole("button", { name: /^publish$/i })).toBeInTheDocument();
});

it("the preview line says it describes the SAVED settings", async () => {
  // The endpoint reads the stored board, not the values sitting in the form,
  // so an admin who has just typed a new minimum score is looking at the old
  // numbers. Making the preview live is a product call that stays open; saying
  // which settings it describes is the honest half, and this pins it.
  const user = userEvent.setup();
  renderDetail();
  await screen.findByText("Corridor board");
  await user.click(screen.getByRole("checkbox", { name: /fill this board automatically/i }));
  await waitFor(() => {
    const basis = document.querySelector('[data-field="auto-preview-basis"]') as HTMLElement;
    expect(basis).toHaveTextContent(/saved settings/i);
  });
});
