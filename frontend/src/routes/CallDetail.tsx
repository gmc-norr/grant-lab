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
  // THE URL IS THE STATE (D11). The reader arrived from a triage view on some
  // profile, and `to="/"` would land them in the Inbox of no profile in
  // particular — not where they were. `sp` is what they arrived with, verbatim.
  const backTo = sp.toString() ? `/?${sp.toString()}` : "/";
  const hide = useHideCall();
  const unhide = useUnhideCall();
  const [confirmHide, setConfirmHide] = useState(false);
  // Remember the profile the hide was made ON, so Undo deletes the same row
  // even if the URL changes underneath us.
  const [hidden, setHidden] = useState<{ id: number; profileId: number } | null>(null);
  const { data, isLoading, isError, error } = useCallDetail(callId, profile);

  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data) {
    const notFound = (error as { code?: string } | null)?.code === "not_found";
    return <Text role="alert" p="md">{notFound ? "Call not found." : "Couldn't load this call."}</Text>;
  }

  const { call } = data;

  return (
    <Stack component="main" maw="52rem" mx="auto" px="xs" pt="md" pb="xl" gap="lg">
      <Anchor component={Link} to={backTo} size="sm">← All calls</Anchor>

      <CallDetailBody {...data} />

      <Group component="section" aria-label="Hide this call" align="center" gap="sm">
        {hidden !== null ? (
          <>
            <Text span c="dimmed">Call hidden.</Text>
            <Button variant="subtle" size="xs" disabled={unhide.isPending}
                    onClick={async () => {
                      // No per-call onSuccess: useUnhideCall (api/calls.ts)
                      // invalidates ["calls"] AND ["decision-counts"] itself, so
                      // the feed's rail is current whether or not this page is
                      // still mounted when the DELETE lands. In THIS edition
                      // there is no rail mounted at all and the counts half is a
                      // no-op — which is exactly why it must not be this page's
                      // job to remember it.
                      await unhide.mutateAsync(hidden);
                      setHidden(null);
                    }}>
              Undo
            </Button>
            <Button variant="subtle" size="xs" onClick={() => navigate(backTo)}>
              Back to all calls
            </Button>
          </>
        ) : confirmHide ? (
          <>
            <Text span c="dimmed">Hide this call from your feed?</Text>
            <Button variant="default" size="xs" disabled={hide.isPending || profile === null}
                    onClick={async () => {
                      if (profile === null) return;
                      await hide.mutateAsync({ id: call.id, profileId: profile });
                      setConfirmHide(false);
                      setHidden({ id: call.id, profileId: profile });
                    }}>
              Yes, hide it
            </Button>
            <Button variant="subtle" size="xs" onClick={() => setConfirmHide(false)}>
              Cancel
            </Button>
          </>
        ) : (
          <>
            {/* A decision belongs to a profile: call_decisions.profile_id is NOT
                NULL, so with no ?profile= in scope there is nowhere to record the
                hide. The button is disabled rather than firing a 422 the user
                cannot act on. */}
            <Button variant="default" size="xs" disabled={profile === null}
                    onClick={() => setConfirmHide(true)}>
              Hide this call
            </Button>
            <Text c="dimmed" fz="0.8rem">
              {profile === null
                ? "Pick a profile to hide calls."
                : "Removes it from this profile's feed. Other users are unaffected."}
            </Text>
          </>
        )}
      </Group>
    </Stack>
  );
}
