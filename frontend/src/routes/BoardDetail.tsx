import {
  Anchor, Badge, Box, Button, Card, Group, NumberInput, SegmentedControl, Stack, Text,
  TextInput, Title,
} from "@mantine/core";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { apiErrorMessage } from "../api/client";
import {
  useBoard, useBoardItems, usePublishBoard, useReorderItems, useUnapproveItem, useUpdateBoard,
  type Board, type BoardSettingsBody,
} from "../api/boards";
import { useSession } from "../api/session";
import { BoardQueue } from "./BoardQueue";
import { Deadline } from "./Deadline";

export function BoardDetail() {
  const { id } = useParams();
  const boardId = Number(id);
  const board = useBoard(boardId);
  const session = useSession();

  if (board.isLoading) return <Text p="md">Loading…</Text>;
  if (board.isError || !board.data) {
    // GET /api/boards/{id} is EDIT-gated, unlike the list, which is only
    // READ-gated -- a reader who can list this board still 403s opening it,
    // so that case gets its own message rather than the 404 copy.
    const code = (board.error as { code?: string } | null)?.code;
    const forbidden = code === "forbidden";
    return (
      <Box component="main" maw="48rem" mx="auto" px="xs" pt="md" pb="xl">
        <Text role="alert">
          {forbidden
            ? "You can see this board from the list, but only its owner or an admin can manage it."
            : "This board isn't shared with you, or it doesn't exist."}
        </Text>
        <Anchor component={Link} to="/boards" size="sm" mt="xs" display="inline-block">
          ← All boards
        </Anchor>
      </Box>
    );
  }

  // MINOR 8: `key={board.data.id}` is DEFENSIVE, not a caught bug -- this
  // component's own `if (board.isLoading) return <Text .../>` a few lines
  // above already unmounts `DetailView` for the duration of a
  // `/boards/1` -> `/boards/2` navigation (react-query's queryKey changes
  // with `id`, so `useBoard` returns `isPending` and the tree briefly
  // renders `<Text>` instead of `<DetailView>`), so `SettingsForm`'s
  // mount-seeded local state is already gone by the time board 2's data
  // arrives, with no `placeholderData`/`keepPreviousData` set to bridge that
  // gap. A prior version of this comment claimed the key had CAUGHT that
  // stale-form bug; it had not, because the scenario cannot occur. Kept
  // anyway as cheap insurance against a future change (e.g. adding
  // placeholderData to `useBoard` to remove the loading flash) that would
  // make the remount stop happening for free.
  return (
    <DetailView
      key={board.data.id}
      board={board.data}
      isAdmin={!!session.data?.capabilities.admin}
    />
  );
}

function DetailView({ board, isAdmin }: { board: Board; isAdmin: boolean }) {
  return (
    <Box component="main" maw="56rem" mx="auto" px="xs" pt="md" pb="xl">
      <Anchor component={Link} to="/boards" size="sm">
        ← All boards
      </Anchor>
      <Group justify="space-between" align="flex-start" wrap="nowrap" gap="md" my="md">
        <div>
          <Title order={1}>{board.title}</Title>
          <Text c="dimmed" size="sm">
            {board.orientation} · {board.approved_count} approved
          </Text>
        </div>
        <Anchor href={`/b/${board.slug}`} target="_blank" rel="noreferrer" size="sm">
          View public board →
        </Anchor>
      </Group>

      {board.owner_undeliverable && (
        // D9 / Open-3: a delivery the system knows it suppressed is the one
        // case an in-app surface is not optional. Independent of
        // board_alerts_enabled -- this is about REACHABILITY, not preference.
        <Text role="alert" size="sm" c="red" mb="md">
          The profile owner can't be notified about this board — no active email
          subscription was found for them.
        </Text>
      )}

      <PublishControl board={board} isAdmin={isAdmin} />

      <SettingsForm board={board} />

      <ApprovedItems board={board} />

      <Card withBorder radius="md" p="md" bg="var(--app-sidebar)" mb="md">
        <Title order={2} size="h5" mb="sm">
          Approval queue
        </Title>
        <BoardQueue board={board} />
      </Card>
    </Box>
  );
}

