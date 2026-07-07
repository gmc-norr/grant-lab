import { Button, Group, Text } from "@mantine/core";
import { useEffect } from "react";
import { setSave } from "../cockpit/editorStore";
import type { CollabProvider, ProviderStatus } from "./collabProvider";
import { collabSaveState } from "./collabStatus";
import { useSaveVersion } from "./useSaveVersion";
import { useSettled } from "./useSettled";

export function SaveVersionButton(props: {
  id: string;
  artifact: "draft" | "spec";
  provider: CollabProvider;
  canWrite: boolean;
  status: ProviderStatus;
}) {
  const save = useSaveVersion(props.id, props.artifact);
  // Reactive settle state (socket open + drained + idle since last edit), polled via
  // the shared useSettled hook so this stays in sync with the selection toolbar.
  const settled = useSettled(props.provider);
  useEffect(() => {
    setSave(collabSaveState(props.status, props.canWrite));
  }, [props.status, props.canWrite]);
  const disabled = !props.canWrite || !settled || save.isPending;
  return (
    <Group gap="xs">
      {/* mutate() (not mutateAsync) so a rejected checkpoint is captured in
          save.isError instead of an unhandled promise rejection — a failed
          checkpoint must be SURFACED, not silently swallowed. */}
      <Button size="xs" variant="default" disabled={disabled} onClick={() => save.mutate()}>
        {save.isPending ? "Saving…" : "Save a version"}
      </Button>
      {save.isError && (
        <Text size="xs" c="red" role="alert">
          Save failed — retry
        </Text>
      )}
    </Group>
  );
}
