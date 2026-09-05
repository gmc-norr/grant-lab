// frontend/src/routes/TriageCard.tsx
import { Link } from "react-router-dom";

import type { CallItem } from "../api/dashboard";
import type { TriageView } from "../api/decisions";
import { CriterionChips } from "../components/CriterionChips";
import { DeadlineLocal, deadlineLocalView } from "./Deadline";
import styles from "./TriageFeed.module.css";

// One call in the triage feed, in spec §5's field order — *what is this, what
// does it fund, why is it in front of me, can I apply*. The FOCUSED card is
// expanded with its evidence; every other card collapses to title, funder and
// days left, which is the whole reason the feed is one column instead of a
// mail-client split.
//
// NO RAW AGGREGATE, EVER. Spec §4.2 keeps the band for sorting and the digest
// and shows the number nowhere; §5's table lists ten fields and the aggregate is
// not one of them. This file therefore does not read that field at all, and
// TriageCard.test.tsx asserts BOTH halves of it — the rendered <article> does not
// contain the score, and this SOURCE does not name it. The RENDER check is the
// one with teeth: the source scan is a string scan, so a computed key walks
// straight past it.

function budgetText(call: CallItem): string | null {
  const { budget_min: lo, budget_max: hi, currency } = call;
  // "Often absent" per §5's table, and absent means absent: no "—" placeholder
  // row, because the card must read correctly with the optional fields missing
  // rather than showing a column of em-dashes.
  if (lo == null && hi == null) return null;
  const cur = currency ? ` ${currency}` : "";
  const fmt = (n: number) => n.toLocaleString("en-US");
  if (lo != null && hi != null) {
    // A RANGE when min differs from max — a single figure otherwise, because
    // "500 000 – 500 000" reads as two numbers a reader has to compare.
    return lo === hi ? `${fmt(lo)}${cur}` : `${fmt(lo)} – ${fmt(hi)}${cur}`;
  }
  return lo != null ? `From ${fmt(lo)}${cur}` : `Up to ${fmt(hi as number)}${cur}`;
}

// The matched sentence: the funder's own words, which spec §5 calls "the
// decision input", so it gets prime space rather than living behind a chip
// click. topic_fit is preferred because it is the criterion that answers "why is
// this in front of me"; any other quoted criterion is better than none.
function matchedSentence(call: CallItem): string | null {
  const topic = call.criteria.find((c) => c.is_topic_fit && c.quoted_evidence);
  const any = call.criteria.find((c) => c.quoted_evidence);
  return (topic ?? any)?.quoted_evidence ?? null;
}

function daysLeftText(call: CallItem): string | null {
  const local = call.deadline_local_date ?? call.deadline?.slice(0, 10) ?? null;
  if (local === null) return null;
  // The Stockholm calendar-day rule lives in ONE place (Deadline.tsx) and is
  // shared, not re-derived: it is the same arithmetic the urgency bands use.
  const { days } = deadlineLocalView(local);
  // A past deadline already reads "Closed · <date>" from DeadlineLocal; adding
  // "-9 days left" beside it would be noise, not information.
  if (days < 0) return null;
  if (days === 0) return "today";
  return days === 1 ? "1 day left" : `${days} days left`;
}

// THE URL IS THE STATE (D11). Both parameters travel to the detail page: the
// profile because it decides which ranking the page shows, the view because the
// page's back link has to return the reader to the list they came from.
function detailHref(id: number, profileId: number | null, view: TriageView): string {
  const params = new URLSearchParams();
  if (profileId != null) params.set("profile", String(profileId));
  params.set("view", view);
  return `/calls/${id}?${params.toString()}`;
}

export function TriageCard({
  call,
  expanded,
  profileId,
  view,
  onUndecide,
  cardRef,
}: {
  call: CallItem;
  expanded: boolean;
  profileId: number | null;
  view: TriageView;
  onUndecide?: (call: CallItem) => void;
  cardRef?: (el: HTMLElement | null) => void;
}) {
  const detailTo = detailHref(call.id, profileId, view);
  const budget = budgetText(call);
  const days = daysLeftText(call);
  const quote = matchedSentence(call);

  return (
    <article
      ref={cardRef}
      className={`${styles.card} ${expanded ? styles.cardExpanded : ""}`}
      data-call-id={call.id}
      data-expanded={expanded ? "true" : "false"}
      aria-current={expanded ? "true" : undefined}
      // Focusable so j/k can move REAL focus, not just a highlight — otherwise a
      // screen-reader user pressing j hears nothing move.
      tabIndex={-1}
    >
      <h2 className={styles.cardTitle}>
        <Link to={detailTo}>{call.title}</Link>
      </h2>

      <p className={styles.cardMeta}>
        <span data-field="funder">{call.source_name ?? "Unknown funder"}</span>
        {/* The separator is part of the programme, not of the funder: with no
            programme there is no dangling " · ". */}
        {call.programme && (
          <>
            <span aria-hidden> · </span>
            <span data-field="programme">{call.programme}</span>
          </>
        )}
      </p>

      <p className={styles.cardFacts}>
        {call.deadline && (
          <span data-field="deadline">
            <DeadlineLocal
              localDate={call.deadline_local_date ?? null}
              isoFallback={call.deadline}
            />
            {days && <span data-field="days-left"> · {days}</span>}
          </span>
        )}
        {budget && (
          <>
            {call.deadline && <span aria-hidden> · </span>}
            <span data-field="budget">{budget}</span>
          </>
        )}
      </p>

      {expanded && (
        <div className={styles.cardBody}>
          {call.summary ? (
            <p data-field="summary">{call.summary}</p>
          ) : (
            // §5: "Where Call.summary is absent, say so and link out rather than
            // collapsing the section: an empty space reads as a bug, a sentence
            // reads as a fact about the source." This is the COMMON case — the
            // field depends on the source adapter — so it is written as a fact,
            // not as an apology.
            <p data-field="no-summary" className={styles.noSummary}>
              This source publishes no description with the call.{" "}
              <a href={call.url} target="_blank" rel="noopener noreferrer">
                Read it at the funder →
              </a>
            </p>
          )}

          {quote && (
            <blockquote data-field="matched-sentence" className={styles.quote}>
              “{quote}”
            </blockquote>
          )}

          <CriterionChips criteria={call.criteria} />

          {call.keywords.length > 0 && (
            <p data-field="keywords" className={styles.keywords}>
              {call.keywords.map((kw) => (
                <span key={kw} className={styles.keyword}>
                  {kw}
                </span>
              ))}
            </p>
          )}

          {/* "N things to check" — escalated calls ONLY, and only when the
              remote path actually wrote notes. `escalated` without notes would
              promise a link to an empty section on the detail page. */}
          {call.escalated && call.applicant_note_count > 0 && (
            <p data-field="things-to-check">
              <Link to={detailTo}>
                {call.applicant_note_count === 1
                  ? "1 thing to check"
                  : `${call.applicant_note_count} things to check`}
              </Link>
            </p>
          )}

          {/* UN-DECIDE (D2). The `u` key does the same thing, but keys are
              undiscoverable and the spec's own scenario ("un-hide returns it to
              the inbox") was unperformable without a control. Saved and Hidden
              only: an inbox card has no decision row to delete. */}
          {view !== "inbox" && onUndecide && (
            <p className={styles.cardActions}>
              <button type="button" className={styles.undecide} onClick={() => onUndecide(call)}>
                Return to inbox
              </button>
            </p>
          )}
        </div>
      )}
    </article>
  );
}
