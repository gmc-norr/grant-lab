import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import type { AutoPreview, AutoPreviewQuery, Board, BoardItem } from "../api/boards";
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
  last_reconcile_attempt_at: null,
  last_reconciled_at: null,
  last_reconcile_error: null,
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
  deleteResult,
}: {
  admin?: boolean;
  board?: Board;
  boardError?: { status: number; code: string; message: string };
  approvedItems?: BoardItem[];
  putResult?: unknown;
  publishResult?: unknown;
  reorderResult?: unknown;
  autoPreview?: AutoPreview | ((query: AutoPreviewQuery) => AutoPreview);
  deleteResult?: unknown;
} = {}) {
  const get = vi.spyOn(clientMod.api, "GET").mockImplementation((async (
    path: string,
    opts?: { params?: { query?: AutoPreviewQuery } },
  ) => {
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
    if (path === AUTO_PREVIEW_PATH) {
      // Answering as a function of the query is what lets a test tell a LIVE
      // preview from a stored one: a fixed object comes back identical whether
      // the component forwarded the form's values or ignored them.
      const data =
        typeof autoPreview === "function" ? autoPreview(opts?.params?.query ?? {}) : autoPreview;
      return { data, error: undefined };
    }
    return { data: undefined, error: undefined };
  }) as never);
  const put = vi.spyOn(clientMod.api, "PUT").mockImplementation((async (path: string) => {
    if (path === PUBLISH_PATH) return publishResult ?? { data: { ...board, is_published: true }, error: undefined };
    if (path === ITEMS_PATH) return reorderResult ?? { data: { items: approvedItems }, error: undefined };
    return putResult ?? { data: board, error: undefined };
  }) as never);
  const del = vi
    .spyOn(clientMod.api, "DELETE")
    .mockResolvedValue((deleteResult ?? { data: undefined, error: undefined }) as never);
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
  // Kept in step with `update_board`'s 409 in src/grant_lab/web/api/boards.py.
  // A hand-written fixture cannot redden when the backend's wording moves, so
  // this is the one place the two can drift silently -- the "and each removal
  // also suppresses" clause is the part that was added after removal stopped
  // being a plain delete, and it is what an operator has to read here.
  const overflowMessage =
    "this board has 20 approved calls; the new settings hold 4 — remove 16 first, " +
    "and each removal also suppresses that call, keeping it off this board until " +
    "it is approved again";
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

it("the preview answers for the values in the FORM, not the saved board", async () => {
  // Spec Sec 6 requires this preview to be LIVE: the admin drags the threshold
  // and reads the four numbers BEFORE saving. It used to read only the stored
  // board, so a just-lowered minimum score showed the OLD numbers -- and
  // `truncated`, the one number this preview exists to surface, is precisely
  // the one a lowered threshold changes most.
  //
  // The stub answers as a FUNCTION OF the query, which is what separates a live
  // preview from a stored one here: a fixed fixture comes back identical whether
  // the component forwarded the form's values or dropped them on the floor.
  const user = userEvent.setup();
  renderDetail({
    autoPreview: (q: AutoPreviewQuery) => ({ ...AUTO_PREVIEW, qualifying: q.min_score ?? -1 }),
  });
  await screen.findByText("Corridor board");
  await user.click(screen.getByRole("checkbox", { name: /fill this board automatically/i }));

  // BOARD.min_score is 60, so that is what the first, untouched call asks about.
  await waitFor(() => {
    expect(document.querySelector('[data-field="auto-preview"]')).toHaveTextContent("60 qualify");
  });

  const threshold = screen.getByLabelText(/minimum score/i);
  await user.clear(threshold);
  await user.type(threshold, "40");

  // No Save. The numbers move because the form's value moved.
  await waitFor(() => {
    expect(document.querySelector('[data-field="auto-preview"]')).toHaveTextContent("40 qualify");
  });
  expect(screen.queryByText(/saved settings/i)).toBeNull();
});

it("the preview follows the orientation flip's page size, which only the server derives", async () => {
  // The form deliberately OMITS items_per_page when the user has not touched it,
  // so the server re-derives it from the new orientation. A preview that echoed
  // the stored 8 would describe an 8-up board while Save built a 4-up one.
  //
  // The fix is that the preview is told the ORIENTATION and does the same
  // derivation the PUT does (`_effective_items_per_page`), rather than the
  // client keeping its own copy of the orientation -> page size map. So what
  // this pins is that `items_per_page` is NOT sent and `orientation` IS.
  const user = userEvent.setup();
  const seen: AutoPreviewQuery[] = [];
  renderDetail({
    autoPreview: (q: AutoPreviewQuery) => {
      seen.push(q);
      return AUTO_PREVIEW;
    },
  });
  await screen.findByText("Corridor board");
  await user.click(screen.getByRole("checkbox", { name: /fill this board automatically/i }));
  await waitFor(() => expect(seen.length).toBeGreaterThan(0));

  await user.click(screen.getByRole("radio", { name: /portrait/i }));

  await waitFor(() => {
    const last = seen[seen.length - 1];
    expect(last.orientation).toBe("portrait");
  });
  // Untouched, so the server derives it. Sending the stale 8 here is the defect.
  expect(seen[seen.length - 1].items_per_page).toBeUndefined();
});


it("a failed removal says so, and offers to try again", async () => {
  // REPRODUCED by an outside review: an injected 500 left the card sitting on
  // the board with no message anywhere. This is the control an admin reaches for
  // to take something inappropriate off a PUBLIC wall, so "looks like nothing
  // happened" is the worst answer the screen can give -- the operator walks away
  // believing the card is gone.
  const user = userEvent.setup();
  const { del } = renderDetail({
    approvedItems: [THREE_ITEMS[0]],
    deleteResult: {
      data: undefined,
      error: { error: { code: "internal", message: "Something went wrong." } },
      response: { status: 500 },
    },
  });
  // Wait for the ITEM, not the board title: the items query resolves separately
  // and the Remove button does not exist until it has.
  await screen.findByText("First");
  await user.click(screen.getByRole("button", { name: /remove/i }));

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/couldn't remove that call/i);
  expect(del).toHaveBeenCalledTimes(1);

  // A retry affordance, not just a notice: the failure is usually transient and
  // the operator's next move is to try the same thing again.
  await user.click(within(alert).getByRole("button", { name: /try again/i }));
  await waitFor(() => expect(del).toHaveBeenCalledTimes(2));
});

it("saving settings refetches the QUEUE, whose contents the settings decide", async () => {
  // The approval queue is filtered by the board's own min_score, so a settings
  // save changes which candidates belong in it. Without invalidating it, raising
  // the threshold from 60 to 95 left a score-70 candidate sitting there,
  // approvable, under a board that no longer accepts it.
  const user = userEvent.setup();
  const { get } = renderDetail();
  await screen.findByText("Corridor board");
  const before = get.mock.calls.filter((c) => c[0] === "/api/boards/{board_id}/queue").length;

  await user.click(screen.getByRole("button", { name: /save settings/i }));

  await waitFor(() => {
    const after = get.mock.calls.filter((c) => c[0] === "/api/boards/{board_id}/queue").length;
    expect(after).toBeGreaterThan(before);
  });
});

it("the page-size input takes the value the SERVER derived, not the one it was seeded with", async () => {
  // items_per_page is the one setting this form does not decide: with the field
  // untouched the server derives it from the orientation. After a flip to
  // portrait the board really holds 4 and the input still read the seeded 8 -- a
  // number that was simply false, and which the next save would then send back
  // as an explicit override, undoing the derivation it was showing.
  const user = userEvent.setup();
  renderDetail({
    putResult: { data: { ...BOARD, orientation: "portrait", items_per_page: 4 }, error: undefined },
  });
  await screen.findByText("Corridor board");
  expect(screen.getByLabelText(/items per page/i)).toHaveValue("8");

  await user.click(screen.getByRole("button", { name: /save settings/i }));

  await waitFor(() => expect(screen.getByLabelText(/items per page/i)).toHaveValue("4"));
});

const HOURS_AGO = (n: number) => new Date(Date.now() - n * 3600_000).toISOString();

it("an auto board that has never run says so, and does not invent a date", async () => {
  // NULL is "never". Rendered through a date formatter it becomes 1970, which
  // reads as a board that refreshed once and then died -- the opposite of the
  // truth, and the exact confusion this panel exists to remove.
  renderDetail({ board: { ...BOARD, auto_publish: true } });
  await screen.findByText("Corridor board");
  const line = document.querySelector('[data-field="health-never"]');
  expect(line).toHaveTextContent(/hasn't run yet/i);
  expect(line?.textContent).not.toMatch(/1970|ago/);
});

it("a healthy auto board says when it last refreshed", async () => {
  renderDetail({
    board: {
      ...BOARD,
      auto_publish: true,
      last_reconcile_attempt_at: HOURS_AGO(3),
      last_reconciled_at: HOURS_AGO(3),
    },
  });
  await screen.findByText("Corridor board");
  expect(document.querySelector('[data-field="health-ok"]')).toHaveTextContent(/3 hours ago/i);
});

it("a FAILING board names the error AND when it last actually worked", async () => {
  // The pair is the whole point. "Last refresh failed" on its own does not say
  // whether the wall is showing last night's cards or a fortnight's -- and on a
  // corridor display, that is the only question anyone walking past cares about.
  renderDetail({
    board: {
      ...BOARD,
      auto_publish: true,
      last_reconcile_attempt_at: HOURS_AGO(2),
      last_reconciled_at: HOURS_AGO(72),
      last_reconcile_error: "qr backend down",
    },
  });
  await screen.findByText("Corridor board");
  const alert = document.querySelector('[data-field="health-error"]') as HTMLElement;
  expect(alert).toHaveTextContent(/failed 2 hours ago/i);
  expect(alert).toHaveTextContent(/qr backend down/);
  expect(alert).toHaveTextContent(/last successful refresh: 3 days ago/i);
});

it("a MANUAL board has no refresh panel at all", async () => {
  // Guards the three above: a panel rendered unconditionally would invent a
  // nightly process for a board nobody automated, and "hasn't run yet" would be
  // true forever and mean nothing.
  renderDetail({ board: { ...BOARD, auto_publish: false } });
  await screen.findByText("Corridor board");
  expect(screen.queryByText(/automatic refresh/i)).toBeNull();
  expect(document.querySelector('[data-field="health-never"]')).toBeNull();
});

it("the preview renders the wall at its REAL size, not a mock-up", async () => {
  // This is the panel that would have caught the corridor board growing to
  // 1745px on a 1080px screen: nothing else on this page renders the board at
  // the size it actually runs at, which is why the overflow reached a wall.
  const user = userEvent.setup();
  renderDetail({ board: { ...BOARD, is_published: true, orientation: "landscape" } });
  await screen.findByText("Corridor board");
  await user.click(screen.getByRole("button", { name: /show preview/i }));

  const frame = await screen.findByTitle(/corridor board at 1920x1080/i);
  // The DIMENSIONS are the assertion. A preview at any other size answers a
  // question nobody asked; scaling it down for the panel is presentation.
  expect(frame).toHaveStyle({ width: "1920px", height: "1080px" });
  expect(frame).toHaveAttribute("src", "/b/abc123");
});

it("a portrait board previews at portrait dimensions", async () => {
  // Guards the gate above: a hard-coded 1920x1080 would pass it and then show a
  // portrait wall in the wrong aspect ratio, which is precisely the class of
  // error the preview exists to catch.
  const user = userEvent.setup();
  renderDetail({ board: { ...BOARD, is_published: true, orientation: "portrait" } });
  await screen.findByText("Corridor board");
  await user.click(screen.getByRole("button", { name: /show preview/i }));

  const frame = await screen.findByTitle(/corridor board at 1080x1920/i);
  expect(frame).toHaveStyle({ width: "1080px", height: "1920px" });
});

it("an unpublished board says why it cannot be previewed, rather than showing a blank", async () => {
  // `/b/:slug` reads the PUBLIC endpoint, which 404s while the board is
  // unpublished -- so the iframe would render an honest empty rectangle that
  // looks exactly like a broken board.
  renderDetail({ board: { ...BOARD, is_published: false } });
  await screen.findByText("Corridor board");
  expect(document.querySelector('[data-field="preview-unpublished"]')).toHaveTextContent(
    /publish this board to preview it/i,
  );
  expect(screen.queryByRole("button", { name: /show preview/i })).toBeNull();
});
