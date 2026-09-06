import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type Board = components["schemas"]["BoardOut"];
export type BoardCreateBody = components["schemas"]["BoardCreateIn"];
export type BoardSettingsBody = components["schemas"]["BoardSettingsIn"];
export type QueueCall = components["schemas"]["QueueCallOut"];
export type BoardQueueResult = components["schemas"]["BoardQueueOut"];
export type BoardItem = components["schemas"]["BoardItemOut"];
export type ApproveBody = components["schemas"]["ApproveIn"];
export type ReorderBody = components["schemas"]["ReorderIn"];

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
    },
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
