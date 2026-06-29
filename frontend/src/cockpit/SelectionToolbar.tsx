import { Button, Group, Menu, Paper, TextInput } from "@mantine/core";
import { useState } from "react";

import { ApiError } from "../api/client";
import { useAckDisclosure, useSubmitTurn } from "../api/turns";
import { MAX_SELECTION_CODEPOINTS, toCodePointOffset, type SelectionContext } from "./selection";
import styles from "./SelectionToolbar.module.css";

type Sel = { from: number; to: number; text: string };

const ASK_PRESETS = [
  { label: "Explain this", text: "Explain the selected text." },
  { label: "Is this supported by our references?", text: "Is the selected claim supported by our references? Cite which." },
];
const EDIT_PRESETS = [
  { label: "Shorten", text: "Shorten the selected text without losing meaning." },
  { label: "Lengthen", text: "Expand the selected text with more detail." },
  { label: "Rephrase", text: "Rephrase the selected text more clearly." },
  { label: "Fix grammar", text: "Fix grammar and spelling in the selected text." },
  { label: "Change tone", text: "Make the selected text more formal and precise." },
];

export type SelectionToolbarProps = {
  workspaceId: string;
  selection: Sel | null;
  editorValue: string;
  sectionIndex: number;
  headSha: string;
  sectionSha: string;
  canEdit: boolean;
  isDirty: boolean;
  onCite: () => void;
  onSubmitted?: (jobId: string) => void;
};

export function SelectionToolbar(props: SelectionToolbarProps) {
  const { selection, editorValue, sectionIndex, headSha, sectionSha, canEdit, isDirty, workspaceId } = props;
  const submit = useSubmitTurn(workspaceId);
  const ack = useAckDisclosure(workspaceId);
  const [free, setFree] = useState("");
  // A member (or owner) who hasn't acked the cloud-LLM disclosure gets a 403;
  // hold the pending instruction and offer an inline ack + retry, since the
  // chat-footer ack path isn't shown to non-owners.
  const [needsAck, setNeedsAck] = useState<{ rt: "ask" | "modify"; text: string } | null>(null);

  if (!selection) return null;

  const fromCp = toCodePointOffset(editorValue, selection.from);
  const toCp = toCodePointOffset(editorValue, selection.to);
  const tooLong = toCp - fromCp > MAX_SELECTION_CODEPOINTS;

  function send(requestType: "ask" | "modify", instruction: string) {
    if (!instruction.trim() || tooLong || submit.isPending) return;
    const selection_context: SelectionContext = {
      request_type: requestType, target_artifact: "draft", section_index: sectionIndex,
      base_commit_sha: headSha, base_section_sha: sectionSha,
      anchor: selection!.text, from_offset: fromCp, to_offset: toCp,
    };
    submit.mutate(
      { content: instruction, selection_context },
      {
        onSuccess: (r) => { setFree(""); setNeedsAck(null); props.onSubmitted?.(r.job_id); },
        onError: (e) => {
          if (e instanceof ApiError && /disclosure/i.test(e.message)) {
            setNeedsAck({ rt: requestType, text: instruction });
          }
        },
      },
    );
  }

  function ackAndRetry() {
    const p = needsAck;
    ack.mutate(undefined, {
      onSuccess: () => { setNeedsAck(null); if (p) send(p.rt, p.text); },
    });
  }

  const editTitle = isDirty ? "Save section to edit" : undefined;

  return (
    <Paper className={styles.toolbar} withBorder p="xs" role="toolbar" aria-label="Selection actions">
      <Group gap="xs" wrap="nowrap">
        <Menu position="bottom-start" disabled={tooLong}>
          <Menu.Target><Button size="xs" variant="default">Ask</Button></Menu.Target>
          <Menu.Dropdown>
            {ASK_PRESETS.map((p) => (
              <Menu.Item key={p.label} onClick={() => send("ask", p.text)}>{p.label}</Menu.Item>
            ))}
          </Menu.Dropdown>
        </Menu>
        {canEdit && (
          <Menu position="bottom-start" disabled={isDirty || tooLong}>
            <Menu.Target>
              <Button size="xs" variant="default" disabled={isDirty || tooLong} title={editTitle}>Edit</Button>
            </Menu.Target>
            <Menu.Dropdown>
              {EDIT_PRESETS.map((p) => (
                <Menu.Item key={p.label} onClick={() => send("modify", p.text)}>{p.label}</Menu.Item>
              ))}
            </Menu.Dropdown>
          </Menu>
        )}
        {canEdit && (
          <Button size="xs" variant="default" disabled={isDirty || tooLong} title={editTitle} onClick={props.onCite}>
            Cite
          </Button>
        )}
      </Group>
      <Group gap="xs" mt={4} wrap="nowrap">
        <TextInput
          size="xs" flex={1} aria-label="Selection instruction" placeholder="Ask or instruct…"
          value={free} onChange={(e) => setFree(e.currentTarget.value)}
        />
        {free.trim() && (
          <Button size="xs" onClick={() => send("ask", free)} disabled={tooLong || submit.isPending}>
            Ask
          </Button>
        )}
        {canEdit && free.trim() && (
          <Button size="xs" variant="light" title={editTitle}
            onClick={() => send("modify", free)} disabled={isDirty || tooLong || submit.isPending}>
            Edit
          </Button>
        )}
      </Group>
      {needsAck && (
        <div className={styles.disclosure} role="alert">
          <span>Answers use a hosted model. Don&apos;t paste patient-identifiable data.</span>
          <Button size="xs" onClick={ackAndRetry} disabled={ack.isPending}>I understand — ask</Button>
        </div>
      )}
      {tooLong && <span className={styles.hint}>Selection too long — pick a smaller span.</span>}
    </Paper>
  );
}
