import { Badge, Button, Checkbox, Group, NumberInput, Stack, Text } from "@mantine/core";
import { useState } from "react";

import { apiErrorMessage } from "../api/client";
import { useApproveItems, useBoardQueue, type Board, type QueueCall } from "../api/boards";
import { Deadline } from "./Deadline";

/** The admin approval checklist. Spec §4.3: an approval UI that costs a
 *  minute per call gets abandoned, and an abandoned gate is worse than no
 *  gate because the board silently stops updating. So this ships all three
 *  affordances the spec names, not two of them:
 *
 *  1. a checklist ordered by score, highest first
 *  2. "approve everything at or above N" -- one request, not one per call
 *  3. a visible count of what is waiting
 *
 *  D3: an already-approved candidate stays in the list (flagged, not
 *  checkable again), because hiding it would make the checklist a worse
 *  record of the board's state than the queue actually has.
 *
 *  D7: capacity is read off `board.items_per_page * board.max_pages` --
 *  the STORED setting, never a hardcoded 24 -- so a portrait board (4 per
 *  page) shows its true, smaller cap.
 *
 *  The 409 a bulk approve can return (capacity exceeded) is rendered
 *  VERBATIM, numbers intact: it already names exactly how many to drop, and
 *  replacing it with a generic message would throw that away.
 */
export function BoardQueue({ board }: { board: Board }) {
  const queue = useBoardQueue(board.id);
  const approve = useApproveItems(board.id);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [minScoreInput, setMinScoreInput] = useState<number | string>(board.min_score);

  const cap = board.items_per_page * board.max_pages;

  if (queue.isLoading) return <Text size="sm">Loading the queue…</Text>;
  if (queue.isError || !queue.data) {
    return (
      <Text role="alert" size="sm" c="red">
        Couldn't load the approval queue.
      </Text>
    );
  }

  // Highest score first -- trusted DEFENSIVELY, not assumed from the fetch
  // order, so a future change to the API's own ordering cannot silently
  // reverse the checklist's most important property.
  const rows = [...queue.data.items].sort((a, b) => b.score - a.score);

  function toggle(callId: number, checked: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) next.add(callId);
      else next.delete(callId);
      return next;
    });
  }

  function approveSelected() {
    const call_ids = [...selected];
    if (call_ids.length === 0) return;
    approve.mutate({ call_ids }, { onSuccess: () => setSelected(new Set()) });
  }

  function approveAtOrAbove() {
    const n = Number(minScoreInput);
    if (!Number.isFinite(n)) return;
    approve.mutate({ min_score: n });
  }

  return (
    <Stack gap="sm">
      <Group justify="space-between">
        <Text size="sm">
          <Text span fw={700}>
            {queue.data.waiting_count}
          </Text>{" "}
          waiting for a decision
        </Text>
        <Text size="sm" c="dimmed">
          {board.approved_count} of {cap} slots used
        </Text>
      </Group>

      <Group align="flex-end" gap="sm">
        <NumberInput
          label="Approve everything at or above"
          min={0}
          max={100}
          value={minScoreInput}
          onChange={setMinScoreInput}
          w={140}
        />
        <Button variant="default" onClick={approveAtOrAbove} disabled={approve.isPending}>
          Approve all ≥ {minScoreInput || 0}
        </Button>
      </Group>

      {rows.length === 0 ? (
        <Text size="sm" c="dimmed">
          Nothing waiting right now.
        </Text>
      ) : (
        <Stack gap={4}>
          {rows.map((row: QueueCall) => (
            <Group key={row.call_id} data-field="queue-row" wrap="nowrap" justify="space-between">
              {row.already_approved ? (
                <Group gap="xs" wrap="nowrap">
                  <Text size="sm">{row.title}</Text>
                  <Badge color="teal" size="sm">
                    Approved
                  </Badge>
                </Group>
              ) : (
                <Checkbox
                  label={row.title}
                  checked={selected.has(row.call_id)}
                  onChange={(e) => toggle(row.call_id, e.currentTarget.checked)}
                />
              )}
              <Group gap="sm" wrap="nowrap">
                <Text size="xs" c="dimmed">
                  {row.source_name}
                </Text>
                <Text size="xs" c="dimmed">
                  Score {row.score}
                </Text>
                {row.deadline && <Deadline iso={row.deadline} />}
              </Group>
            </Group>
          ))}
        </Stack>
      )}

      <Group gap="sm" align="center">
        <Button onClick={approveSelected} disabled={approve.isPending || selected.size === 0}>
          Approve selected ({selected.size})
        </Button>
        {approve.isError && (
          <Text role="alert" c="red" size="sm">
            {apiErrorMessage(approve.error, "Couldn't approve.")}
          </Text>
        )}
      </Group>
    </Stack>
  );
}
