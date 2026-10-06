import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components, paths } from "./schema.gen";

export type Board = components["schemas"]["BoardOut"];
export type BoardCreateBody = components["schemas"]["BoardCreateIn"];
export type BoardSettingsBody = components["schemas"]["BoardSettingsIn"];
export type QueueCall = components["schemas"]["QueueCallOut"];
export type BoardQueueResult = components["schemas"]["BoardQueueOut"];
export type BoardItem = components["schemas"]["BoardItemOut"];
export type ApproveBody = components["schemas"]["ApproveIn"];
export type ReorderBody = components["schemas"]["ReorderIn"];
export type AutoPreview = components["schemas"]["AutoPreviewOut"];
// The what-if settings the preview answers for. Derived from the generated
// schema rather than hand-written, so a parameter renamed or dropped
// server-side is a tsc error here instead of a query string the backend
// silently ignores -- which would look exactly like a preview that works.
export type AutoPreviewQuery = NonNullable<
  paths["/api/boards/{board_id}/auto-preview"]["get"]["parameters"]["query"]
>;

// --- boards list + CRUD ----------------------------------------------------
//
// GET /api/boards is READ-gated (any profile the caller can read); GET
// /api/boards/{id}, PUT and DELETE are EDIT-gated (profiles.py's
// `_profile_for_edit` rule, reused as-is). So a board can legitimately be
// LISTED and then 403 when opened -- BoardDetail has to handle that, the list
// itself cannot promise every row opens.

export function useBoardsList() {
  return useQuery({
    queryKey: ["boards"],
    queryFn: async () => unwrap(await api.GET("/api/boards", {})),
    retry: false,
  });
}

export function useBoard(id: number) {
  return useQuery({
    queryKey: ["board", id],
    queryFn: async () =>
      unwrap(await api.GET("/api/boards/{board_id}", { params: { path: { board_id: id } } })),
    retry: false,
  });
}

export function useCreateBoard() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: BoardCreateBody) => unwrap(await api.POST("/api/boards", { body })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["boards"] }),
  });
}

export function useUpdateBoard(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: BoardSettingsBody) =>
      unwrap(
        await api.PUT("/api/boards/{board_id}", { params: { path: { board_id: id } }, body }),
      ),
    onSuccess: (data) => {
      qc.setQueryData(["board", id], data);
      qc.invalidateQueries({ queryKey: ["boards"] });
      // The auto-preview line (`useAutoPreview` below) is keyed on its own
      // ["board-auto-preview", id] and neither prefix above matches it, so
      // without this it would keep showing the pre-save numbers -- e.g. the
      // "not shown" count that raising max_pages was meant to fix -- for the
      // rest of the session (App.tsx's 20s staleTime + no window-focus
      // refetch, and SettingsForm is mounted unkeyed in BoardDetail).
      qc.invalidateQueries({ queryKey: ["board-auto-preview", id] });
      // The APPROVAL QUEUE is filtered by the board's own `min_score`, so the
      // settings that just changed decide which candidates belong in it. Without
      // this, raising the threshold from 60 to 95 left a score-70 candidate
      // sitting in the queue, approvable, under a board that no longer accepts
      // it -- the screen showing two different answers to the same question.
      qc.invalidateQueries({ queryKey: ["board-queue", id] });
    },
  });
}

// What an auto board WOULD carry right now: qualifying, displayed, truncated,
// pages, loop_seconds. Four numbers rather than one, because a single count
// cannot distinguish "nothing qualifies" from "plenty qualifies and the cap is
// silently dropping most of it" -- and the second is the failure the preview
// exists to surface. Edit-gated like the rest of /api/boards/{id}, and
// `enabled` so the settings form does not fetch it while the switch is off.
// `query` carries the settings the admin has typed but not saved. Omitted, the
// endpoint falls back to the stored board, so a bare call still describes the
// saved settings. It is part of the queryKey because two different what-ifs are
// two different answers; sharing one cache entry would show the previous
// question's numbers under the current one's inputs.
export function useAutoPreview(
  id: number,
  opts?: { enabled?: boolean; query?: AutoPreviewQuery },
) {
  const query = opts?.query ?? {};
  return useQuery({
    queryKey: ["board-auto-preview", id, query],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/boards/{board_id}/auto-preview", {
          params: { path: { board_id: id }, query },
        }),
      ),
    enabled: opts?.enabled ?? true,
    retry: false,
  });
}

