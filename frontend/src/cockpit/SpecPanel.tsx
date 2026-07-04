import { Alert, Box, Button, Group, Stack, Text, Title } from "@mantine/core";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { useLatestCoherence, useStartCoherence } from "../api/coherence";
import { useInitSpec, useLockSpec, useSaveSpecSection, useSpec, useUnlockSpec } from "../api/editor";
import { CollabEditor } from "../collab/CollabEditor";
import { ArtifactSectionEditor } from "../editor/ArtifactSectionEditor";

export function SpecPanel({ id }: { id: string }) {
  const spec = useSpec(id);
  const save = useSaveSpecSection(id);
  const init = useInitSpec(id);
  const lock = useLockSpec(id);
  const unlock = useUnlockSpec(id);
  const coherence = useLatestCoherence(id);
  const startCoherence = useStartCoherence(id);
  const runStatus = coherence.data?.status;
  const checking = runStatus === "queued" || runStatus === "running";
  const qc = useQueryClient();
  useEffect(() => {
    if (runStatus === "completed") {
      qc.invalidateQueries({ queryKey: ["proposed-changes", id, "spec"] });
      qc.invalidateQueries({ queryKey: ["proposed-changes", id, "draft"] });
    }
  }, [runStatus, id, qc]);

  if (spec.isLoading)
    return (
      <Box component="main" p="md">
        <Text aria-busy="true">Loading spec…</Text>
      </Box>
    );
  if (spec.isError || !spec.data) {
    return (
      <Box component="main" p="md">
        <Title order={2} role="alert">
          Could not load the spec
        </Title>
      </Box>
    );
  }
  const { head, can_edit, sections, locked, locked_at, locked_by, can_manage_lock } = spec.data;
  // Explicit skeleton CTA — no silent commit-on-open. Same whitespace-only
  // predicate the init endpoint uses, so the CTA and the server seed agree.
  const isEmpty = sections.every((s) => s.text.trim() === "");
  if (isEmpty) {
    return (
      <Box component="main" p="md">
        <Stack gap="sm" align="flex-start">
          <Title order={2}>Spec</Title>
          <Text>This proposal has no spec yet.</Text>
          <Button disabled={!can_edit || init.isPending} onClick={() => init.mutate()}>
            Start spec from skeleton
          </Button>
        </Stack>
      </Box>
    );
  }
  const lockedOn = locked_at ? new Date(locked_at).toLocaleDateString() : "";
  return (
    <Stack gap="sm">
      {locked && (
        <Alert role="status" color="teal" variant="light">
          🔒 Spec locked{locked_by ? ` by ${locked_by}` : ""}
          {lockedOn ? ` on ${lockedOn}` : ""}
        </Alert>
      )}
      {can_manage_lock && (
        <Group gap="sm">
          {locked ? (
            <Button size="xs" variant="default" disabled={unlock.isPending} onClick={() => unlock.mutate()}>
              Unlock spec
            </Button>
          ) : (
            <Button size="xs" variant="default" disabled={lock.isPending} onClick={() => lock.mutate()}>
              Lock spec
            </Button>
          )}
        </Group>
      )}
      <Group gap="sm" align="center">
        <Button
          size="xs"
          disabled={checking || startCoherence.isPending}
          onClick={() => startCoherence.mutate()}
        >
          {checking ? "Checking coherence…" : "Check coherence"}
        </Button>
        {runStatus === "completed" && coherence.data && (
          <Text role="status" size="sm" c="dimmed">
            Last check: {coherence.data.n_proposals} proposal(s)
          </Text>
        )}
        {runStatus === "failed" && (
          <Text role="status" size="sm" c="red">
            {coherence.data?.error ?? "Coherence check failed."}
          </Text>
        )}
      </Group>
      {spec.data.collab_enabled ? (
        <CollabEditor
          key={`collab-spec:${id}`}
          workspaceId={id}
          artifact="spec"
          canWrite={spec.data.can_write}
        />
      ) : (
        <ArtifactSectionEditor
          // Remount on workspace change so index-keyed buffers / peek / selection never
          // leak across workspaces (a param nav keeps Cockpit + this editor mounted).
          key={`spec:${id}`}
          workspaceId={id}
          queryKey={["spec", id]}
          head={head}
          canEdit={can_edit}
          sections={sections}
          save={save}
          refetch={spec.refetch}
          citations={false}
          fullViewLabel="Full spec"
        />
      )}
    </Stack>
  );
}
