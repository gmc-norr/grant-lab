// frontend/src/routes/TriageFeed.tsx
import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useHideCall, useUnhideCall } from "../api/calls";
import { dashboardParamsFrom, useDashboard, type CallItem } from "../api/dashboard";
import {
  invalidateDecisions,
  useDecisionCounts,
  useSaveCall,
  useUnsaveCall,
  type TriageView,
} from "../api/decisions";
import { TriageCard } from "./TriageCard";
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

export function TriageFeed() {
  const [sp] = useSearchParams();
  const qc = useQueryClient();
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

  // A new view is a new list; the cursor belongs at the top of it (D20).
  useEffect(() => {
    setFocusIndex(0);
    hasMoved.current = false;
  }, [view]);

  useEffect(() => {
    if (!hasMoved.current || focused < 0) return;
    cardEls.current[focused]?.focus();
    cardEls.current[focused]?.scrollIntoView?.({ block: "nearest" });
  }, [focused]);

  const decide = useCallback(
    (kind: "save" | "hide", call: CallItem) => {
      // A decision belongs to a profile; with none resolved there is nowhere to
      // record it and the route would 422.
      if (profileId === null) return;
      const args = { id: call.id, profileId };
      if (kind === "save") save.mutate(args);
      // useHideCall predates the rail and invalidates only ["calls"], so the
      // counts are refreshed here explicitly. See invalidateDecisions.
      else hide.mutate(args, { onSuccess: () => invalidateDecisions(qc) });
    },
    [hide, profileId, qc, save],
  );

  // UN-DECIDE (D2): back to undecided, which is the ABSENCE of a decision row —
  // so it is a DELETE of the row this view is made of, never a write. Saving a
  // hidden call is a different act (a flip), and `s` in Hidden still does that.
  const undecide = useCallback(
    (call: CallItem) => {
      if (profileId === null) return;
      const args = { id: call.id, profileId };
      if (view === "saved") unsave.mutate(args);
      else if (view === "hidden")
        // useUnhideCall, like useHideCall, invalidates only ["calls"].
        unhide.mutate(args, { onSuccess: () => invalidateDecisions(qc) });
      // The Inbox has no decision row to delete, so `u` there does nothing.
    },
    [profileId, qc, unhide, unsave, view],
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

        {items.length === 0 ? (
          <EmptyView view={view} profileName={profileName} />
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
                    cardRef={(el) => {
                      cardEls.current[i] = el;
                    }}
                  />
                </li>
              ))}
            </ol>
            {data.pagination.has_next && (
              <p className={styles.nextPage}>
                <Link to={nextPageHref()}>Next page →</Link>
              </p>
            )}
          </>
        )}
      </div>
    </main>
  );
}