function SettingsForm({ board }: { board: Board }) {
  const update = useUpdateBoard(board.id);
  const [title, setTitle] = useState(board.title);
  const [orientation, setOrientation] = useState<Board["orientation"]>(board.orientation);
  const [itemsPerPage, setItemsPerPage] = useState<number>(board.items_per_page);
  // IMPORTANT 2: the server treats an explicit items_per_page as an OVERRIDE
  // that wins over its own orientation-driven default
  // (ORIENTATION_ITEMS_PER_PAGE, boards.py:348-353) -- so if this always sent
  // the seeded-at-mount value, flipping orientation without touching this
  // field would save the OLD page size under the NEW orientation (a landscape
  // 8/page flipped to portrait would save as portrait/8, four implicit rows
  // overflowing the fixed-height wall). Track whether the user actually
  // edited this field and omit it from the body when they have not, so the
  // server's own derivation runs.
  const [itemsPerPageTouched, setItemsPerPageTouched] = useState(false);
  const [minScore, setMinScore] = useState<number>(board.min_score);
  const [pageSeconds, setPageSeconds] = useState<number>(board.page_seconds);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const body: BoardSettingsBody = {
      title,
      orientation,
      min_score: minScore,
      page_seconds: pageSeconds,
      ...(itemsPerPageTouched ? { items_per_page: itemsPerPage } : {}),
    };
    update.mutate(body);
  }

  return (
    <Card withBorder radius="md" p="md" bg="var(--app-sidebar)" mb="md">
      <Title order={2} size="h5" mb="sm">
        Settings
      </Title>
      <form onSubmit={submit}>
        <Stack gap="sm">
          <TextInput
            label="Title"
            required
            value={title}
            onChange={(e) => setTitle(e.currentTarget.value)}
            description="Shown on the public wall once published — free text an author chose, so it should not name anything the profile's owner would not put on a corridor wall."
          />
          <SegmentedControl
            aria-label="Orientation"
            value={orientation}
            onChange={(v) => setOrientation(v as Board["orientation"])}
            data={[
              { label: "Landscape", value: "landscape" },
              { label: "Portrait", value: "portrait" },
            ]}
          />
          <Group grow>
            <NumberInput
              label="Items per page"
              description="1–64"
              min={1}
              max={64}
              value={itemsPerPage}
              onChange={(v) => {
                setItemsPerPageTouched(true);
                setItemsPerPage(typeof v === "number" ? v : itemsPerPage);
              }}
            />
            <NumberInput
              label="Minimum score"
              description="0–100"
              min={0}
              max={100}
              value={minScore}
              onChange={(v) => setMinScore(typeof v === "number" ? v : minScore)}
            />
            <NumberInput
              label="Seconds per page"
              description="1–3600"
              min={1}
              max={3600}
              value={pageSeconds}
              onChange={(v) => setPageSeconds(typeof v === "number" ? v : pageSeconds)}
            />
          </Group>
          <Group gap="sm" align="center">
            <Button type="submit" disabled={update.isPending}>
              Save settings
            </Button>
            {update.isError && (
              // Verbatim, numbers intact -- a capacity-shrinking save names
              // exactly how many approved calls to remove first.
              <Text role="alert" c="red" size="sm">
                {apiErrorMessage(update.error, "Couldn't save settings.")}
              </Text>
            )}
          </Group>
        </Stack>
      </form>
    </Card>
  );
}

