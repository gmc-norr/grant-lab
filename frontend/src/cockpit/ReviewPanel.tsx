import { type ReviewItemKind, useLatestReviewRun, useReviewFile, useStartReviewRun } from "../api/review";
import type { ReviewRunItem, ReviewRunStatus } from "../api/review";

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

const STATUS_CLASS: Record<string, string> = {
  done: "review-status-done",
  running: "review-status-running",
  queued: "review-status-queued",
  failed: "review-status-failed",
  cancelled: "review-status-cancelled",
  partial: "review-status-partial",
};

const ACTIVE_STATUSES: ReviewRunStatus[] = ["queued", "running"];

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function StatusPill({ status }: { status: string }) {
  return (
    <span className={`review-status-pill ${STATUS_CLASS[status] ?? ""}`}>{status}</span>
  );
}

function ItemRow({ item }: { item: ReviewRunItem }) {
  return (
    <li className="review-item-row">
      <StatusPill status={item.status} />
      <span className="review-item-kind">{ITEM_LABEL[item.kind] ?? item.kind}</span>
      {item.error_message && (
        <span className="review-item-error">{item.error_message}</span>
      )}
    </li>
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
  if (isLoading) return <p className="review-doc-loading">{title}: loading…</p>;
  if (!markdown) return null;
  return (
    <section className="review-doc-section">
      <h2 className="review-doc-title">{title}</h2>
      <pre className="review-doc">{markdown}</pre>
    </section>
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

  // Determine disabled reason for the run button
  let disabledReason: string | null = null;
  if (start.isError) {
    disabledReason = (start.error as Error)?.message ?? "Could not start review.";
  } else if (isActive) {
    disabledReason = "A review is already in progress.";
  } else if (budgetExhausted) {
    disabledReason = `Budget cap reached (${run!.budget.runs_used}/${run!.budget.runs_cap} runs).`;
  }

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

  const showDocs =
    run != null && (run.status === "done" || run.status === "partial");

  return (
    <main className="review-shell">
      {/* Header */}
      <div className="review-header">
        <h1>Panel review</h1>
        {run?.stale && (
          <span className="review-stale-badge" role="status">
            stale
          </span>
        )}
      </div>

      {/* Run button + disabled reason */}
      <div className="review-controls">
        <button
          type="button"
          disabled={btnDisabled}
          onClick={() => start.mutate()}
        >
          Run full mock-panel review
        </button>
        {disabledReason && (
          <span className="review-disabled-reason" role="status">
            {disabledReason}
          </span>
        )}
      </div>

      {/* Budget line */}
      {run && (
        <p className="review-budget">
          Budget: {run.budget.runs_used}/{run.budget.runs_cap} runs · $
          {run.budget.dollars_used.toFixed(2)} / ${run.budget.dollars_cap.toFixed(2)}
        </p>
      )}

      {/* Active / partial / failed banner */}
      {run && (run.status === "partial" || run.status === "failed") && (
        <div className="review-banner" role="status">
          This review completed as <strong>{run.status}</strong>
          {run.items.some((i) => i.error_message) && " — some items encountered errors"}.
        </div>
      )}
      {isActive && (
        <div className="review-banner" role="status">
          Review in progress…
        </div>
      )}

      {/* Per-item progress */}
      {run && run.items.length > 0 && (
        <ul className="review-items">
          {run.items.map((item) => (
            <ItemRow key={item.kind} item={item} />
          ))}
        </ul>
      )}

      {/* Cost + timestamps */}
      {run && (
        <p className="review-meta">
          Est. cost: ${run.estimated_dollars.toFixed(2)}
          {run.triggered_at && (
            <>
              {" · "}
              started {new Date(run.triggered_at).toLocaleString()}
            </>
          )}
          {run.completed_at && (
            <>
              {" · "}
              completed {new Date(run.completed_at).toLocaleString()}
            </>
          )}
        </p>
      )}

      {/* Markdown documents (preformatted, no renderer dependency) */}
      {showDocs && (
        <div className="review-docs">
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
        </div>
      )}

      {/* Empty state */}
      {!run && !latest.isLoading && (
        <p className="review-empty">
          No panel review yet — run a full mock-panel review to get started.
        </p>
      )}
    </main>
  );
}
