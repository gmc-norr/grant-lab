import { Alert, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "../api/client";
import { BoardPresentation } from "./BoardScreen";

export function BoardDraftPreview({ boardId }: { boardId: number }) {
  const preview = useQuery({
    queryKey: ["board-preview", boardId],
    queryFn: async () => unwrap(await api.GET("/api/boards/{board_id}/preview", { params: { path: { board_id: boardId } } })),
    retry: false,
    gcTime: 0,
    staleTime: 0,
  });
  return <div style={{ position: "fixed", inset: 0, zIndex: 500, background: "var(--app-editor)" }}>
    {preview.isPending ? <Text p="md">Loading preview...</Text> : preview.isError ? <Alert role="alert" color="red">Preview unavailable. Check your access and try again.</Alert> : <BoardPresentation board={preview.data} />}
  </div>;
}