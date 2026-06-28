import { Anchor, Box, Button, Stack, Text, TextInput, Title } from "@mantine/core";
import { type FormEvent, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { useCreateWorkspace } from "../api/workspace";

export function WorkspaceCreate() {
  const [sp] = useSearchParams();
  const callIdRaw = sp.get("call_id");
  const callId = callIdRaw ? Number(callIdRaw) : null;
  const callTitle = sp.get("call_title") ?? "";
  const profileIdRaw = sp.get("profile_id");
  const profileId = profileIdRaw ? Number(profileIdRaw) : null;

  const [title, setTitle] = useState(callTitle);
  const [deadline, setDeadline] = useState("");
  const create = useCreateWorkspace();
  const navigate = useNavigate();

  // From-call only: without a call there is nothing to draft against (Phase-0
  // requires a call). Guard a direct hit / refresh-without-params.
  if (callId === null || Number.isNaN(callId)) {
    return (
      <Box component="main" role="alert" maw="32rem">
        <Title order={1}>Start drafting</Title>
        <Text>Pick a call from the dashboard to start drafting.</Text>
        <Anchor component={Link} to="/">← Back to calls</Anchor>
      </Box>
    );
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const out = await create.mutateAsync({
      call_id: callId as number,
      title,
      deadline: deadline || undefined,
      profile_id: profileId,
    });
    // Setup + rubric now live in the React cockpit (Slice E).
    navigate(`/cockpit/${out.id}`);
  }

  return (
    <Box component="main" maw="32rem">
      <Title order={1}>New workspace</Title>
      <Text c="dimmed" fz="0.9rem" mt="0.25rem" mb="md">Drafting for: {callTitle || `call #${callId}`}</Text>
      <Box component="form" onSubmit={onSubmit}>
        <Stack gap="0.85rem" align="flex-start">
          <TextInput
            label="Title"
            w="100%"
            value={title}
            onChange={(e) => setTitle(e.currentTarget.value)}
            required
            withAsterisk={false}
            maxLength={500}
          />
          <TextInput
            label="Deadline (optional)"
            type="datetime-local"
            w="100%"
            value={deadline}
            onChange={(e) => setDeadline(e.currentTarget.value)}
          />
          <Button type="submit" variant="default" disabled={create.isPending || !title.trim()}>
            {create.isPending ? "Creating…" : "Start drafting"}
          </Button>
          {create.isError && (
            <Text role="alert" c="var(--app-danger, #e5484d)" fz="0.85rem">
              {create.error instanceof Error ? create.error.message : "Couldn't create the workspace."}
            </Text>
          )}
        </Stack>
      </Box>
    </Box>
  );
}
