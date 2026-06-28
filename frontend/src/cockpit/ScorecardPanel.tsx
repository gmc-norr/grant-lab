import { Anchor, Box, Button, Group, Stack, Text, Title } from "@mantine/core";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import {
  useLatestEvaluator,
  useScorecard,
  useStartEvaluator,
} from "../api/evaluator";
import type { OnNavigate } from "./nav";
import { BandBadge } from "./BandBadge";

export function ScorecardPanel({
  id,
  onNavigate,
}: {
  id: string;
  onNavigate: OnNavigate;
}) {
  const latest = useLatestEvaluator(id);
  const start = useStartEvaluator(id);
  const scorecard = useScorecard(id);
  const status = latest.data?.status;
  const running = status === "queued" || status === "running";
  const qc = useQueryClient();
  useEffect(() => {
    if (status === "completed") qc.invalidateQueries({ queryKey: ["scorecard", id] });
  }, [status, id, qc]);

  const data = scorecard.data;
  const criteria = data
    ? [...data.scorecard.criteria].sort((a, b) => b.effective_weight - a.effective_weight)
    : [];

  return (
    <Box component="main" p="md">
      <Stack gap="sm">
        <Group gap="sm" align="center">
          <Title order={2}>Scorecard</Title>
          {data && <BandBadge band={data.scorecard.overall_band} />}
          <Button
            size="xs"
            variant="default"
            disabled={running || start.isPending}
            onClick={() => start.mutate()}
          >
            {running ? "Evaluating…" : "Re-evaluate"}
          </Button>
          {status === "failed" && (
            <Text c="red" role="status" size="sm">
              {latest.data?.error ?? "Evaluation failed."}
            </Text>
          )}
          {start.isError && (
            <Text c="red" role="status" size="sm">
              {(start.error as Error)?.message ?? "Could not start evaluation."}
            </Text>
          )}
        </Group>

        {data && (
          <Text size="sm" c="dimmed">
            evaluated {data.completed_at ? new Date(data.completed_at).toLocaleString() : ""}
            {data.stale && (
              <Text span c="yellow">
                {" "}
                · ⚠ changed since: {data.stale_reasons.join(", ")}
              </Text>
            )}
          </Text>
        )}

        {data?.scorecard.summary && (
          <Text size="sm" c="dimmed">
            {data.scorecard.summary}
          </Text>
        )}

        {!data && (
          <Text size="sm" c="dimmed">
            No evaluation yet — Re-evaluate to generate a scorecard.
          </Text>
        )}
        {data && criteria.length === 0 && (
          <Text size="sm" c="dimmed">
            This call has no extracted criteria to evaluate.
          </Text>
        )}

        <Stack gap="xs">
          {criteria.map((c) => (
            <Stack key={c.index} gap={2}>
              <Group gap="sm" align="center" wrap="nowrap">
                <BandBadge band={c.band} />
                <Text fw={600} size="sm">
                  {c.name}
                </Text>
                <Text size="xs" c="dimmed">
                  w {c.weight}
                </Text>
                {c.deep_link && (
                  <Anchor
                    component="button"
                    size="xs"
                    onClick={() =>
                      onNavigate({
                        top_tab: c.deep_link!.artifact,
                        section_index: c.deep_link!.section_index,
                      })
                    }
                  >
                    Go to §{c.deep_link.section_index}
                  </Anchor>
                )}
              </Group>
              <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
                {c.note}
              </Text>
            </Stack>
          ))}
        </Stack>
      </Stack>
    </Box>
  );
}
