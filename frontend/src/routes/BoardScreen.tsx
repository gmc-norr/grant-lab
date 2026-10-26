import { useEffect, useState } from "react";
import { Progress, Text } from "@mantine/core";
import { useParams } from "react-router-dom";

import { usePublicBoard, type PublicBoardItemOut, type PublicBoardOut } from "../api/publicBoard";
import { BoardCard, closesInText } from "./BoardCard";
import { deadlineLocalView } from "./Deadline";
import styles from "./BoardScreen.module.css";
// The wall's own bundled font faces, imported HERE rather than in main.tsx or
// theme.css: the kiosk is the only surface whose layout is measured against a
// specific font (BoardScreen.fonts.css explains why), and the rest of the app
// is deliberately left on the system stack. Both renderers of the wall reach
// these faces through this module — the /b/:slug route via BoardScreen, and the
// admin draft preview (BoardDetail.tsx, `?preview=1`) via BoardPresentation,
// which is exported below.
import "./BoardScreen.fonts.css";

// Re-render often enough that a card whose deadline passes gets dropped
// without waiting for the next network poll — a single-page board has no
// other reason to re-render between the ~5-minute polls in usePublicBoard,
// and "deadlines expire locally too" (D6) must hold even then.
const DEADLINE_RECHECK_MS = 60_000;

function isOpen(item: PublicBoardItemOut): boolean {
  if (!item.deadline_local_date) return true;
  return deadlineLocalView(item.deadline_local_date).state !== "closed";
}

// Lexicographic comparison on YYYY-MM-DD strings is chronological (the same
// fact Deadline.tsx's own comment relies on) — no Date parsing needed to find
// the earliest one.
function nearestDeadlineLocal(items: PublicBoardItemOut[]): string | null {
  let nearest: string | null = null;
  for (const item of items) {
    if (item.deadline_local_date && (nearest === null || item.deadline_local_date < nearest)) {
      nearest = item.deadline_local_date;
    }
  }
  return nearest;
}

export function BoardScreen() {
  const { slug = "" } = useParams();
  const board = usePublicBoard(slug);
  return <BoardPresentation board={board} />;
}

export function BoardPresentation({ board }: { board: PublicBoardOut | null }) {
  const [page, setPage] = useState(0);
  // No dependency on `board` deliberately: this must force a render on its
  // own schedule so `isOpen` below is re-evaluated against the CURRENT date
  // even when board reference and page are both unchanged (Gate D — filtering
  // only on a fresh 200 would miss exactly this case).
  const [, recheckDeadlines] = useState(0);

  const itemsPerPage = board?.items_per_page ?? 8;
  const pageSeconds = board?.page_seconds ?? 30;

  // Deliberately NOT memoized on `board` alone — recomputed on every render,
  // including the ones DEADLINE_RECHECK_MS below triggers with no new fetch,
  // so a stale copy still loses a card the moment its deadline passes.
  const openItems = board ? board.items.filter(isOpen) : [];
  const totalPages = Math.max(1, Math.ceil(openItems.length / itemsPerPage));

  useEffect(() => {
    const id = setInterval(() => recheckDeadlines((t) => t + 1), DEADLINE_RECHECK_MS);
    return () => clearInterval(id);
  }, []);

  // Clamp whenever a fresh fetch (or a deadline dropping a card) shrinks the
  // page count, so a leftover page index never points past the new end.
  useEffect(() => {
    setPage((p) => (p >= totalPages ? 0 : p));
  }, [totalPages]);

  // The rotation timer. Restarts if page_seconds itself changes (an admin
  // edited it) or the page count changes; a single page needs no timer at all.
  useEffect(() => {
    if (totalPages <= 1) return;
    const id = setInterval(() => {
      setPage((p) => (p + 1) % totalPages);
    }, pageSeconds * 1000);
    return () => clearInterval(id);
  }, [totalPages, pageSeconds]);

  // D6's fourth outcome (blanked by 404/410, or a last-good copy past its
  // 60-minute bound) and the unpublish path both land here: nothing renders.
  // usePublicBoard already collapses both cases to `null` — this component
  // does not need to (and must not) tell them apart.
  if (!board) {
    return <div className={styles.screen} data-board-state="blank" />;
  }

  // Header: the nearest deadline ACROSS ALL PAGES, not the current one — §6's
  // "hook that makes someone stop" would silently miss whatever page isn't on
  // screen right now if this were computed from `pageItems` instead.
  const nearest = nearestDeadlineLocal(openItems);
  const nearestText = nearest ? closesInText(nearest) : null;

  const pageItems = openItems.slice(page * itemsPerPage, (page + 1) * itemsPerPage);
  // D5: no reflow. Always exactly `items_per_page` slots; short pages pad
  // with empty ones rather than shrinking the grid to queue depth.
  const slots: (PublicBoardItemOut | null)[] = Array.from(
    { length: itemsPerPage },
    (_, i) => pageItems[i] ?? null,
  );

  return (
    <div className={styles.screen} data-board-state="live" data-orientation={board.orientation}>
      <header className={styles.header}>
        <Text component="h1" className={styles.title}>
          {board.title}
        </Text>
        {nearestText && (
          <Text className={styles.nearestDeadline} data-field="nearest-deadline">
            {nearestText}
          </Text>
        )}
      </header>

      {openItems.length === 0 ? (
        // D5: zero approved (or all past deadline) is the title and one quiet
        // line — worded for what it is, not "no open calls right now", which
        // would be a false claim: the threshold set and the approved set are
        // deliberately different.
        <div className={styles.empty}>
          <Text className={styles.emptyText} data-field="empty-board">
            No calls are currently posted
          </Text>
        </div>
      ) : (
        <>
          <div className={styles.grid} data-orientation={board.orientation}>
            {slots.map((item, i) => (
              <BoardCard key={i} item={item} />
            ))}
          </div>
          <footer className={styles.footer}>
            <Progress value={((page + 1) / totalPages) * 100} size="sm" aria-hidden="true" />
            <Text className={styles.footerLabel} data-field="page-indicator">
              {`Sida ${page + 1} av ${totalPages}`}
            </Text>
          </footer>
        </>
      )}
    </div>
  );
}
