import { Box, Button, Card, Code, Group, Table, Text, TextInput, Title } from "@mantine/core";
import { useState } from "react";

import type { AdminGoldenSet as GoldenSet } from "../api/adminGoldenSet";
import { useAddTriple, useAdminGoldenSet, useDeleteTriple } from "../api/adminGoldenSet";
import { ApiError } from "../api/client";

type Candidate = GoldenSet["candidates"][number];

/** The server's reason a write was refused, beside the control that sent it. */
function WriteFailure({ action, error }: { action: string; error: Error | null }) {
  if (!error) return null;
  const reason = error instanceof ApiError ? error.userMessage : error.message;
  return (
    <Text role="alert" c="var(--mantine-color-error)" size="xs" mt={4}>
      Couldn't {action}: {reason}
    </Text>
  );
}

function CandidateRow({ c }: { c: Candidate }) {
  const [score, setScore] = useState("");
  const add = useAddTriple();
  return (
    <Table.Tr>
      <Table.Td>{c.call_title}</Table.Td>
      <Table.Td>{c.profile_name}</Table.Td>
      <Table.Td>{c.aggregate_score}</Table.Td>
      <Table.Td>{c.embed_similarity !== null ? c.embed_similarity.toFixed(3) : "-"}</Table.Td>
      <Table.Td>
        <Code>{c.reason}</Code>
        {/* THE HUMAN'S REASON, beside the signal that disagreed with them (D7).
            This is what the whole note feature is collected FOR: the mining
            query already surfaces "the ranker said 74, a researcher hid it" --
            the note is the half that says why, and a curator setting an
            expected score needs it in the same cell, not two columns away.

            UNATTRIBUTED, and there is nothing to omit: CandidateOut sends bare
            strings, with no user and no timestamp to render even by accident
            (D3). The researcher was told their name is not attached, and a
            privacy property that depends on this component remembering not to
            print a field is not a privacy property -- so the field is not
            there. Capped and ordered newest-first server-side. */}
        {c.user_notes.length > 0 && (
          <Box component="ul" data-user-notes="" fz="xs" mt={4} pl="1rem" c="dimmed">
            {c.user_notes.map((n, i) => (
              // Index key: the notes are unattributed strings off one row and
              // two researchers can write the same words, so the text is not a
              // stable identity. The list is read-only and never reordered.
              <li key={i}>{n}</li>
            ))}
          </Box>
        )}
      </Table.Td>
      <Table.Td>
        <Box component="form" onSubmit={(e) => {
          e.preventDefault();
          add.mutate({ call_id: c.call_id, profile_id: c.profile_id, expected_score: Number(score) },
            { onSuccess: () => setScore("") });
        }}>
          <Group gap="xs" align="center" wrap="nowrap">
            <TextInput size="xs" type="number" min={0} max={100} placeholder="0–100" required
                       value={score} onChange={(e) => setScore(e.currentTarget.value)} />
            <Button size="xs" type="submit" disabled={add.isPending}>Add candidate</Button>
          </Group>
          <WriteFailure action="add the candidate" error={add.error} />
        </Box>
      </Table.Td>
    </Table.Tr>
  );
}

export function AdminGoldenSet() {
  const { data, isLoading, isError } = useAdminGoldenSet();
  const del = useDeleteTriple();
  const add = useAddTriple();
  const [callId, setCallId] = useState("");
  const [profileId, setProfileId] = useState("");
  const [score, setScore] = useState("");
  const [note, setNote] = useState("");

  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data)
    return (
      <Box component="main" role="alert" maw="60rem" mx="auto" px="xs" pt="md" pb="xl">
        <Text>Couldn't load the golden set.</Text>
      </Box>
    );

  const submitManual = (e: React.FormEvent) => {
    e.preventDefault();
    add.mutate(
      { call_id: Number(callId), profile_id: Number(profileId), expected_score: Number(score), note: note.trim() || undefined },
      { onSuccess: () => { setCallId(""); setProfileId(""); setScore(""); setNote(""); } },
    );
  };

  return (
    <Box component="main" maw="60rem" mx="auto" px="xs" pt="md" pb="xl">
      <Title order={1}>Golden set</Title>

      <Card component="section" withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Curated triples ({data.triples.length})</Title>
        {data.triples.length === 0 ? <Text c="dimmed" size="sm">No triples yet.</Text> : (
          <Table horizontalSpacing="sm" verticalSpacing="xs" fz="sm">
            <Table.Thead><Table.Tr><Table.Th>Call</Table.Th><Table.Th>Profile</Table.Th><Table.Th>Expected score</Table.Th><Table.Th>Note</Table.Th><Table.Th></Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {data.triples.map((t) => (
                <Table.Tr key={t.id}>
                  <Table.Td>{t.call_title}</Table.Td><Table.Td>{t.profile_name}</Table.Td><Table.Td>{t.expected_score}</Table.Td><Table.Td>{t.note ?? ""}</Table.Td>
                  <Table.Td><Button size="xs" variant="default" disabled={del.isPending}
                                    onClick={() => del.mutate({ triple_id: t.id })}>Delete</Button></Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
        <WriteFailure action="delete the triple" error={del.error} />
      </Card>

      <Card component="section" withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Add triple manually</Title>
        <Box component="form" onSubmit={submitManual}>
          <Group gap="xs" align="center">
            <TextInput size="xs" type="number" placeholder="call_id" required value={callId} onChange={(e) => setCallId(e.currentTarget.value)} />
            <TextInput size="xs" type="number" placeholder="profile_id" required value={profileId} onChange={(e) => setProfileId(e.currentTarget.value)} />
            <TextInput size="xs" type="number" min={0} max={100} placeholder="Expected score (0–100)" required value={score} onChange={(e) => setScore(e.currentTarget.value)} />
            <TextInput size="xs" type="text" placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.currentTarget.value)} />
            <Button size="xs" type="submit" disabled={add.isPending}>Add</Button>
          </Group>
          <WriteFailure action="add the triple" error={add.error} />
        </Box>
      </Card>

      <Card component="section" withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Candidates ({data.candidates.length})</Title>
        {data.candidates.length === 0 ? (
          <Text c="dimmed" size="sm">No candidates — no dismissed-high-score, cosine-disagreement, or escalation-disagreement pairs found.</Text>
        ) : (
          <Table horizontalSpacing="sm" verticalSpacing="xs" fz="sm">
            <Table.Thead><Table.Tr><Table.Th>Call</Table.Th><Table.Th>Profile</Table.Th><Table.Th>Score</Table.Th><Table.Th>Cosine</Table.Th><Table.Th>Signal</Table.Th><Table.Th>Add</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {data.candidates.map((c) => <CandidateRow key={`${c.call_id}-${c.profile_id}`} c={c} />)}
            </Table.Tbody>
          </Table>
        )}
      </Card>
    </Box>
  );
}