function PublishControl({ board, isAdmin }: { board: Board; isAdmin: boolean }) {
  const publish = usePublishBoard(board.id);
  const [confirming, setConfirming] = useState(false);
  if (!isAdmin) return null;

  const errorText = publish.isError && (
    <Text role="alert" c="red" size="sm">
      {apiErrorMessage(publish.error, "Couldn't change publish state.")}
    </Text>
  );

  if (board.is_published) {
    return (
      <Group gap="sm" mb="md">
        <Badge color="teal">Published</Badge>
        <Button
          variant="default"
          size="compact-sm"
          onClick={() => publish.mutate(false)}
          disabled={publish.isPending}
        >
          Unpublish
        </Button>
        {errorText}
      </Group>
    );
  }

  if (confirming) {
    return (
      <Card withBorder radius="md" p="sm" mb="md" data-field="publish-confirm">
        {/* Open-5: names the CURRENT title and count, not a fixed sentence --
            the title is free text an author chose. */}
        <Text size="sm">
          Publish “{board.title}” with {board.approved_count} approved calls? It becomes visible
          to anyone with the link, with no login required — make sure the title does not name
          anything the profile's owner would not put on a public corridor wall.
        </Text>
        <Group gap="sm" mt="sm">
          <Button
            onClick={() => publish.mutate(true, { onSuccess: () => setConfirming(false) })}
            disabled={publish.isPending}
          >
            Confirm publish
          </Button>
          <Button variant="default" onClick={() => setConfirming(false)}>
            Cancel
          </Button>
        </Group>
        {errorText}
      </Card>
    );
  }

  return (
    <Group gap="sm" mb="md">
      <Button onClick={() => setConfirming(true)}>Publish</Button>
      {errorText}
    </Group>
  );
}

function ApprovedItems({ board }: { board: Board }) {
  const items = useBoardItems(board.id);
  const unapprove = useUnapproveItem(board.id);
  const reorder = useReorderItems(board.id);

  // IMPORTANT 3: PUT /api/boards/{id}/items is all-or-nothing -- it wants
  // the COMPLETE permutation of every approved call_id, not one moved id, so
  // moving a row rebuilds the whole ordered list and sends that.
  function move(index: number, direction: -1 | 1) {
    if (!items.data) return;
    const order = items.data.items.map((it) => it.call_id);
    const target = index + direction;
    if (target < 0 || target >= order.length) return;
    [order[index], order[target]] = [order[target], order[index]];
    reorder.mutate(order);
  }

  return (
    <Card withBorder radius="md" p="md" bg="var(--app-sidebar)" mb="md">
      <Title order={2} size="h5" mb="sm">
        Approved items ({board.approved_count})
      </Title>
      {items.isLoading && <Text size="sm">Loading…</Text>}
      {items.isError && (
        <Text role="alert" c="red" size="sm">
          Couldn't load approved items.
        </Text>
      )}
      {items.data && items.data.items.length === 0 && (
        <Text size="sm" c="dimmed">
          Nothing approved yet.
        </Text>
      )}
      {items.data && items.data.items.length > 0 && (
        <Stack gap={4}>
          {items.data.items.map((item, index) => (
            <Group key={item.call_id} justify="space-between" wrap="nowrap">
              <Group gap="sm" wrap="nowrap">
                <Group gap={2} wrap="nowrap">
                  <Button
                    variant="subtle"
                    size="compact-sm"
                    aria-label={`Move ${item.title} up`}
                    disabled={index === 0 || reorder.isPending}
                    onClick={() => move(index, -1)}
                  >
                    ↑
                  </Button>
                  <Button
                    variant="subtle"
                    size="compact-sm"
                    aria-label={`Move ${item.title} down`}
                    disabled={index === items.data.items.length - 1 || reorder.isPending}
                    onClick={() => move(index, 1)}
                  >
                    ↓
                  </Button>
                </Group>
                <Text size="sm">{item.title}</Text>
                <Text size="xs" c="dimmed">
                  {item.source_name}
                </Text>
                {item.deadline && <Deadline iso={item.deadline} />}
              </Group>
              <Button
                variant="subtle"
                size="compact-sm"
                aria-label={`Remove ${item.title}`}
                disabled={unapprove.isPending}
                onClick={() => unapprove.mutate(item.call_id)}
              >
                Remove
              </Button>
            </Group>
          ))}
        </Stack>
      )}
    </Card>
  );
}
