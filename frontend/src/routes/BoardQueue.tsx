import { Badge, Button, Checkbox, Group, NumberInput, Stack, Text } from "@mantine/core";
import { useState } from "react";

import { apiErrorMessage } from "../api/client";
import { QUEUE_PAGE_SIZE, useApproveItems, useBoardQueue, type Board, type QueueCall } from "../api/boards";
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

  // An emptied field is "no threshold chosen", not zero -- Number("") is 0 and
  // Number.isFinite(0) is true, so without this an emptied input would arm a
  // whole-queue approval (min_score: 0) with no confirmation on a bulk action.
  const scoreFloorIsEmpty = minScoreInput === "";

  function approveAtOrAbove() {
    if (scoreFloorIsEmpty) return;
    const n = Number(minScoreInput);
    if (!Number.isFinite(n)) return;
    // Clear the selection here too (MINOR 7): otherwise a call approved by
    // this path keeps its id in `selected` after the refetch drops its
    // checkbox, leaving "Approve selected (N)" showing a stale count.
    approve.mutate({ min_score: n }, { onSuccess: () => setSelected(new Set()) });
  }

  return (
    <Stack gap="sm">
      <Group justify="space-between">
        <Text size="sm">
          <Text span fw={700}>
            {queue.data.waiting_count}
          </Text>{" "}
          waiting for a decision
          {queue.data.items.length >= QUEUE_PAGE_SIZE && (
            // The list is one page of QUEUE_PAGE_SIZE, but `waiting_count` is
            // the WHOLE waiting set -- so without this an admin working a long
            // queue cannot tell they are deciding on a slice. (Both final
            // reviews flagged the silent truncation.)
            <Text span c="dimmed">
              {" "}
              — showing the first {QUEUE_PAGE_SIZE}
            </Text>
          )}
        </Text>
        <Text size="sm" c="dimmed">
          {board.approved_count} of {cap} slots used
        </Text>
      </Group>

      <Group align="flex-end" gap="sm">
        <NumberInput
          label="Approve everything at or above"
          description="0–100"
          min={0}
          max={100}
          value={minScoreInput}
          onChange={setMinScoreInput}
          w={140}
        />
        <Button
          variant="default"
          onClick={approveAtOrAbove}
          disabled={approve.isPending || scoreFloorIsEmpty}
        >
          Approve all ≥ {scoreFloorIsEmpty ? "…" : minScoreInput}
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
              ) : row.suppressed ? (
                /* Someone took this off this board. It stays in the list so
                   the reason is visible -- a row that just vanished would look
                   like a ranking change. No checkbox: "approve everything at
                   or above N" deliberately skips it, and ticking it here would
                   promise an action the bulk path will not take.

                   RESTORE (Astra, Medium). The comment here used to end "re-
                   approving is still possible by naming the call" -- true of the
                   API and false of this screen, which offered no way to name
                   one. Removal was therefore a one-way door in the UI. This
                   button names exactly this call, which is the explicit,
                   per-call signal `approve_items` treats as newer and stronger
                   than the suppression; the bulk threshold sweep still skips it,
                   so an exception list can only be undone deliberately, one row
                   at a time. */
                <Group gap="xs" wrap="nowrap">
                  <Text size="sm" c="dimmed">
                    {row.title}
                  </Text>
                  <Badge color="gray" size="sm">
                    Removed from this board
                  </Badge>
                  <Button
                    size="compact-xs"
                    variant="light"
                    disabled={approve.isPending}
                    onClick={() => approve.mutate({ call_ids: [row.call_id] })}
                  >
                    Restore to board
                  </Button>
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
