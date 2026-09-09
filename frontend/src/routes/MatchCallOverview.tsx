import { Anchor, Badge, Box, Group, Stack, Text, Title } from "@mantine/core";
import type { ReactNode } from "react";
import type { CallDetail } from "../api/calls";
import { CriterionChips } from "../components/CriterionChips";
import { DeadlineLocal } from "./Deadline";

export function MatchCallOverview({ call, ranking, criteria, actions }: CallDetail & { actions?: ReactNode }) {
  const budget = [call.budget_min, call.budget_max].filter((value): value is number => value !== null);
  const hasRanking = ranking && (ranking.rationale || ranking.aggregate_reason_label || ranking.embed_similarity !== null);
  return <>
    <header><Title order={1} size="h2">{call.title}</Title><Text c="dimmed" size="sm">{call.source_name}{call.programme && ` / ${call.programme}`}</Text></header>
    {actions}
    <Box component="section" aria-label="Call details">
      <Group gap="xl" align="flex-start">
        <div><Text size="sm" c="dimmed">Deadline</Text>{call.deadline ? <DeadlineLocal localDate={call.deadline_local_date ?? null} isoFallback={call.deadline} /> : <Text>No deadline published</Text>}</div>
        <div><Text size="sm" c="dimmed">Funding</Text><Text>{budget.length ? `${budget.map(value => value.toLocaleString("en-US")).join(" - ")} ${call.currency ?? ""}` : "Not specified"}</Text></div>
      </Group>
      <Anchor href={call.url} target="_blank" rel="noopener noreferrer" display="inline-block" mt="sm">Open at source →</Anchor>
    </Box>
    <section><Title order={2} size="h4" mb="xs">About this call</Title><Text>{call.summary ?? ranking?.summary ?? "No description published."}</Text></section>
    {hasRanking && <section aria-label="Ranking">
      <Title order={2} size="h4" mb="xs">Why this matches</Title>
      {ranking.aggregate_reason_label && <Badge variant="default" tt="none">{ranking.aggregate_reason_label}</Badge>}
      <CriterionChips criteria={criteria.filter(criterion => !criterion.is_reason_code)} />
      <details className="match-options"><summary>Matching details</summary>
        <Text size="sm">Analyzed by {ranking.model_label}</Text>
        <Text size="sm">{ranking.rationale}</Text>
        {ranking.embed_similarity !== null && <Text size="sm">Cosine similarity: {ranking.embed_similarity.toFixed(3)}</Text>}
        {criteria.filter(criterion => criterion.is_reason_code).map(criterion => <Text key={criterion.criterion} size="sm">{criterion.label}: {criterion.detail ?? criterion.rationale ?? criterion.rating}</Text>)}
      </details>
    </section>}
    {ranking && ranking.applicant_notes.length > 0 && <section aria-label="Deep analysis">
      <Title order={2} size="h4" mb="sm">Before you apply</Title>
      <Stack gap="md">{ranking.applicant_notes.map((note, index) => <div key={index}><Text fw={600}>{note.heading}</Text><Text>{note.text}</Text></div>)}</Stack>
    </section>}
  </>;
}