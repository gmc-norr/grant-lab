// frontend/src/collab/TiptapSelectionToolbar.tsx
import { Button, Group, Menu, Paper, Text } from "@mantine/core";
import type { Editor } from "@tiptap/core";
import { BubbleMenu, type BubbleMenuProps } from "@tiptap/react/menus";
import { useEffect, useState } from "react";

import { ApiError } from "../api/client";
import { useAckDisclosure, useSubmitTurn } from "../api/turns";
import { CitationComposer, type ClaimContext } from "../cockpit/CitationComposer";
import { toCodePointOffset, type SelectionContext } from "../cockpit/selection";
import type { CollabProvider } from "./collabProvider";
import { deriveClaimAnchor } from "./selectionAnchor";
import styles from "./TiptapSelectionToolbar.module.css";
import { useSettled } from "./useSettled";

// The full turn payload frozen for an ack-retry — never rebuilt from the live claim.
type TurnPayload = { content: string; selection_context: SelectionContext };

const ASK_PRESETS = [
  { label: "Explain this", text: "Explain the selected text." },
  { label: "Is this supported?", text: "Is the selected claim supported by our references? Cite which." },
];
const EDIT_PRESETS = [
  { label: "Shorten", text: "Shorten the selected text without losing meaning." },
  { label: "Rephrase", text: "Rephrase the selected text more clearly." },
];

const BUBBLE_OPTIONS = {
  strategy: "fixed",
  placement: "top",
  offset: 8,
  flip: true,
  shift: { padding: 8 },
  inline: true,
} as const;

const shouldShowSelectionToolbar: NonNullable<BubbleMenuProps["shouldShow"]> = ({ state }) =>
  deriveClaimAnchor(state) !== null;

const appendToBody = () => document.body;

export type TiptapSelectionToolbarProps = {
  editor: Editor;
  provider: CollabProvider;
  workspaceId: string;
  head: string;
  sections: { sha: string; text: string }[];
  canEdit: boolean;
  canWrite: boolean;
};

