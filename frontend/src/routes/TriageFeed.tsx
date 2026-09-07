// frontend/src/routes/TriageFeed.tsx
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useHideCall, useSetDecisionNote, useUnhideCall } from "../api/calls";
import { dashboardParamsFrom, useDashboard, type CallItem } from "../api/dashboard";
import {
  useDecisionCounts,
  useSaveCall,
  useUnsaveCall,
  type TriageView,
} from "../api/decisions";
import { DecisionNoteStrip, type NoteTarget } from "./DecisionNoteStrip";
import { TriageCard } from "./TriageCard";
import { TriageEmptyProfile } from "./TriageEmptyProfile";
import styles from "./TriageFeed.module.css";

// The match edition's index screen (spec §5). A single ~680 px expanding column
// with a 240 px rail for orientation only, driven by j/k/s/h/u, and an inbox
// that ends.
//
// THE EDITION SPLIT IS THE MOUNT POINT, not a branch. router.simple.tsx mounts
// this instead of Dashboard, so Dashboard/CallRow/MatchBadge/DashboardHeader/
// Pagination become reachable only from router.tsx and leave the match bundle
// entirely — which is why none of them needs an `isMatchEdition` check and why
// none may be deleted. check_match_bundle.mjs measures the result.

// The three triage views. `archive` — the server's default and the FULL
// edition's list — is deliberately NOT offered here: release 1's index is the
// per-profile inbox, and an "everything ever ingested" tab is the firehose the
// spec is trying to replace.
const RAIL: { view: TriageView; label: string }[] = [
  { view: "inbox", label: "Inbox" },
  { view: "saved", label: "Saved" },
  { view: "hidden", label: "Hidden" },
];

// THE GUARD. Spec §5's keys are single unmodified letters, so the listener must
// stand down whenever the keystroke belongs to a text field — otherwise typing
// "she" into a note field saves, hides and moves the selection.
//
// THREE targets, because a guard that covers one of them is one third of a
// guard: `<input>`, `<textarea>`, and contenteditable (every rich editor in this
// codebase is a contenteditable host, not an input).
//
// `closest()` and not a tagName equality check: the keydown target inside a
// contenteditable host is whatever inline element the caret sits in, so the
// check has to walk up. `[contenteditable]:not([contenteditable="false"])`
// rather than `[contenteditable]` because contenteditable="false" is the
// documented way to embed a read-only island INSIDE an editable region.
function isEditableTarget(target: EventTarget | null): boolean {
  const el = target instanceof HTMLElement ? target : null;
  if (el === null) return false;
  if (el.closest("input") !== null) return true;
  if (el.closest("textarea") !== null) return true;
  if (el.closest('[contenteditable]:not([contenteditable="false"])') !== null) return true;
  return false;
}

// Inbox zero is a DESIGNED SCREEN, not an empty div (spec §5: "Reaching zero is
// the reward"). Each view earns its own copy: "nothing left to triage" and "you
// have not saved anything yet" are different facts, and a shared string would be
// wrong for one of them.
function EmptyView({ view, profileName }: { view: TriageView; profileName: string | null }) {
  if (view === "saved") {
    return (
      <div className={styles.empty} data-empty="saved">
        <p className={styles.emptyHeadline}>Nothing saved yet.</p>
        <p className={styles.emptyBody}>
          Press <kbd>s</kbd> on a call in your inbox and it lands here.
        </p>
      </div>
    );
  }
  if (view === "hidden") {
    return (
      <div className={styles.empty} data-empty="hidden">
        <p className={styles.emptyHeadline}>Nothing hidden.</p>
        <p className={styles.emptyBody}>
          Calls you dismiss with <kbd>h</kbd> are kept here, not deleted.
        </p>
      </div>
    );
  }
  return (
    <div className={styles.empty} data-empty="inbox">
      <p className={styles.emptyHeadline}>Inbox zero.</p>
      <p className={styles.emptyBody}>
        You have triaged every call matched against {profileName ?? "your profile"}. New calls
        appear here as sources publish them.
      </p>
    </div>
  );
}

