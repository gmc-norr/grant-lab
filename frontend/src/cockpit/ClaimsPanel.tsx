import { useState } from "react";

import { Badge, Blockquote, Box, Button, Group, Stack, Text, TextInput, Title } from "@mantine/core";

import {
  type ClaimFinding,
  type ClaimSection,
  useAnnotations,
  useDismissAnnotation,
  useInsertAnnotation,
  useReanalyzeSection,
} from "../api/annotations";

const KIND_LABEL: Record<string, string> = {
  uncited_supported: "Uncited · supported",
  uncited_no_match: "Uncited · no match",
  cited_unsupported: "Cited · unsupported",
};

export function ClaimsPanel({ id }: { id: string }) {
  const ann = useAnnotations(id);
  const insert = useInsertAnnotation(id);
  const dismiss = useDismissAnnotation(id);
  const reanalyze = useReanalyzeSection(id);

  if (ann.isLoading) return <Text aria-busy="true">Loading claims…</Text>;
  if (ann.isError || !ann.data) return <Text role="alert" c="red">Could not load claims.</Text>;

  const isOwner = ann.data.is_owner;
  if (ann.data.sections.length === 0)
    return <Text c="dimmed">No draft yet — claims appear once drafting begins.</Text>;

  return (
    <Stack gap="md">
      {ann.data.sections.map((sec) => (
        <ClaimSectionView
          key={`${sec.section_sha}:${sec.occurrence}`}
          section={sec}
          isOwner={isOwner}
          onInsert={(fid) => insert.mutate(fid)}
          onDismiss={(fid, reason) => dismiss.mutate({ annotationId: fid, reason })}
          onReanalyze={() => reanalyze.mutate(sec.index)}
        />
      ))}
    </Stack>
  );
}

function ClaimSectionView({
  section,
  isOwner,
  onInsert,
  onDismiss,
  onReanalyze,
}: {
  section: ClaimSection;
  isOwner: boolean;
  onInsert: (findingId: number) => void;
  onDismiss: (findingId: number, reason: string) => void;
  onReanalyze: () => void;
}) {
  return (
    <Stack component="section" gap="sm">
      <Group component="header" gap="sm" align="center">
        <Title order={4}>{section.heading || "(preamble)"}</Title>
        {section.job.status === "pending" && (
          <Text role="status" span size="sm" c="dimmed">
            analyzing…
          </Text>
        )}
        {section.job.status === "failed" && (
          <Text role="status" span size="sm" c="red">
            analysis failed{section.job.error ? ` — ${section.job.error}` : ""}
          </Text>
        )}
        {isOwner && (
          <Button type="button" size="xs" variant="subtle" onClick={onReanalyze}>
            Reanalyze
          </Button>
        )}
      </Group>
      {section.findings.length === 0 ? (
        <Text c="dimmed" size="sm">No open findings</Text>
      ) : (
        <Stack component="ul" gap="xs" style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {section.findings.map((f) => (
            <FindingRow
              key={f.id}
              finding={f}
              isOwner={isOwner}
              onInsert={() => onInsert(f.id)}
              onDismiss={(reason) => onDismiss(f.id, reason)}
            />
          ))}
        </Stack>
      )}
    </Stack>
  );
}

function FindingRow({
  finding,
  isOwner,
  onInsert,
  onDismiss,
}: {
  finding: ClaimFinding;
  isOwner: boolean;
  onInsert: () => void;
  onDismiss: (reason: string) => void;
}) {
  const [dismissing, setDismissing] = useState(false);
  const [reason, setReason] = useState("");

  return (
    <Box component="li">
      <Badge size="sm" variant="light">{KIND_LABEL[finding.kind] ?? finding.kind}</Badge>
      <Text size="sm">{finding.claim_text}</Text>
      {finding.evidence_quote && <Blockquote p="xs">{finding.evidence_quote}</Blockquote>}
      <Text span size="xs" c="dimmed">{finding.ref_label}</Text>
      {isOwner && (
        <Group gap="sm">
          {finding.can_insert && (
            <Button type="button" size="xs" onClick={onInsert}>
              Insert
            </Button>
          )}
          {!dismissing ? (
            <Button type="button" size="xs" onClick={() => setDismissing(true)}>
              Dismiss
            </Button>
          ) : (
            <Group gap="xs">
              <TextInput
                size="xs"
                placeholder="Reason (required)"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
              <Button
                type="button"
                size="xs"
                aria-label="Confirm dismiss"
                disabled={reason.trim().length === 0}
                onClick={() => onDismiss(reason.trim())}
              >
                Confirm dismiss
              </Button>
            </Group>
          )}
        </Group>
      )}
    </Box>
  );
}
