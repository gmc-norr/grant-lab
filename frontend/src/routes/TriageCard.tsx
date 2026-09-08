// frontend/src/routes/TriageCard.tsx
import { useRef, useState } from "react";
import { Link } from "react-router-dom";

import { useSetDecisionNote } from "../api/calls";
import type { CallItem } from "../api/dashboard";
import type { TriageView } from "../api/decisions";
import { CriterionChips } from "../components/CriterionChips";
import { DeadlineLocal, deadlineLocalView } from "./Deadline";
import { DecisionNoteStrip } from "./DecisionNoteStrip";
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

// THE REASON, ON THE ROW IT BELONGS TO (D2, D4). Two halves, and the second is
// the one that is easy to leave out: the feed's strip appears once, at the
// moment of the decision, and is deliberately ignorable (D2) -- so a surface
// that only DISPLAYS an existing note is a surface most people can never write
// on. "I'll say why later" has to be reachable from the row itself.
//
// Saved and Hidden only, and only with a profile resolved: a note hangs off a
// `call_decisions` row whose `profile_id` is NOT NULL, and the Inbox has no
// such row -- an affordance there would offer to annotate a decision that does
// not exist, and the endpoint would 404.
//
// Its own component, not a branch inside TriageCard, so `useSetDecisionNote`
// is mounted on the rows that can actually write and on no others.
function DecisionNote({
  call,
  profileId,
  state,
}: {
  call: CallItem;
  profileId: number;
  state: "saved" | "hidden";
}) {
  const [editing, setEditing] = useState(false);
  const setNote = useSetDecisionNote();
  const note = call.decision_note;
  // WHETHER THE CARET STILL BELONGS IN THE BOX, across the remount a save
  // causes. The click that opens the editor puts it there; an Enter-commit
  // leaves it there; a BLUR-commit means the user went somewhere else and the
  // remount must not fetch them back.
  //
  // This ref is why the fix lives here and not in the strip: the key below
  // remounts the strip on every successful save, so the strip's own refs are
  // thrown away exactly when this bit is needed. THIS component is not
  // remounted -- it is the same `DecisionNote` for the same row -- so it is
  // where the bit can survive.
  //
  // A ref rather than state, deliberately: it must not schedule a render (a
  // render mid-typing is how the seed goes stale), and it is read during the
  // render the key change triggers, by which time `onCommit` has already
  // written it synchronously.
  const keepCaret = useRef(true);

  if (editing) {
    return (
      <DecisionNoteStrip
        // KEYED ON THE CALL ONLY. This used to include the note, to make the
        // strip re-seed when the stored value changed underneath it (the strip
        // trims before sending, D8, so "  padded  " is stored as "padded").
        // But the note's everyday change is this editor's OWN acknowledgement,
        // so the key turned each save into a remount mid-sentence: type
        // "First", press Enter, keep typing, and the PUT's return snapped the
        // box back to "First". The strip now adopts a new `initialNote` itself,
        // and only while the box is clean.
        key={call.id}
        target={{ callId: call.id, profileId, title: call.title, state }}
        // OPTIONAL on the strip because Task 6 only ever creates. Passed here
        // because this surface EDITS, and a strip with no way to receive the
        // current value can only ever replace it from blank.
        initialNote={note}
        // The box is here because the user CLICKED for it, and that click
        // unmounted the button they clicked -- so without this, focus falls to
        // <body> while a text box is visibly open, the feed's window listener
        // stays live, and `u` on that row deletes the decision the note hangs
        // off (D4). See the prop's comment on DecisionNoteStrip.
        //
        // NOT a bare `autoFocus`, and that is the composition bug this pair
        // fixes. Both halves are individually right -- the key stops a stale
        // seed, the focus effect stops an unfocused-but-open box leaving the
        // feed's `u` live over the decision the note hangs off -- and together
        // they stole the caret back after EVERY successful save. Measured
        // 2026-09-07: after a blur-commit the caret returned to the box the
        // user had left, `isEditableTarget` then stood the window listener
        // down, and `j`/`k`/`s`/`h`/`u` were silently dead (D2 promises the
        // opposite); with a second row's editor open, the yanked caret sent
        // that row's reason into THIS row's note -- the mis-filing D10 and D11
        // exist to prevent, arriving through focus instead of through ids.
        autoFocus={keepCaret.current}
        onCommit={(via) => {
          keepCaret.current = via === "enter";
        }}
        // expectedState is the ROW's, supplied by the caller rather than read
        // off the strip: the strip must not carry two ways to say which
        // decision it is about (D11). The server 409s a note filed against a
        // decision that has flipped since the box was opened.
        onSave={(args) => setNote.mutateAsync({ ...args, expectedState: state })}
        onDismiss={() => setEditing(false)}
      />
    );
  }

  return (
    <p className={styles.cardNote}>
      {note !== null ? (
        <button
          type="button"
          data-field="decision-note"
          className={styles.noteText}
          // The rendered text is clamped to two lines; this gives the whole of
          // it to a hover without adding a second control to the row.
          title={note}
          // RE-ARM. `keepCaret` is false after a blur-commit, and this click is
          // the user coming back -- without this, reopening an editor that was
          // last left by clicking away would open unfocused, which is the
          // data-loss state `autoFocus` exists to prevent.
          onClick={() => {
            keepCaret.current = true;
            setEditing(true);
          }}
        >
          {note}
        </button>
      ) : (
        <button
          type="button"
          className={styles.noteAdd}
          onClick={() => {
            keepCaret.current = true;
            setEditing(true);
          }}
        >
          Add note
        </button>
      )}
    </p>
  );
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
  onSave,
  onHide,
  onFocus,
  busy = false,
  cardRef,
}: {
  call: CallItem;
  expanded: boolean;
  profileId: number | null;
  view: TriageView;
  onUndecide?: (call: CallItem) => void;
  onSave?: (call: CallItem) => void;
  onHide?: (call: CallItem) => void;
  onFocus?: () => void;
  busy?: boolean;
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
      // REPORT REAL FOCUS BACK (Astra, High). `focusIndex` used to move only on
      // j/k, so clicking or tabbing to a card left the cursor pointing at the
      // previous one: focus the second call, press `s`, and the FIRST call was
      // saved. React's onFocus is focusin and therefore bubbles, so focusing
      // this card's title link counts too -- which is what a keyboard user
      // tabbing through the feed actually does.
      onFocus={onFocus}
    >
      <h2 className={styles.cardTitle}>
        <Link to={detailTo}>{call.title}</Link>
      </h2>

      {/* UNDER THE TITLE, and OUTSIDE the `expanded` block.
          NOT the same choice the Save/Hide row makes -- that row renders only
          when view === "inbox", and the control these two views actually have,
          "Return to inbox", is INSIDE `expanded` and pinned there by
          TriageCard.test.tsx's "does not offer it on a collapsed card either".
          So this inverts the precedent for Saved and Hidden, deliberately: a
          note is a scanning aid, not a decision, and the reason is what the
          reader is scanning Hidden FOR. An affordance only on the focused card
          would mean reaching any other call's reason requires moving the cursor
          there first. Decided on that, not on the Save/Hide row. */}
      {view !== "inbox" && profileId !== null && (
        <DecisionNote call={call} profileId={profileId} state={view} />
      )}

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
      {/* SAVE / HIDE (Astra, High). The inbox had no controls at all: the only
          way to triage was `s` and `h`, which are undiscoverable, and which act
          on whatever the cursor believes is current.

          OUTSIDE the `expanded` block on purpose. Inside it, only the focused
          card would carry buttons -- so reaching any other call still meant
          moving the cursor first, which is the interaction that was broken. On
          every card, a click carries its own call and cannot decide a different
          one. Inbox only: a saved or hidden card's move is "Return to inbox"
          above, and Save on an already-saved card would be a no-op control. */}
      {view === "inbox" && (onSave || onHide) && (
        <p className={styles.cardActions}>
          {onSave && (
            <button
              type="button"
              className={styles.decide}
              disabled={busy}
              onClick={() => onSave(call)}
            >
              Save
            </button>
          )}
          {onHide && (
            <button
              type="button"
              className={styles.decideSecondary}
              disabled={busy}
              onClick={() => onHide(call)}
            >
              Hide
            </button>
          )}
        </p>
      )}
    </article>
  );
}
