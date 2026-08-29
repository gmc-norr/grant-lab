import { Group, Text } from "@mantine/core";
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
import styles from "./CollabEditor.module.css";
import { EditorToolbar } from "./EditorToolbar";
import { SaveVersionButton } from "./SaveVersionButton";
import { InsertTableButton, TableMenu } from "./TableControls";
import { TiptapSelectionToolbar } from "./TiptapSelectionToolbar";
import { useCollabDoc } from "./useCollabDoc";
import { useSectionJump } from "./useSectionJump";

type CollabEditorProps = {
  workspaceId: string;
  artifact: "draft" | "spec";
  canWrite: boolean;
  /** Panel-level actions rendered at the END of the toolbar. */
  actions?: React.ReactNode;
  /** Optional props for the draft selection toolbar (draft-only) */
  head?: string;
  sections?: { sha: string; text: string }[];
  canEdit?: boolean;
};

export function CollabEditor(props: CollabEditorProps) {
  const { doc, provider, status, synced } = useCollabDoc(props);
  const { data: me } = useSession();
  // Clear the shared editor store on unmount so a stale collab save-status
  // (e.g. "Synced"/"Read-only") does not leak into the StatusBar of the next
  // cockpit tab/view — mirrors the CM6 ArtifactSectionEditor's reset.
  useEffect(() => () => resetEditorStore(), []);
  // Gate the editor mount on a RESOLVED identity as well as the live doc/provider.
  // Mounting CollaborationCaret before useSession() resolves would seed and
  // broadcast an "Anonymous" fallback caret for one render (Slice 3b-5b #1). The
  // doc/provider are still built + connected above, so sync is not delayed; only the
  // editable surface waits for identity — normally instant (me is app-shell cached),
  // and the cockpit's auth gate guarantees me resolves for anyone who reaches it.
  // The provider retries on its own; this only decides what the user is told.
  // Saying nothing is the worst outcome — a silent blank page reads as data loss.
  const unreachable = status === "offline" || status === "closed-policy";
  // `synced` — NOT `status`. A connected socket that has not yet delivered the
  // document leaves the Y.Doc empty, and CollabEditorInner renders that empty
  // doc as a blank page: indistinguishable from a draft whose text is gone.
  // Observed for real — the room held 4,347 bytes and the Outline listed seven
  // sections while the editor showed nothing at all.
  //
  // `synced` latches on the first sync frame, so an EMPTY room still unblocks
  // (empty is an answer), and a later disconnect does not throw the user out of
  // an editor whose content they already have.
  if (!doc || !provider || !me?.user || !synced) {
    // The `actions` slot must survive this branch. Both panels pass their
    // panel-level button (Export on draft, Check coherence on spec) ONLY as
    // `actions` once collab is on, and neither can see this state — so dropping
    // the slot here blinks the button out of existence on every mount. Keeping
    // it renders the action in exactly one place in both states, with no
    // duplicate once the editor mounts.
    // Spec: docs/superpowers/specs/2026-08-26-editor-toolbar-design.md (§ "Each
    // panel must keep its own row…", Sol review finding 1).
    return (
      <div data-collab-status={status} data-testid="collab-connecting">
        <Group gap="sm">
          <Text size="sm" c={unreachable ? undefined : "dimmed"}>
            {unreachable
              ? "Could not reach the live document — retrying."
              : "Loading the draft…"}
          </Text>
          {props.actions}
        </Group>
      </div>
    );
  }
  return (
    <CollabEditorInner {...props} doc={doc} provider={provider} status={status} me={me} />
  );
}

function CollabEditorInner(
  props: CollabEditorProps & {
    doc: Y.Doc;
    provider: CollabProvider;
    status: ProviderStatus;
    me: NonNullable<ReturnType<typeof useSession>["data"]>;
  },
) {
  const label = displayName(props.me.user);
  const color = colorForUserId(props.me.user?.id ?? 0);
  const editor = useEditor(
    {
      editable: props.canWrite,
      extensions: [
        ...collabEditorExtensions,
        Collaboration.configure({ document: props.doc, field: "prosemirror" }),
        // Identity is resolved before this component mounts (gated in CollabEditor),
        // so the caret seeds with the real name/color — never a fallback "Anonymous".
        CollaborationCaret.configure({ provider: props.provider, user: { name: label, color } }),
      ],
    },
    [props.doc], // editor keyed ONLY on the doc — NOT rebuilt on identity change
  );

  // Scroll to whichever section the sidebar Outline asks for. Lives here, next
  // to the editor that owns the document — the sidebar cannot reach into it.
  useSectionJump(editor);

  // Keep identity fresh if the user renames themselves mid-session (a cheap no-op
  // otherwise). The mount gate — not this effect — is now the primary mechanism.
  useEffect(() => {
    if (editor) editor.commands.updateUser({ name: label, color });
  }, [editor, label, color]);

  return (
    <div data-collab-status={props.status} className={styles.editor}>
      <EditorToolbar
        editor={editor}
        canWrite={props.canWrite}
        actions={props.actions}
        builtins={
          <>
            <SaveVersionButton
              id={props.workspaceId}
              artifact={props.artifact}
              provider={props.provider}
              canWrite={props.canWrite}
              status={props.status}
            />
            <InsertTableButton editor={editor} canWrite={props.canWrite} />
          </>
        }
      />
      <TableMenu editor={editor} canWrite={props.canWrite} />
      <EditorContent editor={editor} data-testid="collab-editor" />
      {props.artifact === "draft" && props.sections != null && props.head != null && editor && (
        <TiptapSelectionToolbar
          editor={editor}
          provider={props.provider}
          workspaceId={props.workspaceId}
          head={props.head}
          sections={props.sections}
          canEdit={props.canEdit ?? false}
          canWrite={props.canWrite}
        />
      )}
    </div>
  );
}
