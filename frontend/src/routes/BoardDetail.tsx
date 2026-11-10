import {
  Alert, Anchor, Badge, Box, Button, Card, Checkbox, Group, NumberInput, SegmentedControl,
  Stack,
  Text, TextInput, Title,
} from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { apiErrorMessage } from "../api/client";
import {
  BOARD_TITLE_MAX_CHARS,
  useAutoPreview, useBoard, useBoardItems, usePublishBoard, useReorderItems, useUnapproveItem,
  useUpdateBoard,
  type Board, type BoardSettingsBody,
} from "../api/boards";
import { useSession } from "../api/session";
import { BoardQueue } from "./BoardQueue";
import { Deadline } from "./Deadline";
import { BoardDraftPreview } from "./BoardDraftPreview";
import { ProfileEligibilityWarning } from "./EligibilityWarning";

export function BoardDetail() {
  const { id } = useParams();
  const boardId = Number(id);
  const board = useBoard(boardId);
  const session = useSession();
  const [params] = useSearchParams();

  if (board.isLoading) return <Text p="md">Loading…</Text>;
  if (board.isError || !board.data) {
    // GET /api/boards/{id} is EDIT-gated, unlike the list, which is only
    // READ-gated -- a reader who can list this board still 403s opening it,
    // so that case gets its own message rather than the 404 copy.
    const code = (board.error as { code?: string } | null)?.code;
    const forbidden = code === "forbidden";
    // THE THIRD BRANCH (review 2026-10-26, A8). Until now every error that was
    // not `forbidden` — a 500, a dropped connection, a 502 from the proxy — read
    // as "This board isn't shared with you, or it doesn't exist.", which sends
    // the owner of a perfectly good board hunting for a sharing setting that was
    // never the problem. Only `not_found` means that; everything else is the
    // server failing, and says so.
    const missing = code === "not_found";
    return (
      <Box component="main" maw="48rem" mx="auto" px="xs" pt="md" pb="xl">
        <Text role="alert">
          {forbidden
            ? "You can see this board from the list, but only its owner or an admin can manage it."
            : missing
              ? "This board isn't shared with you, or it doesn't exist."
              : "Couldn't load this board. Something went wrong on the server — try again in a moment."}
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
  if (params.get("preview") === "1") return <BoardDraftPreview boardId={boardId} />;
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
        {board.is_published && <Anchor href={`/b/${board.slug}`} target="_blank" rel="noreferrer" size="sm">
          View public board →
        </Anchor>}
      </Group>

      {board.owner_undeliverable && (
        // D9 / Open-3: a delivery the system knows it suppressed is the one
        // case an in-app surface is not optional. Independent of
        // board_alerts_enabled -- this is about REACHABILITY, not preference.
        <Text role="alert" size="sm" c="red" mb="md">
          The profile owner can't be notified about this board by email, so
          alerts about it go to admins instead. (Their account may be inactive,
          their digest may be bounce-disabled, or they may have no subscription
          to this profile.)
        </Text>
      )}

      {/* Above everything that counts cards. Until 2026-11-10 an unfilled
          profile capped every score at 50 and left a board with the default
          minimum of 60 empty; the cap is gone, and the warning now says only
          that eligibility can't be checked. */}
      <ProfileEligibilityWarning profileId={board.profile_id} />

      <PublishControl board={board} isAdmin={isAdmin} />

      <BoardHealth board={board} />

      <BoardPreview board={board} />

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

/* "x hours ago", locally. A deliberate small duplicate of a date helper, not an
   oversight. Relative, because the question board health answers is "is this
   stale?", and a reader cannot subtract two timestamps at a glance. */
function agoText(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 90) return "just now";
  const units: [number, string][] = [
    [60, "minute"],
    [3600, "hour"],
    [86400, "day"],
  ];
  let value = seconds / 60;
  let unit = "minute";
  for (const [size, name] of units) {
    if (seconds >= size) {
      value = seconds / size;
      unit = name;
    }
  }
  const n = Math.floor(value);
  return `${n} ${unit}${n === 1 ? "" : "s"} ago`;
}

/* BOARD HEALTH. A corridor wall that quietly stopped updating looks exactly
   like one that is working -- the cards are still there, they are just
   yesterday's -- so the only way to know is to say when the job last ran.

   Shown for AUTO boards only: a manual board has no nightly run to be late,
   and a "last refreshed" line on one would invent a process that does not
   exist. */
function BoardHealth({ board }: { board: Board }) {
  if (!board.auto_publish) return null;

  const attempted = board.last_reconcile_attempt_at;
  const succeeded = board.last_reconciled_at;
  const error = board.last_reconcile_error;

  return (
    <Card withBorder radius="md" p="md" bg="var(--app-sidebar)" mb="md">
      <Title order={2} size="h5" mb="sm">
        Automatic refresh
      </Title>
      {attempted === null ? (
        /* NULL is "never", and must never render as a date. Every board that
           existed when this shipped reads this way until its first run. */
        <Text size="sm" c="dimmed" data-field="health-never">
          Hasn&apos;t run yet. This board fills itself on the next nightly run.
        </Text>
      ) : error !== null ? (
        <Alert role="alert" color="red" data-field="health-error">
          <Text size="sm">
            The last refresh failed {agoText(attempted)}: {error}
          </Text>
          {/* The pair is the point. "Failed" alone does not say whether the wall
              is showing week-old cards or last night's. */}
          <Text size="sm" mt={4}>
            {succeeded === null
              ? "This board has never refreshed successfully, so it is empty or stale."
              : `Last successful refresh: ${agoText(succeeded)}. The wall is showing what it published then.`}
          </Text>
        </Alert>
      ) : (
        <Text size="sm" c="dimmed" data-field="health-ok">
          Last refreshed {agoText(attempted)}.
        </Text>
      )}
    </Card>
  );
}

/* The sizes these boards are actually authored for (spec Sec 6). A preview at
   any OTHER size is the reason the wall shipped broken: at 1920x1080 with real
   funding titles the page grew to 1745px and the second row sat below the
   screen, and nothing anybody could look at on this page would have shown it. */
const TARGET_SIZE = {
  landscape: { width: 1920, height: 1080 },
  portrait: { width: 1080, height: 1920 },
} as const;

/* Render at the authored size, scaled to fit. Drafts use an edit-gated
  endpoint with the same presentation as the public board. Loading the
  second SPA is opt-in. */
function BoardPreview({ board }: { board: Board }) {
  const [shown, setShown] = useState(false);
  const [width, setWidth] = useState(0);
  const box = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const measure = () => setWidth(el.clientWidth);
    measure();
    // Measured rather than assumed: a hard-coded panel width would squash the
    // preview on a narrow window, and a squashed preview cannot answer the one
    // question it exists for.
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [shown]);

  const target = TARGET_SIZE[board.orientation];
  const scale = width > 0 ? width / target.width : 0;

  return (
    <Card withBorder radius="md" p="md" bg="var(--app-sidebar)" mb="md">
      <Group justify="space-between" mb="sm">
        <Title order={2} size="h5">
          Preview at {target.width}x{target.height}
        </Title>
        <Button size="compact-sm" variant="light" onClick={() => setShown((s) => !s)}>
          {shown ? "Hide preview" : "Show preview"}
        </Button>
      </Group>
      {!shown ? (
        <Text size="sm" c="dimmed" data-field="preview-unpublished">
          {board.is_published ? "Published board" : "Private draft preview"}
        </Text>
      ) : (
        <div ref={box} style={{ width: "100%" }} data-field="preview-frame">
          <div
            style={{
              width: "100%",
              height: scale > 0 ? target.height * scale : 0,
              overflow: "hidden",
              position: "relative",
              borderRadius: 4,
            }}
          >
            <iframe
              key={`${board.updated_at}:${board.approved_count}`}
              title={`${board.title} at ${target.width}x${target.height}`}
              src={board.is_published ? `/b/${board.slug}` : `/boards/${board.id}?preview=1`}
              style={{
                width: target.width,
                height: target.height,
                border: 0,
                position: "absolute",
                top: 0,
                left: 0,
                transform: `scale(${scale})`,
                transformOrigin: "top left",
              }}
            />
          </div>
        </div>
      )}
    </Card>
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
  // RECONCILE WITH THE SERVER'S ANSWER. `items_per_page` is the one setting this
  // form does not decide: when the field is untouched the server DERIVES it from
  // the orientation, so after a flip to portrait the board really holds 4 and
  // this input still read the seeded 8 -- a number that was simply false, and
  // which the next save would then send back as an explicit override, undoing
  // the derivation.
  //
  // Only this field needs it. Every other setting is sent as typed and echoed
  // back unchanged, so it cannot disagree with the response.
  useEffect(() => {
    setItemsPerPage(board.items_per_page);
    // The saved value IS the value now, so the override is no longer pending.
    setItemsPerPageTouched(false);
  }, [board.items_per_page]);
  const [minScore, setMinScore] = useState<number>(board.min_score);
  const [pageSeconds, setPageSeconds] = useState<number>(board.page_seconds);
  const [autoPublish, setAutoPublish] = useState<boolean>(board.auto_publish);
  // Unlike items_per_page, this needs no "touched" tracking: the server has no
  // orientation-driven default for max_pages to derive, so sending the seeded
  // value back is a no-op rather than a clobber.
  const [maxPages, setMaxPages] = useState<number>(board.max_pages);

  // The settings Save would send, minus the two the preview has no use for:
  // `title` changes nothing it measures, and `auto_publish` is the switch that
  // decides whether to ask at all. ONE object feeds both the preview and the
  // submit body, so the numbers on screen are answers about the request the
  // button will actually make -- including the `itemsPerPageTouched` rule, which
  // a second copy would be free to get wrong in exactly the case it exists for.
  const pendingSettings = useMemo(
    () => ({
      orientation,
      min_score: minScore,
      page_seconds: pageSeconds,
      max_pages: maxPages,
      ...(itemsPerPageTouched ? { items_per_page: itemsPerPage } : {}),
    }),
    [orientation, minScore, pageSeconds, maxPages, itemsPerPageTouched, itemsPerPage],
  );
  // Debounced because these are NumberInputs: without it every keystroke in the
  // threshold is a request, and the intermediate values are ones the admin is
  // typing THROUGH rather than asking about. 300ms matches CallsFilters.
  const [debouncedSettings] = useDebouncedValue(pendingSettings, 300);

  // Only fetched while the switch is on -- the numbers are about what an auto
  // board would carry, and a manual board's owner has not asked the question.
  const preview = useAutoPreview(board.id, { enabled: autoPublish, query: debouncedSettings });

  // The switch is being turned ON and there is hand-picked work on the board
  // that the next nightly run would replace. Shown BEFORE saving, because the
  // PUT itself has no place to warn: a partial update carries no confirmation
  // step, and the loss happens hours later at 05:00 rather than on the save.
  const replacesExistingCards = autoPublish && !board.auto_publish && board.approved_count > 0;

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const body: BoardSettingsBody = { title, auto_publish: autoPublish, ...pendingSettings };
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
            maxLength={BOARD_TITLE_MAX_CHARS}
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
            {/* The only lever that bounds an auto board's loop length, and
                therefore the only way to fix a board the preview shows as
                truncated. Bounds mirror the API's own ge=1/le=64 so the
                control cannot send a 422. */}
            <NumberInput
              label="Maximum pages"
              description="1–64"
              min={1}
              max={64}
              value={maxPages}
              onChange={(v) => setMaxPages(typeof v === "number" ? v : maxPages)}
            />
          </Group>
          {/* "Fill", never "Publish". Two elements below, PublishControl's
              own button says Publish/Unpublish and its confirmation says "It
              becomes visible to anyone with the link" -- a different act, and
              the one place in this product where the spec insists the two stay
              visibly distinct: auto-publish FILLS a board, it never makes one
              world-readable. Sharing the verb across two adjacent controls is
              how an admin comes to believe this checkbox publishes the wall. */}
          <Checkbox
            label="Fill this board automatically"
            description="Each night at 05:00 this board is rebuilt from the calls that clear its minimum score."
            checked={autoPublish}
            onChange={(e) => setAutoPublish(e.currentTarget.checked)}
          />
          {autoPublish && preview.data && (
            // Five numbers, not one: `qualifying` minus `displayed` IS the
            // truncation, and a preview that showed only the displayed count
            // would hide exactly the failure it exists to surface.
            <Text size="sm" c="dimmed" data-field="auto-preview">
              {preview.data.qualifying} qualify · {preview.data.displayed} shown ·{" "}
              {preview.data.truncated} not shown · {preview.data.pages} pages ·{" "}
              {preview.data.loop_seconds}s loop
            </Text>
          )}
          {replacesExistingCards && (
            <Text role="alert" size="sm" c="orange" data-field="auto-publish-warning">
              This replaces the {board.approved_count} cards on this board at the next nightly run.
            </Text>
          )}
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
      {/* WRITE FAILURES, SAID OUT LOUD. An injected 500 left the card sitting on
          the board with no message anywhere -- and this is the control an admin
          reaches for to take something inappropriate off a PUBLIC wall, so
          "looks like nothing happened" is the worst answer the screen can give.
          Reorder gets the same treatment: it is all-or-nothing too, so a failed
          move silently leaves the wall in the old order while the rows on screen
          have already been re-read from a cache that never changed. */}
      {unapprove.isError && (
        <Alert role="alert" color="red" mb="sm" data-field="unapprove-error">
          <Group gap="sm">
            <Text size="sm">Couldn&apos;t remove that call: {apiErrorMessage(unapprove.error, "the server refused it")}</Text>
            <Button
              size="compact-xs"
              variant="light"
              onClick={() => unapprove.variables !== undefined && unapprove.mutate(unapprove.variables)}
            >
              Try again
            </Button>
          </Group>
        </Alert>
      )}
      {reorder.isError && (
        <Alert role="alert" color="red" mb="sm" data-field="reorder-error">
          <Group gap="sm">
            <Text size="sm">Couldn&apos;t reorder the board: {apiErrorMessage(reorder.error, "the server refused it")}</Text>
            <Button
              size="compact-xs"
              variant="light"
              onClick={() => reorder.variables !== undefined && reorder.mutate(reorder.variables)}
            >
              Try again
            </Button>
          </Group>
        </Alert>
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
