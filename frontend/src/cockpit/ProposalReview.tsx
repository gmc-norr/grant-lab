import { Box, Button, Group, Stack, Text, Title } from "@mantine/core";
import { useState } from "react";

import { ApiError } from "../api/client";
import {
  type Artifact,
  type ProposalRow,
  useAcceptProposal,
  useProposedChanges,
  useRejectProposal,
} from "../api/proposals";
import styles from "./CockpitPanels.module.css";
import { lineDiff } from "./diff";

export function ProposalReview({
  workspaceId,
  artifact,
  currentSections,
  locked = false,
}: {
  workspaceId: string;
  artifact: Artifact;
  currentSections: { index: number; text: string }[];
  locked?: boolean;
}) {
  const list = useProposedChanges(workspaceId, artifact);
  const accept = useAcceptProposal(workspaceId, artifact);
  const reject = useRejectProposal(workspaceId, artifact);
  const [notice, setNotice] = useState<string | null>(null);

  const prose = (list.data ?? []).filter((p: ProposalRow) => p.kind === "prose_edit");
  if (prose.length === 0) return null;

  const currentFor = (idx: number) => currentSections.find((s) => s.index === idx)?.text ?? "";

  async function onAccept(p: ProposalRow) {
    setNotice(null);
    try {
      const result = await accept.mutateAsync(p.id);
      if (result.status === "stale") {
        setNotice("That proposal went stale (the section moved) — reloading.");
      }
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message.replace(/^[a-z_]+: /, "") : "Accept failed.");
    }
  }

  async function onReject(p: ProposalRow) {
    setNotice(null);
    try {
      await reject.mutateAsync(p.id);
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message.replace(/^[a-z_]+: /, "") : "Reject failed.");
    }
  }

  return (
    <Box component="section" aria-label="Pending proposals" p="sm" style={{ border: "1px solid var(--app-border)", borderRadius: 6 }}>
      <Stack gap="sm">
        <Title order={3}>Pending proposals ({prose.length})</Title>
        {notice && (
          <Text role="status" size="sm" c="dimmed">
            {notice}
          </Text>
        )}
        <Stack gap="sm">
          {prose.map((p) => (
            <Box key={p.id} style={{ borderTop: "1px solid var(--app-border)", paddingTop: "0.5rem" }}>
              <Text size="xs" c="dimmed">
                proposed by the assistant{p.created_at ? ` · ${new Date(p.created_at).toLocaleString()}` : ""}
              </Text>
              <pre className={styles.proposalDiff}>
                {lineDiff(currentFor(p.section_index), p.new_text ?? "").map((r, k) => (
                  <div key={k} data-diff={r.tag}>
                    {(r.tag === "add" ? "+ " : r.tag === "del" ? "- " : "  ") + r.text}
                  </div>
                ))}
              </pre>
              <Group gap="sm">
                <Button
                  size="xs"
                  disabled={locked || accept.isPending}
                  title={locked ? "Unlock the spec to accept" : undefined}
                  onClick={() => onAccept(p)}
                >
                  Accept
                </Button>
                <Button size="xs" variant="default" disabled={reject.isPending} onClick={() => onReject(p)}>
                  Reject
                </Button>
              </Group>
            </Box>
          ))}
        </Stack>
      </Stack>
    </Box>
  );
}
