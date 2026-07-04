import Collaboration from "@tiptap/extension-collaboration";
import { EditorContent, useEditor } from "@tiptap/react";
import type { components } from "../api/schema.gen";
import { collabEditorExtensions } from "./CitationNode";
import { useCollabDoc } from "./useCollabDoc";

export function CollabEditor(props: {
  workspaceId: string;
  artifact: "draft" | "spec";
  canWrite: boolean;
  /** Citation map used by the Pill tooltip (Task 5). Optional — spec panel omits it. */
  citations?: Record<string, components["schemas"]["CitationOut"]>;
}) {
  const { doc, status } = useCollabDoc(props);
  const editor = useEditor(
    {
      editable: props.canWrite,
      extensions: [
        ...collabEditorExtensions,
        Collaboration.configure({ document: doc, field: "prosemirror" }),
      ],
    },
    [doc], // rebuild the editor if the doc identity changes
  );
  return (
    <div data-collab-status={status}>
      <EditorContent editor={editor} data-testid="collab-editor" />
    </div>
  );
}
