import { useEffect } from "react";

import { Button, Code, Group, Stack, Text } from "@mantine/core";

import { type AuditIssue, useCitationAudit, useDeepCheck } from "../api/audit";
import { useCockpitMeta } from "../api/workspace";
import type { OnNavigate } from "./nav";

export function CitationsPanel({ id, onNavigate }: { id: string; onNavigate: OnNavigate }) {
  const audit = useCitationAudit(id);
  const deep = useDeepCheck(id);
  const meta = useCockpitMeta(id);
  const isOwner = meta.data?.role === "owner";

  // Drop any prior deep-check result when the workspace changes, so stale deep data
  // can never supersede a different workspace's deterministic baseline (Codex finding 3).
  useEffect(() => {
    deep.reset();
  }, [id]); // eslint-disable-line react-hooks/exhaustive-deps -- reset is stable; intentional id-only

  if (audit.isLoading) return <Text aria-busy="true">Checking citations…</Text>;
  if (audit.isError || !audit.data)
    return <Text role="alert" c="red">Could not run the citation audit.</Text>;
  if (!audit.data.draft_present)
    return <Text c="dimmed">No draft yet — citations appear once drafting begins.</Text>;

  // Deep-check results (when present) supersede the deterministic baseline. Render errors
  // before warnings (spec §5.1) — the backend returns them interleaved per row. Array.sort
  // is stable, so within a severity the backend order is preserved.
  const RANK: Record<string, number> = { error: 0, warning: 1 };
  const issues: AuditIssue[] = [...(deep.data?.issues ?? audit.data.issues)].sort(
    (a, b) => (RANK[a.severity] ?? 9) - (RANK[b.severity] ?? 9),
  );

  return (
    <div className="citations-panel">
      <Group gap="sm">
        <Button
          type="button"
          size="xs"
          variant="default"
          onClick={() => {
            deep.reset(); // clear superseding deep-check data so Re-run shows the fresh baseline
            void audit.refetch();
          }}
        >
          Re-run
        </Button>
        {isOwner && (
          <Button
            type="button"
            size="xs"
            variant="default"
            onClick={() => deep.mutate()}
            disabled={deep.isPending}
          >
            {deep.isPending ? "Deep checking…" : "Deep check"}
          </Button>
        )}
        {deep.data && (
          <Text span size="sm" c="dimmed">
            Checked {deep.data.checked} · {deep.data.error_count} error
            {deep.data.error_count === 1 ? "" : "s"} · {deep.data.warning_count} warning
            {deep.data.warning_count === 1 ? "" : "s"}
          </Text>
        )}
      </Group>
      {deep.data?.cap_exceeded && (
        <Text role="status" size="sm" c="yellow">
          Deep check hit the daily AI budget — some citations may not have been judged. Try
          again tomorrow.
        </Text>
      )}
      {deep.data && !deep.data.semantic_available && (
        <Text size="sm" c="dimmed">
          Semantic checking is unavailable; showing deterministic results only.
        </Text>
      )}
      {issues.length === 0 ? (
        <Text c="dimmed">No citation issues found.</Text>
      ) : (
        <Stack component="ul" gap="xs" style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {issues.map((iss, i) => (
            <Group component="li" key={i} gap="sm" align="flex-start" wrap="wrap">
              <Text span size="sm" fw={600}>{iss.type}</Text>
              {iss.citekey && <Code>{iss.citekey}</Code>}
              {iss.location && <Text span size="sm" c="dimmed">{iss.location}</Text>}
              <Text span size="sm" c="dimmed">{iss.detail}</Text>
              {iss.evidence_id && (
                <Button
                  type="button"
                  size="xs"
                  variant="subtle"
                  onClick={() =>
                    onNavigate({ top_tab: "draft", evidence_id: iss.evidence_id ?? undefined })
                  }
                >
                  View source
                </Button>
              )}
            </Group>
          ))}
        </Stack>
      )}
    </div>
  );
}