export function TiptapSelectionToolbar(props: TiptapSelectionToolbarProps) {
  const { editor, provider, workspaceId, head, sections, canEdit, canWrite } = props;
  const submit = useSubmitTurn(workspaceId);
  const [claim, setClaim] = useState(() => deriveClaimAnchor(editor.state));
  const [citeOpen, setCiteOpen] = useState(false);
  // Reactive settle state (polled) so Edit/Cite re-enable when the WS buffer drains —
  // the render-time sample latched disabled after a selection's awareness send (D0).
  const settled = useSettled(provider);
  const ack = useAckDisclosure(workspaceId);
  const [needsAck, setNeedsAck] = useState<TurnPayload | null>(null);
  const [turnError, setTurnError] = useState<string | null>(null);

  useEffect(() => {
    const update = () => setClaim(deriveClaimAnchor(editor.state));
    editor.on("selectionUpdate", update);
    return () => { editor.off("selectionUpdate", update); };
  }, [editor]);

  // Locate the section in the sections array
  const section = claim ? sections[claim.sectionIndex] : undefined;
  const sectionSha = section?.sha ?? "";

  // Compute section-local code-point offsets against the canonical section text.
  // Reject a NON-UNIQUE anchor: indexOf returns the FIRST occurrence, which would
  // silently target the wrong duplicate (the backend text[from:to]==anchor check
  // still passes). Refuse rather than mis-target (review C#1).
  const utf16Idx = claim && section ? section.text.indexOf(claim.anchor) : -1;
  const anchorUnique = !!claim
    && utf16Idx >= 0
    && utf16Idx === section!.text.lastIndexOf(claim.anchor);
  const anchorable = !!claim && claim.singleSection && !!sectionSha && anchorUnique;
  const fromOffset = anchorable ? toCodePointOffset(section!.text, utf16Idx) : 0;
  const toOffset = anchorable
    ? toCodePointOffset(section!.text, utf16Idx + claim!.anchor.length)
    : 0;

  // Gate checks — when not anchorable we disable all actions to avoid sending bad offsets
  const askReady = anchorable;
  const writeReady = settled && anchorable && !claim?.headingOnly;

  // submitTurn owns mutate + error handling. On a disclosure 403 it freezes the WHOLE
  // payload (content + the selection_context captured at submit time) into needsAck, so
  // ackAndRetry re-submits the ORIGINAL selection — immune to the user changing/clearing
  // the selection between the 403 and the ack.
  function submitTurn(payload: TurnPayload) {
    submit.mutate(payload, {
      onSuccess: () => {
        setNeedsAck(null);
        setTurnError(null);
      },
      onError: (e) => {
        if (e instanceof ApiError && /disclosure/i.test(e.message)) {
          setNeedsAck(payload);
        } else {
          setTurnError("Couldn't do that — please try again.");
        }
      },
    });
  }

  function runTurn(requestType: "ask" | "modify", instruction: string) {
    if (!claim || !instruction.trim() || submit.isPending) return;
    const selection_context: SelectionContext = {
      request_type: requestType,
      target_artifact: "draft",
      section_index: claim.sectionIndex,
      base_commit_sha: head,
      base_section_sha: sectionSha,
      anchor: claim.anchor,
      from_offset: fromOffset,
      to_offset: toOffset,
    };
    submitTurn({ content: instruction, selection_context });
  }

  function ackAndRetry() {
    const pending = needsAck;
    ack.mutate(undefined, {
      onSuccess: () => {
        setNeedsAck(null);
        if (pending) submitTurn(pending); // re-submit the FROZEN payload, not the live claim
      },
    });
  }

  const claimCtx: ClaimContext | null = claim
    ? {
        section_index: claim.sectionIndex,
        base_commit_sha: head,
        base_section_sha: sectionSha,
        anchor: claim.anchor,
      }
    : null;

  return (
    <>
      <BubbleMenu
        editor={editor}
        pluginKey="draft-selection-actions"
        updateDelay={0}
        appendTo={appendToBody}
        shouldShow={shouldShowSelectionToolbar}
        options={BUBBLE_OPTIONS}
        className={styles.anchor}
      >
        {claim && (
          <Paper
            className={styles.toolbar}
            withBorder
            p="xs"
            role="toolbar"
            aria-label="Selection actions"
          >
            <Group gap="xs" wrap="nowrap">
          {/* Ask — always visible, enabled when anchorable */}
          <Menu position="bottom-start" disabled={!askReady}>
            <Menu.Target>
              <Button size="xs" variant="default" disabled={!askReady} aria-label="Ask">
                Ask
              </Button>
            </Menu.Target>
            <Menu.Dropdown>
              {ASK_PRESETS.map((p) => (
                <Menu.Item key={p.label} onClick={() => runTurn("ask", p.text)}>
                  {p.label}
                </Menu.Item>
              ))}
            </Menu.Dropdown>
          </Menu>

          {/* Edit — only when canEdit; disabled when not writeReady */}
          {canEdit && (
            <Menu position="bottom-start" disabled={!writeReady}>
              <Menu.Target>
                <Button size="xs" variant="default" disabled={!writeReady} aria-label="Edit">
                  Edit
                </Button>
              </Menu.Target>
              <Menu.Dropdown>
                {EDIT_PRESETS.map((p) => (
                  <Menu.Item key={p.label} onClick={() => runTurn("modify", p.text)}>
                    {p.label}
                  </Menu.Item>
                ))}
              </Menu.Dropdown>
            </Menu>
          )}

          {/* Cite — only when canWrite; disabled when not writeReady */}
          {canWrite && (
            <Button
              size="xs"
              variant="default"
              disabled={!writeReady}
              aria-label="Cite"
              onClick={() => setCiteOpen(true)}
            >
              Cite
            </Button>
          )}
            </Group>

            {needsAck && (
              <Group gap="xs" mt={4} wrap="nowrap" role="alert">
                <Text size="xs">
                  Answers use a hosted model. Don&apos;t paste patient-identifiable data.
                </Text>
                <Button size="xs" onClick={ackAndRetry} disabled={ack.isPending}>
                  I understand — ask
                </Button>
              </Group>
            )}
            {turnError && (
              <Text size="xs" c="red" mt={4} role="alert">
                {turnError}
              </Text>
            )}
          </Paper>
        )}
      </BubbleMenu>

      {/* Citation composer rendered when Cite is clicked */}
      {citeOpen && claimCtx && (
        <CitationComposer
          workspaceId={workspaceId}
          claim={claimCtx}
          onDone={() => setCiteOpen(false)}
          onCancel={() => setCiteOpen(false)}
        />
      )}
    </>
  );
}
