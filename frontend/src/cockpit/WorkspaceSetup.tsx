import { Alert, Button, Container, Group, Loader, Stack, Text, Title } from "@mantine/core";
import { useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useEffect, useState } from "react";

import { useActivate, useRubric, useSetupStatus, type RubricCriterion } from "../api/rubric";
import { useCockpitMeta } from "../api/workspace";
import { RubricEditor } from "./RubricEditor";

function SetupShell({ children }: { children: ReactNode }) {
  return (
    <Container component="main" size="lg" py="xl">
      {children}
    </Container>
  );
}

function Busy({ label }: { label: string }) {
  return (
    <SetupShell>
      <Group gap="sm">
        <Loader size="sm" />
        <Text>{label}</Text>
      </Group>
    </SetupShell>
  );
}

export function WorkspaceSetup({ id }: { id: string }) {
  const qc = useQueryClient();
  const meta = useCockpitMeta(id);
  const isOwner = meta.data?.role === "owner";
  const setup = useSetupStatus(id);
  const status = setup.data?.status;

  // Viewer (and owner) auto-advance: when setup completes, flip the phase branch.
  useEffect(() => {
    if (status === "done") qc.invalidateQueries({ queryKey: ["cockpit-meta", id] });
  }, [status, id, qc]);

  if (status === "failed")
    return (
      <SetupShell>
        <Stack gap="md">
          <Title order={1}>Setup failed</Title>
          {isOwner && setup.data?.last_error && (
            <Alert color="red" variant="light">
              {setup.data.last_error}
            </Alert>
          )}
          <Text>Delete this workspace and create it again, or contact an administrator.</Text>
        </Stack>
      </SetupShell>
    );

  if (status === "done") return <Busy label="Opening cockpit…" />;

  if (status === "waiting_for_rubric_review" && isOwner) return <OwnerRubricGate id={id} />;

  if (status === "waiting_for_rubric_review")
    return <Busy label="Waiting for the owner to review the rubric…" />;

  return <Busy label="Setting up your workspace… (usually under a minute)" />;
}

function OwnerRubricGate({ id }: { id: string }) {
  const rubric = useRubric(id);
  const activate = useActivate(id);
  const [criteria, setCriteria] = useState<RubricCriterion[]>([]);
  const [funderNotes, setFunderNotes] = useState("");

  useEffect(() => {
    if (rubric.data) {
      setCriteria(rubric.data.criteria);
      setFunderNotes(rubric.data.funder_notes ?? "");
    }
  }, [rubric.data]);

  if (rubric.isLoading) return <Busy label="Loading the rubric…" />;

  return (
    <SetupShell>
      <Stack gap="lg">
        <Stack gap={4}>
          <Title order={1}>Review the funder criteria</Title>
          <Text c="dimmed">Weights are relative — the % shows each criterion's share.</Text>
        </Stack>
        <RubricEditor
          criteria={criteria}
          funderNotes={funderNotes}
          onChange={(n) => {
            setCriteria(n.criteria);
            setFunderNotes(n.funderNotes);
          }}
        />
        {activate.isError && (
          <Alert color="red" variant="light">
            Could not activate. Check the criteria and retry.
          </Alert>
        )}
        <Button
          w="fit-content"
          disabled={activate.isPending}
          loading={activate.isPending}
          onClick={() => activate.mutate({ criteria, funder_notes: funderNotes || null })}
        >
          Confirm &amp; activate
        </Button>
      </Stack>
    </SetupShell>
  );
}