/* AN EMPTY PAGE IS NOT AN EMPTY INBOX (Astra, Medium). Deciding the last card on
   page two emptied `items`, and the component read that as inbox zero -- "you
   have triaged every call" while the rail beside it still said 50. The rail was
   right. The claim is about the whole list, so it has to be made from the whole
   list's count, never from the slice on screen.

   The way back matters as much as the wording: page two of a self-healing inbox
   is a dead end without it, because page one has refilled and nothing on this
   screen points at it. */
function EmptyPageView({ total, prevHref }: { total: number | null; prevHref: string }) {
  return (
    <div className={styles.empty} data-empty="page">
      <p className={styles.emptyHeadline}>Nothing left on this page.</p>
      <p className={styles.emptyBody}>
        {total === null
          ? "There are still calls waiting on earlier pages."
          : `${total} ${total === 1 ? "call is" : "calls are"} still waiting on earlier pages.`}
      </p>
      <p className={styles.emptyBody}>
        <Link to={prevHref}>← Previous page</Link>
      </p>
    </div>
  );
}

export function TriageFeed() {
  const [sp] = useSearchParams();
  const params = dashboardParamsFrom(sp);
  // A PROJECTION of the wire's four views onto the feed's three, NOT a second
  // sanitiser: dashboardParamsFrom has already dropped anything that is not one
  // of the four Literal values, so a mistyped `?view=inbo` arrives here as
  // undefined. The only extra case is `archive` — the full edition's list, which
  // release 1's index does not offer as a tab.
  const view: TriageView =
    params.view === undefined || params.view === "archive" ? "inbox" : params.view;
  const { data, isLoading, isError, isFetching } = useDashboard({ ...params, view });

  const profileId = data?.effective.profile_id ?? null;
  // The rail counts follow the SAME filters as the list, so the two agree under
  // a source filter or a search rather than only in the unfiltered case.
  const counts = useDecisionCounts(profileId, {
    source: params.source,
    q: params.q,
    show_closed: params.show_closed,
  });

  const save = useSaveCall();
  const unsave = useUnsaveCall();
  const hide = useHideCall();
  const unhide = useUnhideCall();
  const setNote = useSetDecisionNote();

  // WHICH decision the note strip is about, or null for no strip. Held here
  // rather than derived from the cursor, because the cursor moves and the
  // decision it was created for does not (D10) -- see DecisionNoteStrip's own
  // header. Set ONLY in a decision's onSuccess, below.
  //
  // `seed` rides in the SAME state object rather than beside it: the strip
  // opens over a note that may already exist, and a second useState cleared by
  // its own functional update in `undecide` is exactly how the two would come
  // to disagree about which call they describe.
  const [noteTarget, setNoteTarget] = useState<(NoteTarget & { seed: string | null }) | null>(null);

  const [focusIndex, setFocusIndex] = useState(0);
  const cardEls = useRef<(HTMLElement | null)[]>([]);
  // Whether the user has actually pressed j/k yet. Without this the feed steals
  // focus on every load, which breaks skip-links and re-focuses the page under a
  // background refetch.
  const hasMoved = useRef(false);

  const items: CallItem[] = data?.items ?? [];
  // Clamp rather than trust the stored index: a decided card leaves the list, so
  // the array can shrink under the cursor between renders.
  const focused = items.length === 0 ? -1 : Math.min(focusIndex, items.length - 1);

  // A new view OR a new page is a new list; the cursor belongs at the top of
  // it (D20). `params.page` too, not just `view`: the Next-page link (D15)
  // changes only `?page=`, so a view-only dependency here left a page-2 load
  // holding the page-1 index — e.g. focused on a card that does not exist at
  // this scroll position, or on the wrong one entirely.
  useEffect(() => {
    setFocusIndex(0);
    hasMoved.current = false;
  }, [view, params.page]);

  // The card actually holding focus, so the effect below re-runs when a
  // decision changes WHICH call sits at `focused` even though the numeric
  // index itself is unchanged (a decided card at a non-final index unmounts,
  // the ones after it shift down one slot, and the id at `focused` is now a
  // different call's — the index never moved so `[focused]` alone would not
  // re-fire and the newly-shifted card would sit expanded but un-focused).
  const focusedCallId = focused >= 0 ? items[focused]?.id : undefined;

  useEffect(() => {
    if (!hasMoved.current || focused < 0) return;
    cardEls.current[focused]?.focus();
    cardEls.current[focused]?.scrollIntoView?.({ block: "nearest" });
  }, [focused, focusedCallId]);

  const decide = useCallback(
    (kind: "save" | "hide", call: CallItem) => {
      // A decision belongs to a profile; with none resolved there is nowhere to
      // record it and the route would 422.
      if (profileId === null) return;
      // D20, restated HERE rather than trusted to the caller: the "Return to
      // inbox" button (TriageCard) calls undecide() directly, bypassing the key
      // listener's own isFetching check entirely, and keepPreviousData renders
      // that button on stale rows while the next view's fetch is in flight.
      if (isFetching) return;
      const args = { id: call.id, profileId };
      // Both hooks invalidate ["calls"] AND ["decision-counts"] themselves (see
      // invalidateDecisions). NOTHING CACHE-RELATED is added per call here on
      // purpose: a mutate-level onSuccess is dropped when this component
      // unmounts before the request lands, which on a keyboard triage loop is
      // the normal case.
      //
      // The note strip is the one thing that IS mutate-level, and it has to be:
      // it is per-call state on THIS component, so a hook-level callback in
      // api/calls.ts (shared with CallDetail, which has no strip) is the wrong
      // home for it, and the drop case is exactly the case where there is no
      // feed left to render a strip on.
      //
      // ON SUCCESS, NEVER OPTIMISTICALLY (D10). A failed hide that still opened
      // a strip would solicit a note for a decision that did not happen, and
      // the note endpoint would then 404 on a row that was never created.
      const nextState = kind === "save" ? "saved" : "hidden";
      // THE NOTE THAT IS ALREADY THERE, or null. `h` on an already-hidden card
      // in the Hidden view re-runs the upsert, whose CASE PRESERVES the note
      // through `hidden -> hidden` -- so an unseeded box would open empty over
      // a stored reason and the first Enter would replace it unseen.
      //
      // A FLIP is the opposite case and must NOT be seeded: `s` on a hidden
      // card writes `hidden -> saved`, which by D9 CLEARS the note server-side,
      // so seeding there would show text the server has just discarded and
      // re-save it on the next Enter. `view` is the row's current state in the
      // two views that have one, so `view === nextState` is exactly "same
      // state, note preserved"; the Inbox matches neither and carries no
      // decision row to have a note on.
      const seed = view === nextState ? (call.decision_note ?? null) : null;
      const onDecided = () =>
        setNoteTarget({
          callId: call.id,
          profileId,
          title: call.title,
          state: nextState,
          seed,
        });
      // onSuccess, never optimistically (D10): a failed hide must not solicit a
      // note for a decision that did not happen, and the note endpoint would
      // then 404 on a row that never existed.
      //
      // In practice this fires LATER than "the POST resolved". Both hooks
      // return invalidateDecisions(qc)'s promise from their hook-level
      // onSuccess, and TanStack v5 awaits that before running mutate-level
      // callbacks -- so the strip appears once the list and the counts have
      // settled, together with the list update rather than ahead of it.
      // Measured 2026-09-07: holding the list refetch holds the strip. That
      // reads better, but it means a hung refetch delays the prompt, which is
      // the window D2 exists for. Recorded so the coupling is a choice.
      if (kind === "save") save.mutate(args, { onSuccess: onDecided });
      else hide.mutate(args, { onSuccess: onDecided });
    },
    [hide, isFetching, profileId, save, view],
  );

  // UN-DECIDE (D2): back to undecided, which is the ABSENCE of a decision row —
  // so it is a DELETE of the row this view is made of, never a write. Saving a
  // hidden call is a different act (a flip), and `s` in Hidden still does that.
  const undecide = useCallback(
    (call: CallItem) => {
      if (profileId === null) return;
      // See decide()'s comment: this is the guard the button actually needs.
      if (isFetching) return;
      const args = { id: call.id, profileId };
      // A note belongs to its decision and dies with it (D4), so un-deciding
      // the call the strip is about leaves a box whose write can only 404.
      // Cleared on SUCCESS, symmetrically with decide(): a failed un-decide has
      // changed nothing.
      const onUndecided = () =>
        setNoteTarget((t) => (t !== null && t.callId === call.id ? null : t));
      if (view === "saved") unsave.mutate(args, { onSuccess: onUndecided });
      else if (view === "hidden") unhide.mutate(args, { onSuccess: onUndecided });
      // The Inbox has no decision row to delete, so `u` there does nothing.
    },
    [isFetching, profileId, unhide, unsave, view],
  );

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      // Chords belong to the browser and the OS: ⌘S is Save Page, not save call.
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (isEditableTarget(e.target)) return;
      if (items.length === 0) return;
      const current = items[focused];
      if (e.key === "j") {
        hasMoved.current = true;
        setFocusIndex((i) => Math.min(i + 1, items.length - 1));
      } else if (e.key === "k") {
        hasMoved.current = true;
        setFocusIndex((i) => Math.max(i - 1, 0));
      } else if (e.key === "s" || e.key === "h" || e.key === "u") {
        // D20. `keepPreviousData` keeps the rows of the list we are LEAVING on
        // screen while the next one is in flight, so a keystroke in that window
        // would decide a card the server has already moved out of the view.
        // Moving the cursor is harmless and stays live.
        if (isFetching) return;
        if (current) {
          if (e.key === "s") decide("save", current);
          else if (e.key === "h") decide("hide", current);
          else undecide(current);
        }
      } else {
        return;
      }
      e.preventDefault();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [decide, focused, isFetching, items, undecide]);

  if (isLoading) return <p className={styles.status}>Loading…</p>;
  if (isError || !data) {
    return (
      <p role="alert" className={styles.status}>
        Couldn&apos;t load calls.
      </p>
    );
  }

  // NO PROFILE IS A DIFFERENT SCREEN, and it REPLACES the feed rather than
  // sitting above it. The server still answers with the unranked fallback here
  // (dashboard.py is untouched by design, so the FULL edition keeps its
  // browsable archive), so returning the layout with an empty column would
  // render every open call in the database ranked against nothing — the
  // firehose release 1 exists to remove.
  //
  // The rail goes with it: its counts are not even fetched without a profile
  // (useDecisionCounts is `enabled: profileId != null`, and profile_id is
  // REQUIRED on that route), and Saved/Hidden hold nothing to show until a
  // profile is picked. Reaching them again is one link away, on the screen
  // below.
  if (profileId === null) {
    return <TriageEmptyProfile profiles={data.facets.profiles} />;
  }

  const profileName = data.facets.profiles.find((p) => p.id === profileId)?.name ?? null;

  const railHref = (v: TriageView) => {
    const next = new URLSearchParams(sp);
    next.set("view", v);
    // A view change is a new list; page 1 of it, not page 4 of the old one.
    next.delete("page");
    return `/?${next.toString()}`;
  };
  // THE PROFILE PICKER (D11). The match index has no other profile switcher —
  // it left with DashboardHeader — so without this the product's own smoke
  // ("switching profile changes which calls are hidden") is unperformable. The
  // link carries the current view, and nothing else: a new profile is a new set
  // of rankings, so the old page number and search would be answering a
  // question about somebody else's list.
  const profileHref = (id: number) => `/?profile=${id}&view=${view}`;
  // D15. The inbox self-heals — decided cards leave and page 1 refills — but
  // Saved and Hidden do not, so past the server's `limit` the rest of the list
  // is unreachable without this.
  const nextPageHref = () => {
    const next = new URLSearchParams(sp);
    next.set("view", view);
    if (profileId != null) next.set("profile", String(profileId));
    next.set("page", String((params.page ?? 1) + 1));
    return `/?${next.toString()}`;
  };
  const prevPageHref = () => {
    const next = new URLSearchParams(sp);
    next.set("view", view);
    if (profileId != null) next.set("profile", String(profileId));
    const prev = (params.page ?? 1) - 1;
    // Page 1 is the ABSENCE of the parameter everywhere else in this file, so
    // writing `page=1` would produce a second URL for the same list.
    if (prev <= 1) next.delete("page");
    else next.set("page", String(prev));
    return `/?${next.toString()}`;
  };

  return (
    <main className={styles.layout}>
      <nav className={styles.rail} aria-label="Triage views">
        <ul className={styles.railList}>
          {RAIL.map((r) => (
            <li key={r.view}>
              <Link
                to={railHref(r.view)}
                className={styles.railLink}
                aria-current={r.view === view ? "page" : undefined}
              >
                <span>{r.label}</span>
                {/* FROM THE COUNTS RESPONSE, never from items.length: the list
                    is paginated and view-scoped, so its length answers a
                    different question than "how many calls are in Saved". */}
                <span className={styles.railCount} data-count={r.view}>
                  {counts.data ? counts.data[r.view] : "—"}
                </span>
              </Link>
            </li>
          ))}
        </ul>

        <p className={styles.railProfile}>
          {profileName ? (
            <>
              Matching against <strong>{profileName}</strong>
            </>
          ) : (
            "No profile selected"
          )}
        </p>

        {data.facets.profiles.length > 0 && (
          <ul className={styles.picker} aria-label="Profiles">
            {data.facets.profiles.map((p) => (
              <li key={p.id}>
                <Link
                  to={profileHref(p.id)}
                  className={styles.pickerLink}
                  data-profile-pick={p.id}
                  aria-current={p.id === profileId ? "true" : undefined}
                >
                  {p.name}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </nav>

      <div className={styles.column}>
        {data.ranking.pending > 0 && (
          <p className={styles.status}>Scoring {data.ranking.pending} calls…</p>
        )}

        {/* OUTSIDE the items.length branch, so the strip survives the list
            emptying under it -- deciding the last card in a view is precisely
            when a reason is worth asking for.

            KEYED by the decision, so the next one REPLACES the strip rather
            than re-labelling it: a fresh instance starts with an empty box and
            its own bound target, and the outgoing instance's in-flight write
            keeps the ids it was created with and is not cancelled (D10).

            expectedState comes from THIS closure's noteTarget, not from the
            strip: the strip must not carry two ways to say which decision it
            is about (D11).

            NO autoFocus, and that is D2 itself: this strip appears on its own
            after a keypress the user made, and grabbing the caret would turn
            `h h h` into typed text in the first box. */}
        {noteTarget !== null && (
          <DecisionNoteStrip
            key={`${noteTarget.callId}:${noteTarget.state}`}
            target={noteTarget}
            initialNote={noteTarget.seed}
            onSave={(args) => setNote.mutateAsync({ ...args, expectedState: noteTarget.state })}
            onDismiss={() => setNoteTarget(null)}
          />
        )}

        {items.length === 0 ? (
          // Past page one, an empty slice means this PAGE ran out -- not the
          // list. While the counts are still loading we do not know, and the
          // honest answer to "do not know" is the one that does not claim the
          // work is finished.
          (params.page ?? 1) > 1 && (counts.data?.[view] ?? 1) > 0 ? (
            <EmptyPageView total={counts.data ? counts.data[view] : null} prevHref={prevPageHref()} />
          ) : (
            <EmptyView view={view} profileName={profileName} />
          )
        ) : (
          <>
            <ol className={styles.feed}>
              {items.map((call, i) => (
                <li key={call.id}>
                  <TriageCard
                    call={call}
                    expanded={i === focused}
                    profileId={profileId}
                    view={view}
                    onUndecide={undecide}
                    onSave={(c) => decide("save", c)}
                    onHide={(c) => decide("hide", c)}
                    busy={isFetching}
                    // The cursor follows REAL focus, so a click or a Tab moves
                    // it the same way j/k do. Without this the two disagreed and
                    // `s` decided whichever card j/k last visited -- Astra
                    // focused the second call and watched the first get saved.
                    // Setting the same index is a no-op re-render, so the effect
                    // that calls .focus() on the focused card cannot loop.
                    onFocus={() => setFocusIndex(i)}
                    cardRef={(el) => {
                      cardEls.current[i] = el;
                    }}
                  />
                </li>
              ))}
            </ol>
            {((params.page ?? 1) > 1 || data.pagination.has_next) && (
              <p className={styles.nextPage}>
                {(params.page ?? 1) > 1 && <Link to={prevPageHref()}>← Previous page</Link>}
                {(params.page ?? 1) > 1 && data.pagination.has_next && " · "}
                {data.pagination.has_next && <Link to={nextPageHref()}>Next page →</Link>}
              </p>
            )}
          </>
        )}
      </div>
    </main>
  );
}
