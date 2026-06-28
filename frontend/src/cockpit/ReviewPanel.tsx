import { Alert, Badge, Box, Button, Group, Stack, Text, Title } from "@mantine/core";
import { type ReviewItemKind, useLatestReviewRun, useReviewFile, useStartReviewRun } from "../api/review";
import type { ReviewRunItem, ReviewRunStatus } from "../api/review";
import styles from "./CockpitPanels.module.css";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const ITEM_LABEL: Record<ReviewItemKind, string> = {
  fit_check: "Fit check",
  persona_domain_skeptic: "Domain skeptic",
  persona_methodology_critic: "Methodology critic",
  persona_translation_skeptic: "Translation skeptic",
  persona_devils_advocate: "Devil's advocate",
  consolidation: "Consolidation",
};

const STATUS_COLOR: Record<string, string> = {
  done: "teal",
  running: "blue",
  queued: "gray",
  failed: "red",
  cancelled: "gray",
  partial: "yellow",
};

const ACTIVE_STATUSES: ReviewRunStatus[] = ["queued", "running"];

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function StatusPill({ status }: { status: string }) {
  return (
    <Badge size="sm" variant="light" color={STATUS_COLOR[status] ?? "gray"}>{status}</Badge>
  );
}

function ItemRow({ item }: { item: ReviewRunItem }) {
  return (
    <Group gap="sm">
      <StatusPill status={item.status} />
      <Text size="sm">{ITEM_LABEL[item.kind] ?? item.kind}</Text>
      {item.error_message && (
        <Text size="sm" c="red">{item.error_message}</Text>
      )}
    </Group>
  );
}

function ReviewDoc({
  title,
  markdown,
  isLoading,
}: {
  title: string;
  markdown: string | undefined;
  isLoading: boolean;
}) {
  if (isLoading) return <Text size="sm" c="dimmed">{title}: loading…</Text>;
  if (!markdown) return null;
  return (
    <Stack gap="xs" component="section">
      <Title order={3}>{title}</Title>
      <pre className={styles.reviewDoc}>{markdown}</pre>
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------

export function ReviewPanel({ id }: { id: string }) {
  const latest = useLatestReviewRun(id);
  const start = useStartReviewRun(id);
  const run = latest.data ?? null;

  const isActive = run != null && ACTIVE_STATUSES.includes(run.status as ReviewRunStatus);
  const budgetExhausted =
    run != null && run.budget.runs_used >= run.budget.runs_cap;

  // A review already in progress, or the daily cap being hit, genuinely blocks a
  // new run and disables the button. A failed POST does NOT — it must stay
  // retryable (a transient 503/network error would otherwise wedge the button
  // until the component remounts), so it surfaces as a separate, non-disabling
  // message. The backend detail is already surfaced via the {error:{message}}
  // envelope, so this string is specific (e.g. the budget/disclosure reason).
  let disabledReason: string | null = null;
  if (isActive) {
    disabledReason = "A review is already in progress.";
  } else if (budgetExhausted) {
    disabledReason = `Budget cap reached (${run!.budget.runs_used}/${run!.budget.runs_cap} runs).`;
  }

  const startError = start.isError
    ? ((start.error as Error)?.message ?? "Could not start review.")
    : null;

  const btnDisabled = disabledReason !== null || start.isPending;

  // Fetch the two markdown documents only when their sha fields are set
  const fitCheckFile = useReviewFile(
    id,
    run != null && run.fit_check_sha ? run.id : null,
    "fit-check.md",
  );
  const rubricReviewFile = useReviewFile(
    id,
    run != null && run.rubric_review_sha ? run.id : null,
    "rubric-review.md",
  );

  // Show whatever artifacts were produced, regardless of terminal status — a
  // run that ends `failed` (e.g. all personas failed) can still have committed a
  // useful fit-check.md. SHAs are only set in finalize, so their presence implies
  // a terminal run; an in-progress run has null SHAs and shows nothing.
  const showDocs =
    run != null && (run.fit_check_sha != null || run.rubric_review_sha != null);

  return (
    <Box component="main" p="md">
      <Stack gap="sm">
        {/* Header */}
        <Group gap="sm">
          <Title order={2}>Panel review</Title>
          {run?.stale && (
            <Badge color="yellow" variant="light" role="status">stale</Badge>
          )}
        </Group>

        {/* Run button + disabled reason */}
        <Group gap="sm">
          <Button size="xs" disabled={btnDisabled} onClick={() => start.mutate()}>
            Run full mock-panel review
          </Button>
          {disabledReason && (
            <Text role="status" size="sm" c="dimmed">
              {disabledReason}
            </Text>
          )}
          {startError && (
            <Text role="alert" size="sm" c="red">
              {startError} — you can try again.
            </Text>
          )}
        </Group>

        {/* Budget line */}
        {run && (
          <Text size="sm" c="dimmed">
            Budget: {run.budget.runs_used}/{run.budget.runs_cap} runs · $
            {Number(run.budget.dollars_used).toFixed(2)} / ${Number(run.budget.dollars_cap).toFixed(2)}
          </Text>
        )}

        {/* Active / partial / failed banner */}
        {run && (run.status === "partial" || run.status === "failed") && (
          <Alert role="status" variant="light">
            This review completed as <Text span fw={700}>{run.status}</Text>
            {run.items.some((i) => i.error_message) && " — some items encountered errors"}.
          </Alert>
        )}
        {isActive && (
          <Alert role="status" variant="light">
            Review in progress…
          </Alert>
        )}

        {/* Per-item progress */}
        {run && run.items.length > 0 && (
          <Stack gap="xs">
            {run.items.map((item) => (
              <ItemRow key={item.kind} item={item} />
            ))}
          </Stack>
        )}

        {/* Cost + timestamps */}
        {run && (
          <Text size="sm" c="dimmed">
            {run.estimated_dollars != null && (
              <span>Est. cost: ${Number(run.estimated_dollars).toFixed(2)}</span>
            )}
            {run.triggered_at && (
              <>
                {run.estimated_dollars != null ? " · " : ""}
                started {new Date(run.triggered_at).toLocaleString()}
              </>
            )}
            {run.completed_at && (
              <>
                {" · "}
                completed {new Date(run.completed_at).toLocaleString()}
              </>
            )}
          </Text>
        )}

        {/* Markdown documents (preformatted, no renderer dependency) */}
        {showDocs && (
          <Stack gap="sm">
            <ReviewDoc
              title="Fit check"
              markdown={fitCheckFile.data?.markdown}
              isLoading={fitCheckFile.isLoading}
            />
            <ReviewDoc
              title="Rubric review"
              markdown={rubricReviewFile.data?.markdown}
              isLoading={rubricReviewFile.isLoading}
            />
          </Stack>
        )}

        {/* Empty state */}
        {!run && !latest.isLoading && (
          <Text size="sm" c="dimmed">
            No panel review yet — run a full mock-panel review to get started.
          </Text>
        )}
      </Stack>
    </Box>
  );
}