export function usePublishBoard(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (is_published: boolean) =>
      unwrap(
        await api.PUT("/api/boards/{board_id}/publish", {
          params: { path: { board_id: id } },
          body: { is_published },
        }),
      ),
    onSuccess: (data) => {
      qc.setQueryData(["board", id], data);
      qc.invalidateQueries({ queryKey: ["boards"] });
    },
  });
}

// --- the approval queue -----------------------------------------------------
//
// waiting_count is the count of PENDING candidates only -- already-approved
// rows are returned (flagged already_approved) but excluded from it, so it is
// never a pagination total (boards/queue.py's own contract, Task 4).
//
// limit=200 (the route's own ceiling, boards.py:492) rather than the default
// 50: the whole point of the checklist affordance is seeing everything at
// once, not paging through a triage queue call by call.
export const QUEUE_PAGE_SIZE = 200;

// web/api/boards.py MAX_BOARD_TITLE_CHARS (review 2026-10-06 M10). The server
// answers 422 past it; the inputs stop there first. HTML counts UTF-16 units,
// Python code points, so the input is at most stricter, never looser.
export const BOARD_TITLE_MAX_CHARS = 200;

export function useBoardQueue(boardId: number) {
  return useQuery({
    queryKey: ["board-queue", boardId],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/boards/{board_id}/queue", {
          params: { path: { board_id: boardId }, query: { limit: QUEUE_PAGE_SIZE } },
        }),
      ),
    retry: false,
  });
}

export function useBoardItems(boardId: number) {
  return useQuery({
    queryKey: ["board-items", boardId],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/boards/{board_id}/items", {
          params: { path: { board_id: boardId } },
        }),
      ),
    retry: false,
  });
}

// Bulk approve -- EITHER a checklist selection OR a score floor (ApproveBody's
// own model_validator refuses both/neither), never a per-call POST: a per-call
// approval costs a minute per call and an abandoned gate is worse than no gate.
export function useApproveItems(boardId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: ApproveBody) =>
      unwrap(
        await api.POST("/api/boards/{board_id}/items", {
          params: { path: { board_id: boardId } },
          body,
        }),
      ),
    onSuccess: (data) => {
      qc.setQueryData(["board-items", boardId], data);
      qc.invalidateQueries({ queryKey: ["board-queue", boardId] });
      qc.invalidateQueries({ queryKey: ["board", boardId] });
      qc.invalidateQueries({ queryKey: ["boards"] });
    },
  });
}

// Reorder -- all-or-nothing (Task 6's own contract): `call_ids` must be the
// COMPLETE permutation of every approved item's id, validated against
// duplicates/unknowns/omissions before the first write. Never send a single
// moved id; the caller builds the whole new order.
export function useReorderItems(boardId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (call_ids: number[]) =>
      unwrap(
        await api.PUT("/api/boards/{board_id}/items", {
          params: { path: { board_id: boardId } },
          body: { call_ids },
        }),
      ),
    onSuccess: (data) => {
      qc.setQueryData(["board-items", boardId], data);
    },
  });
}

export function useUnapproveItem(boardId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (callId: number) =>
      unwrap(
        await api.DELETE("/api/boards/{board_id}/items/{call_id}", {
          params: { path: { board_id: boardId, call_id: callId } },
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["board-items", boardId] });
      qc.invalidateQueries({ queryKey: ["board-queue", boardId] });
      qc.invalidateQueries({ queryKey: ["board", boardId] });
      qc.invalidateQueries({ queryKey: ["boards"] });
    },
  });
}
