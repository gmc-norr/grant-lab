import Collaboration from "@tiptap/extension-collaboration";
import { EditorContent, useEditor } from "@tiptap/react";
import { useEffect } from "react";
import type * as Y from "yjs";
import { resetEditorStore } from "../cockpit/editorStore";
import { collabEditorExtensions } from "./CitationNode";
import type { CollabProvider, ProviderStatus } from "./collabProvider";
import { SaveVersionButton } from "./SaveVersionButton";
import { useCollabDoc } from "./useCollabDoc";

type CollabEditorProps = {
  workspaceId: string;
  artifact: "draft" | "spec";
  canWrite: boolean;
};

export function CollabEditor(props: CollabEditorProps) {
  const { doc, provider, status } = useCollabDoc(props);
  // Clear the shared editor store on unmount so a stale collab save-status
  // (e.g. "Synced"/"Read-only") does not leak into the StatusBar of the next
  // cockpit tab/view — mirrors the CM6 ArtifactSectionEditor's reset.
  useEffect(() => () => resetEditorStore(), []);
  // doc/provider are null on the first render (built in the effect); render a
  // placeholder so the inner editor's useEditor always gets a live Y.Doc.
  if (!doc || !provider) {
    return (
      <div data-collab-status={status} data-testid="collab-connecting">
        Connecting…
      </div>
    );
  }
  return <CollabEditorInner {...props} doc={doc} provider={provider} status={status} />;
}

function CollabEditorInner(
  props: CollabEditorProps & { doc: Y.Doc; provider: CollabProvider; status: ProviderStatus },
) {
  const editor = useEditor(
    {
      editable: props.canWrite,
      extensions: [
        ...collabEditorExtensions,
        Collaboration.configure({ document: props.doc, field: "prosemirror" }),
      ],
    },
    [props.doc], // rebuild the editor if the doc identity changes
  );
  return (
    <div data-collab-status={props.status}>
      <SaveVersionButton
        id={props.workspaceId}
        artifact={props.artifact}
        provider={props.provider}
        canWrite={props.canWrite}
        status={props.status}
      />
      <EditorContent editor={editor} data-testid="collab-editor" />
    </div>
  );
}
