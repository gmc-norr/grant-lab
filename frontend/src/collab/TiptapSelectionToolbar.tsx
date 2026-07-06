// frontend/src/collab/TiptapSelectionToolbar.tsx
import { Button, Group, Menu, Paper } from "@mantine/core";
import type { Editor } from "@tiptap/core";
import { useEffect, useState } from "react";

import { useSubmitTurn } from "../api/turns";
import { CitationComposer, type ClaimContext } from "../cockpit/CitationComposer";
import { toCodePointOffset, type SelectionContext } from "../cockpit/selection";
import type { CollabProvider } from "./collabProvider";
import { deriveClaimAnchor } from "./selectionAnchor";
import { useSettled } from "./useSettled";

const ASK_PRESETS = [
  { label: "Explain this", text: "Explain the selected text." },
  { label: "Is this supported?", text: "Is the selected claim supported by our references? Cite which." },
];
const EDIT_PRESETS = [
  { label: "Shorten", text: "Shorten the selected text without losing meaning." },
  { label: "Rephrase", text: "Rephrase the selected text more clearly." },
];

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

  useEffect(() => {
    const update = () => setClaim(deriveClaimAnchor(editor.state));
    editor.on("selectionUpdate", update);
    return () => { editor.off("selectionUpdate", update); };
  }, [editor]);

  if (!claim) return null;

  // Locate the section in the sections array
  const section = sections[claim.sectionIndex];
  const sectionSha = section?.sha ?? "";

  // Compute section-local code-point offsets against the canonical section text.
  // Reject a NON-UNIQUE anchor: indexOf returns the FIRST occurrence, which would
  // silently target the wrong duplicate (the backend text[from:to]==anchor check
  // still passes). Refuse rather than mis-target (review C#1).
  const utf16Idx = section ? section.text.indexOf(claim.anchor) : -1;
  const anchorUnique = utf16Idx >= 0 && utf16Idx === section!.text.lastIndexOf(claim.anchor);
  const anchorable = claim.singleSection && !!sectionSha && anchorUnique;
  const fromOffset = anchorable ? toCodePointOffset(section!.text, utf16Idx) : 0;
  const toOffset = anchorable ? toCodePointOffset(section!.text, utf16Idx + claim.anchor.length) : 0;

  // Gate checks — when not anchorable we disable all actions to avoid sending bad offsets
  const askReady = anchorable;
  const writeReady = settled && anchorable && !claim.headingOnly;

  function runTurn(requestType: "ask" | "modify", instruction: string) {
    if (!instruction.trim() || submit.isPending) return;
    const selection_context: SelectionContext = {
      request_type: requestType,
      target_artifact: "draft",
      section_index: claim!.sectionIndex,
      base_commit_sha: head,
      base_section_sha: sectionSha,
      anchor: claim!.anchor,
      from_offset: fromOffset,
      to_offset: toOffset,
    };
    submit.mutate({ content: instruction, selection_context });
  }

  const claimCtx: ClaimContext = {
    section_index: claim.sectionIndex,
    base_commit_sha: head,
    base_section_sha: sectionSha,
    anchor: claim.anchor,
  };

  return (
    <>
      <Paper withBorder p="xs" role="toolbar" aria-label="Selection actions">
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
      </Paper>

      {/* Citation composer rendered when Cite is clicked */}
      {citeOpen && (
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
