import { Box, Button, Group, Stack, Text } from "@mantine/core";
import {
  useDismissClaim,
  useLatestUncitedClaimsRun,
  useStartUncitedClaimsRun,
} from "../api/uncitedClaims";
import { type ProblemRow, useProblems } from "../api/problems";
import type { NavTarget, OnNavigate } from "./nav";

const CATEGORY_LABEL: Record<ProblemRow["category"], string> = {
  broken_citation: "Broken citations",
  criterion_gap: "Criteria gaps",
  over_length: "Over-length sections",
  coherence_drift: "Coherence",
  uncited_claim: "Uncited claims",
};
const CATEGORY_ORDER: ProblemRow["category"][] = [
  "broken_citation",
  "criterion_gap",
  "over_length",
  "coherence_drift",
  "uncited_claim",
];

function toNav(target: ProblemRow["target"]): NavTarget {
  return {
    top_tab: target?.top_tab ?? undefined,
    bottom_tab: target?.bottom_tab ?? undefined,
    section_index: target?.section_index ?? undefined,
  };
}

export function ProblemsPanel({ id, onNavigate }: { id: string; onNavigate: OnNavigate }) {
  const q = useProblems(id);
  const latest = useLatestUncitedClaimsRun(id);
  const start = useStartUncitedClaimsRun(id);
  const dismiss = useDismissClaim(id);
  const runStatus = latest.data?.status;
  const running = runStatus === "queued" || runStatus === "running";

  const runButton = (
    <Button
      size="xs"
      variant="default"
      disabled={running || start.isPending}
      onClick={() => start.mutate()}
    >
      {running ? "Checking…" : "Check uncited claims"}
    </Button>
  );

  if (q.isError) {
    return (
      <Stack gap="sm">
        <Group gap="sm">{runButton}</Group>
        <Text role="status" c="red" size="sm">
          Could not load problems.
        </Text>
      </Stack>
    );
  }
  if (q.isLoading) {
    return (
      <Stack gap="sm">
        <Group gap="sm">{runButton}</Group>
        <Text c="dimmed" size="sm">
          Loading…
        </Text>
      </Stack>
    );
  }
  const data = q.data;
  const rows = data?.problems ?? [];
  const scorecardStale = (data?.scorecard_stale ?? []).length > 0;
  const uncitedStale = data?.uncited_claims_stale ?? false;
  const groups = CATEGORY_ORDER.map((cat) => ({
    cat,
    items: rows.filter((r) => r.category === cat),
  })).filter((g) => g.items.length > 0);

  return (
    <Stack gap="sm">
      <Group gap="sm">{runButton}</Group>
      {rows.length === 0 ? (
        <Text c="dimmed" size="sm">
          No problems detected.
        </Text>
      ) : (
        groups.map((g) => (
          <Box key={g.cat} component="section" aria-label={CATEGORY_LABEL[g.cat]}>
            <Text component="h3" tt="uppercase" size="xs" fw={600} c="dimmed">
              {CATEGORY_LABEL[g.cat]}
              {g.cat === "criterion_gap" && scorecardStale && (
                <Text span c="yellow" size="xs"> · may be outdated</Text>
              )}
              {g.cat === "uncited_claim" && uncitedStale && (
                <Text span c="yellow" size="xs"> · may be outdated</Text>
              )}
            </Text>
            <Stack component="ul" gap={4} style={{ listStyle: "none", padding: 0, margin: 0 }}>
              {g.items.map((r, i) => (
                <Group key={`${r.category}-${i}`} component="li" gap="sm" align="flex-start" wrap="nowrap">
                  <Text aria-hidden c={r.severity === "error" ? "red" : "yellow"}>
                    {r.severity === "error" ? "⛔" : "⚠"}
                  </Text>
                  <Text size="sm" fw={600}>{r.title}</Text>
                  <Text size="sm" c="dimmed">{r.detail}</Text>
                  {(r.target || (r.category === "uncited_claim" && r.claim_hash)) && (
                    <Group ml="auto" gap="xs">
                      {r.target && (
                        <Button
                          size="xs"
                          variant="subtle"
                          onClick={() => onNavigate(toNav(r.target))}
                        >
                          Go to {r.title}
                        </Button>
                      )}
                      {r.category === "uncited_claim" && r.claim_hash && (
                        <Button
                          size="xs"
                          variant="subtle"
                          disabled={dismiss.isPending}
                          onClick={() => dismiss.mutate(r.claim_hash as string)}
                        >
                          Dismiss
                        </Button>
                      )}
                    </Group>
                  )}
                </Group>
              ))}
            </Stack>
          </Box>
        ))
      )}
    </Stack>
  );
}
