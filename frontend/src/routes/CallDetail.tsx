// frontend/src/routes/CallDetail.tsx
import { Anchor, Button, Group, Stack, Text } from "@mantine/core";
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { useCallDetail, useHideCall, useUnhideCall } from "../api/calls";
import { CallDetailBody } from "./CallDetailBody";

export function CallDetail() {
  const { id = "" } = useParams();
  const callId = Number(id);
  const [sp] = useSearchParams();
  const profileParam = sp.get("profile");
  const profile = profileParam != null && profileParam !== "" ? Number(profileParam) : null;
  const navigate = useNavigate();
  const hide = useHideCall();
  const unhide = useUnhideCall();
  const [confirmHide, setConfirmHide] = useState(false);
  const [hiddenId, setHiddenId] = useState<number | null>(null);
  const { data, isLoading, isError, error } = useCallDetail(callId, profile);

  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data) {
    const notFound = (error as { code?: string } | null)?.code === "not_found";
    return <Text role="alert" p="md">{notFound ? "Call not found." : "Couldn't load this call."}</Text>;
  }

  const { call } = data;

  return (
    <Stack component="main" maw="52rem" mx="auto" px="xs" pt="md" pb="xl" gap="lg">
      <Anchor component={Link} to="/" size="sm">← All calls</Anchor>

      <CallDetailBody {...data} />

      <Group component="section" aria-label="Hide this call" align="center" gap="sm">
        {hiddenId !== null ? (
          <>
            <Text span c="dimmed">Call hidden.</Text>
            <Button variant="subtle" size="xs" disabled={unhide.isPending}
                    onClick={async () => { await unhide.mutateAsync(hiddenId); setHiddenId(null); }}>
              Undo
            </Button>
            <Button variant="subtle" size="xs" onClick={() => navigate("/")}>
              Back to all calls
            </Button>
          </>
        ) : confirmHide ? (
          <>
            <Text span c="dimmed">Hide this call from your feed?</Text>
            <Button variant="default" size="xs" disabled={hide.isPending}
                    onClick={async () => { await hide.mutateAsync(call.id); setConfirmHide(false); setHiddenId(call.id); }}>
              Yes, hide it
            </Button>
            <Button variant="subtle" size="xs" onClick={() => setConfirmHide(false)}>
              Cancel
            </Button>
          </>
        ) : (
          <>
            <Button variant="default" size="xs" onClick={() => setConfirmHide(true)}>
              Hide this call
            </Button>
            <Text c="dimmed" fz="0.8rem">Removes it from your feed. Other users are unaffected.</Text>
          </>
        )}
      </Group>
    </Stack>
  );
}
