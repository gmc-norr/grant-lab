import { Box, Button, Group, Stack, Text } from "@mantine/core";
import { useState } from "react";

import { ApiError } from "../api/client";
import {
  type Artifact,
  type ProposalRow,
  useAcceptProposal,
  useProposedChanges,
  useRejectProposal,
} from "../api/proposals";
import { DiffView } from "../cockpit/DiffView";
import { lineDiff, refineRuns } from "../cockpit/diff";

export type InlineProposalReviewProps = {
  workspaceId: string;
  artifact: Artifact;
  sectionIndex: number;
  currentText: string;
  canEdit: boolean;
};

export function InlineProposalReview({
  workspaceId, artifact, sectionIndex, currentText, canEdit,
}: InlineProposalReviewProps) {
  const list = useProposedChanges(workspaceId, artifact);
  const accept = useAcceptProposal(workspaceId, artifact);
  const reject = useRejectProposal(workspaceId, artifact);
  const [notice, setNotice] = useState<string | null>(null);

  const pending = (list.data ?? []).find(
    (p: ProposalRow) =>
      p.kind === "prose_edit" && p.status === "pending" && p.section_index === sectionIndex,
  );
  if (!pending) return null;

  const pc = pending;

  async function onAccept() {
    setNotice(null);
    try {
      const r = await accept.mutateAsync(pc.id);
      if (r.status === "stale") setNotice("That proposal went stale (the section moved) — reloading.");
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message.replace(/^[a-z_]+: /, "") : "Accept failed.");
    }
  }
  async function onReject() {
    setNotice(null);
    try {
      await reject.mutateAsync(pc.id);
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message.replace(/^[a-z_]+: /, "") : "Reject failed.");
    }
  }

  return (
    <Box component="section" aria-label="Suggested edit" p="sm"
      style={{ border: "1px solid var(--app-accent)", borderRadius: 6 }}>
      <Stack gap="xs">
        <Text size="xs" c="dimmed">Suggested edit from the assistant</Text>
        {notice && <Text role="status" size="sm" c="dimmed">{notice}</Text>}
        <DiffView rows={refineRuns(lineDiff(currentText, pc.new_text ?? ""))} />
        {canEdit && (
          <Group gap="sm">
            <Button size="xs" disabled={accept.isPending} onClick={onAccept}>Accept</Button>
            <Button size="xs" variant="default" disabled={reject.isPending} onClick={onReject}>Reject</Button>
          </Group>
        )}
      </Stack>
    </Box>
  );
}
