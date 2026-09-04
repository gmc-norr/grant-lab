// frontend/src/routes/CallDetailBody.tsx
import { Anchor, Badge, Box, Button, Card, Code, Group, Stack, Text, Title } from "@mantine/core";
import { Link } from "react-router-dom";

import type { CallDetail } from "../api/calls";
import { isMatchEdition } from "../edition";
import { DeadlineLocal } from "./Deadline";

function budgetText(c: { budget_min: number | null; budget_max: number | null; currency: string | null }): string {
  if (c.budget_min == null && c.budget_max == null) return "—";
  const fmt = (n: number) => n.toLocaleString("en-US");
  const lo = c.budget_min != null ? fmt(c.budget_min) : "—";
  const hi = c.budget_max != null ? fmt(c.budget_max) : "—";
  return `${lo} – ${hi} ${c.currency ?? ""}`.trim();
}

const GRID_AUTOFILL = (min: string) => ({
  display: "grid",
  gridTemplateColumns: `repeat(auto-fill, minmax(${min}, 1fr))`,
  gap: "0.75rem",
});

// Presentational call detail, shared verbatim by the /calls/:id full page
// (CallDetail) and the dashboard reading pane (CallReadingPane). Pure render of
// the useCallDetail payload — no hooks, no routing state, no mutations. Returns a
// Fragment so the parent's <Stack> controls vertical rhythm. Hide/unhide and the
// page chrome live in CallDetail (v1: hide is full-page only).
//
// The "Start drafting workspace" button is GATED on the edition, not deleted —
// see the long note in CallRow.tsx for why (run-record ESCALATE E1). Release 1
// does not mount /cockpit/new, so the match edition must not render it; the full
// edition is the only remaining way to reach WorkspaceCreate. noCockpitLinks
// .test.tsx EXEMPTs this file, so both branches are pinned by CallDetailBody
// .test.tsx and CallDetail.test.tsx instead.
export function CallDetailBody({ call, ranking, criteria, best_profile_id }: CallDetail) {
  const startParams = new URLSearchParams({ call_id: String(call.id), call_title: call.title });
  if (best_profile_id != null) startParams.set("profile_id", String(best_profile_id));

  const band = ranking
    ? ranking.aggregate_score >= 70 ? "strong" : ranking.aggregate_score > 0 ? "mid" : "low"
    : "low";
  const figureColor =
    band === "strong" ? "var(--app-good)" : band === "mid" ? "var(--app-warn)" : "var(--app-dim)";

  return (
    <>
      <Box component="header">
        <Title order={1}>{call.title}</Title>
        <Text c="dimmed" fz="0.9rem">
          {call.source_name}
          {call.programme && <> · {call.programme}</>}
          {call.external_id && <> · <Code>{call.external_id}</Code></>}
        </Text>
        {!isMatchEdition && (
          <Box mt="sm">
            <Button component={Link} to={`/cockpit/new?${startParams.toString()}`} size="sm">
              Start drafting workspace
            </Button>
          </Box>
        )}
      </Box>

      {ranking && (
        <Box component="section" aria-label="Ranking">
          <Card component="article" withBorder radius="md" p="md" bg="var(--app-sidebar)">
            <Group align="flex-start" gap="md" wrap="nowrap">
              <Text data-band={band} fz="2.5rem" fw={700} ta="center" c={figureColor} miw="3rem" lh={1}>
                {ranking.aggregate_score}
              </Text>
              <Stack gap="0.4rem">
                <Text fw={700}>Aggregate score</Text>
                {ranking.aggregate_reason_label && (
                  <Badge variant="default" tt="none" radius="xl">{ranking.aggregate_reason_label}</Badge>
                )}
                <Badge variant="default" tt="none" radius="xl" title="LLM that produced this ranking">
                  Analyzed by {ranking.model_label}
                </Badge>
                {ranking.rationale && !ranking.aggregate_reason_label && (
                  <Text fz="0.9rem">{ranking.rationale}</Text>
                )}
                {ranking.embed_similarity != null && (
                  <Text c="dimmed" fz="0.8rem">Cosine similarity: {ranking.embed_similarity.toFixed(3)}</Text>
                )}
                {ranking.escalated && ranking.escalated_from_score != null && (
                  <Text c="dimmed" fz="0.8rem">
                    Escalated from {ranking.escalated_from_score} (analysis by {ranking.analysis_model_label}).
                  </Text>
                )}
              </Stack>
            </Group>
          </Card>
        </Box>
      )}

      {ranking && (ranking.summary || ranking.applicant_notes.length > 0) && (
        <Box component="section" aria-label="Deep analysis">
          <Title order={2} fz="1.05rem" mb="xs">
            Analyst notes <Text span c="dimmed" fz="0.8rem">— {ranking.model_label}</Text>
          </Title>
          {ranking.summary && <Text>{ranking.summary}</Text>}
          {ranking.applicant_notes.length > 0 && (
            <Box mt="sm" style={GRID_AUTOFILL("14rem")}>
              {ranking.applicant_notes.map((n, i) => (
                <Card key={i} component="article" withBorder radius="sm" py="0.6rem" px="0.75rem">
                  <Text component="h3" fz="0.85rem" c="var(--app-accent)" mb="0.25rem">{n.heading}</Text>
                  <Text fz="0.85rem">{n.text}</Text>
                </Card>
              ))}
            </Box>
          )}
        </Box>
      )}

      <Box component="section" aria-label="Call details">
        <Title order={2} fz="1.05rem" mb="xs">Details</Title>
        <Box style={GRID_AUTOFILL("12rem")}>
          <Stack gap="0.15rem">
            <Text c="dimmed" fz="0.75rem" tt="uppercase">Published</Text>
            <Text>{call.published_at ? call.published_at.slice(0, 10) : "—"}</Text>
          </Stack>
          <Stack gap="0.15rem">
            <Text c="dimmed" fz="0.75rem" tt="uppercase">Deadline</Text>
            <Text component="span">
              {call.deadline
                ? <DeadlineLocal localDate={call.deadline_local_date ?? null} isoFallback={call.deadline} />
                : "—"}
            </Text>
          </Stack>
          <Stack gap="0.15rem">
            <Text c="dimmed" fz="0.75rem" tt="uppercase">Budget</Text>
            <Text>{budgetText(call)}</Text>
          </Stack>
          <Stack gap="0.15rem">
            <Text c="dimmed" fz="0.75rem" tt="uppercase">External link</Text>
            <Anchor href={call.url} target="_blank" rel="noopener noreferrer">Open at source →</Anchor>
          </Stack>
        </Box>
        {call.keywords.length > 0 && (
          <Group mt="sm" gap="0.35rem" align="center">
            <Text c="dimmed" fz="0.8rem">Keywords:</Text>
            {call.keywords.map((kw) => (
              <Badge key={kw} variant="default" tt="none" radius="xl">{kw}</Badge>
            ))}
          </Group>
        )}
      </Box>

      {call.summary && (
        <Box component="section">
          <Title order={2} fz="1.05rem" mb="xs">Summary</Title>
          <Text>{call.summary}</Text>
        </Box>
      )}

      {criteria.length > 0 && (
        <Box component="section">
          <Title order={2} fz="1.05rem" mb="xs">Criterion breakdown</Title>
          <Stack gap="0.6rem">
            {criteria.map((c) => (
              <Card key={c.criterion} withBorder radius="sm" py="0.6rem" px="0.75rem">
                <Group justify="space-between" fw={600} mb="0.25rem">
                  <Text span>{c.label}</Text>
                  <Text span fw={700}>{c.is_topic_fit ? `${c.rating}/5` : c.rating}</Text>
                </Group>
                {c.detail && <Text fz="0.9rem">{c.detail}</Text>}
                {c.rationale && (!c.detail || !c.detail.includes(c.rationale)) && (
                  <Text c="dimmed" fz="0.8rem">
                    {c.is_reason_code ? <>Reason code: <Code>{c.rationale}</Code></> : c.rationale}
                  </Text>
                )}
                {c.quoted_evidence && (
                  <Text component="blockquote" fs="italic" c="dimmed" pl="sm" mt="0.4rem"
                        style={{ borderLeft: "2px solid var(--app-border-strong)" }}>
                    "{c.quoted_evidence}"
                  </Text>
                )}
              </Card>
            ))}
          </Stack>
        </Box>
      )}
    </>
  );
}
