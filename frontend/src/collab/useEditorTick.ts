import type { Editor } from "@tiptap/core";
import { useEffect, useReducer } from "react";

/** Re-render on every editor transaction so isActive()/can() stay fresh. */
export function useEditorTick(editor: Editor | null) {
  const [, bump] = useReducer((n: number) => n + 1, 0);
  useEffect(() => {
    if (!editor) return;
    editor.on("transaction", bump);
    return () => {
      editor.off("transaction", bump);
    };
  }, [editor]);
}
