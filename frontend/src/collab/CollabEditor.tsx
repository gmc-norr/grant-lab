import Collaboration from "@tiptap/extension-collaboration";
import { CollaborationCaret } from "@tiptap/extension-collaboration-caret";
import { EditorContent, useEditor } from "@tiptap/react";
import { useEffect } from "react";
import type * as Y from "yjs";
import { useSession } from "../api/session";
import { resetEditorStore } from "../cockpit/editorStore";
import { collabEditorExtensions } from "./CitationNode";
import { colorForUserId, displayName } from "./collabColor";
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
  const { data: me } = useSession();
  const label = displayName(me?.user);
  const color = colorForUserId(me?.user?.id ?? 0);
  const editor = useEditor(
    {
      editable: props.canWrite,
      extensions: [
        ...collabEditorExtensions,
        Collaboration.configure({ document: props.doc, field: "prosemirror" }),
        // Seed with whatever identity we have now; the effect below corrects it
        // if useSession() resolves after mount (R2-1). Added for writers AND
        // read-only viewers (they see peers' carets and broadcast their own).
        CollaborationCaret.configure({ provider: props.provider, user: { name: label, color } }),
      ],
    },
    [props.doc], // editor keyed ONLY on the doc — NOT rebuilt on identity change
  );

  // R2-1: useEditor's dep list is [props.doc], so a late useSession() result would
  // freeze the fallback name/color. Push identity through the sanctioned v3 command
  // whenever it resolves/changes, instead of rebuilding the editor (which would
  // destroy the caret plugin state).
  useEffect(() => {
    if (editor && me?.user) editor.commands.updateUser({ name: label, color });
  }, [editor, label, color, me?.user]);

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
