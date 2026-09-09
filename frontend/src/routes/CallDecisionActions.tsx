import { Alert, Button, Group, Stack, Text } from "@mantine/core";
import { useState } from "react";

import { useHideCall, useSetDecisionNote, useUnhideCall } from "../api/calls";
import { apiErrorMessage } from "../api/client";
import { useSaveCall, useUnsaveCall } from "../api/decisions";
import { DecisionNoteStrip } from "./DecisionNoteStrip";

type State = "saved" | "hidden" | null;

export function CallDecisionActions({
  callId, profileId, title, initialState, initialNote,
}: {
  callId: number;
  profileId: number | null;
  title: string;
  initialState: State;
  initialNote: string | null;
}) {
  const save = useSaveCall();
  const hide = useHideCall();
  const unsave = useUnsaveCall();
  const unhide = useUnhideCall();
  const setNote = useSetDecisionNote();
  const [override, setOverride] = useState<{ state: State; note: string | null } | null>(null);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const state = override ? override.state : initialState;
  const note = override ? override.note : initialNote;
  const busy = save.isPending || hide.isPending || unsave.isPending || unhide.isPending || setNote.isPending;

  async function decide(next: State) {
    if (profileId === null || busy) return;
    setError(null);
    const args = { id: callId, profileId };
    try {
      if (next === "saved") await save.mutateAsync(args);
      else if (next === "hidden") await hide.mutateAsync(args);
      else if (state === "saved") await unsave.mutateAsync(args);
      else await unhide.mutateAsync(args);
      setOverride({ state: next, note: next === state ? note : null });
      setEditing(false);
    } catch (failure) {
      setError(apiErrorMessage(failure, "Your decision could not be saved. Please try again."));
    }
  }

  return (
    <Stack component="section" aria-label="Your decision" gap="xs">
      <Group gap="sm">
        {state !== "saved" && <Button disabled={profileId === null || busy} onClick={() => void decide("saved")}>Save</Button>}
        {state !== "hidden" && <Button variant="default" disabled={profileId === null || busy} onClick={() => void decide("hidden")}>Hide</Button>}
        {state !== null && (
          <>
            <Text role="status" size="sm">{state === "saved" ? "Saved to this profile" : "Hidden from this profile"}</Text>
            <Button variant="subtle" disabled={busy} onClick={() => void decide(null)}>Return to inbox</Button>
          </>
        )}
      </Group>
      {profileId === null && <Text size="sm" c="dimmed">Select a profile from Calls to save or hide this opportunity.</Text>}
      {error && <Alert role="alert" color="red">{error}</Alert>}
      {state !== null && profileId !== null && (editing ? (
        <DecisionNoteStrip
          target={{ callId, profileId, title, state }}
          initialNote={note}
          autoFocus
          onDismiss={() => setEditing(false)}
          onSave={async args => {
            const result = await setNote.mutateAsync({ ...args, expectedState: state });
            setOverride({ state, note: result.decision_note });
          }}
        />
      ) : (
        <Button variant="subtle" size="sm" style={{ alignSelf: "flex-start", whiteSpace: "normal", height: "auto", textAlign: "left" }} onClick={() => setEditing(true)}>
          {note ?? "Add note"}
        </Button>
      ))}
    </Stack>
  );
}