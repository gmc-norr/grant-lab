import { Text } from "@mantine/core";

import type { PublicBoardItemOut } from "../api/publicBoard";
import { deadlineLocalView } from "./Deadline";
import styles from "./BoardScreen.module.css";

// Same shape as TriageCard.tsx's private helper (the two item types share
// budget_min/budget_max/currency but are otherwise distinct payloads, so this
// is not extracted into a shared util) — absent means absent, no "—" row.
function budgetText(item: PublicBoardItemOut): string | null {
  const { budget_min: lo, budget_max: hi, currency } = item;
  if (lo == null && hi == null) return null;
  const cur = currency ? ` ${currency}` : "";
  // sv-SE, not en-US: this is the app's only public-facing surface and it
// already says "Sida N av M" in the footer. The rest of the app uses
// en-US for authenticated views; the wall is the one place a passer-by
// who never logs in reads a number.
const fmt = (n: number) => n.toLocaleString("sv-SE");
  if (lo != null && hi != null) {
    return lo === hi ? `${fmt(lo)}${cur}` : `${fmt(lo)} – ${fmt(hi)}${cur}`;
  }
  return lo != null ? `From ${fmt(lo)}${cur}` : `Up to ${fmt(hi as number)}${cur}`;
}

// MINOR 2 fix: deadlineLocalView's `days` field is a plain day count;
// Deadline.tsx:23-26's comment calls it "ADDITIVE" (returned alongside the
// existing {state, date} shape without breaking the two prior callers) — it
// is NOT itself worded "Closes in N days"; that wording lives here.
// BoardScreen has already dropped any card whose deadline has passed
// (state === "closed"), so this only ever renders days >= 0. Exported:
// BoardScreen's header uses the same wording for the nearest deadline across
// all pages, and a second copy of the day-count logic would drift from this.
export function closesInText(localDate: string | null): string | null {
  if (!localDate) return null;
  const { days } = deadlineLocalView(localDate);
  if (days < 0) return null;
  // MINOR 1 fix: a deadline dated today is still open (days === 0) and is
  // the most urgent case on the board — "Closes in 0 days" read as a bug on
  // the exact day it matters most. Matches TriageCard.tsx's own days===0
  // special-case ("today", `daysLeftText`).
  if (days === 0) return "Closes today";
  return days === 1 ? "Closes in 1 day" : `Closes in ${days} days`;
}

// One slot in the grid. `item === null` is an empty slot (D5: no reflow, no
// filler text) — BoardScreen pads the page out to `items_per_page` with these
// rather than shrinking the grid to however many calls happen to be approved.
export function BoardCard({ item }: { item: PublicBoardItemOut | null }) {
  if (!item) {
    return <div className={styles.cardEmpty} data-field="empty-slot" aria-hidden="true" />;
  }

  const budget = budgetText(item);
  const closes = closesInText(item.deadline_local_date);
  const m = item.qr_modules;

  return (
    <article className={styles.card} data-field="board-card">
      {/* The title owns the card's FULL width now. Beside the QR it had ~165px
          at 34px type, so a realistic funding title wrapped into a tower and
          pushed the whole second row of the wall off a 1920x1080 screen. */}
      <Text component="h2" className={styles.cardTitle} data-field="title">
        {item.title}
      </Text>
      <div className={styles.cardFoot}>
        <div className={styles.cardFacts}>
          <Text className={styles.cardMeta} data-field="funder">
            {item.source_name}
          </Text>
          {closes && (
            <Text className={styles.cardDeadline} data-field="deadline">
              {closes}
            </Text>
          )}
          {budget && (
            <Text className={styles.cardBudget} data-field="budget">
              {budget}
            </Text>
          )}
        </div>
      {/* qr_modules === 0 means segno's own overflow ceiling rejected this
          URL (schema.gen.ts's doc comment on the field) -- render NO <svg> at
          all rather than an empty/broken one. `d` is an attribute React
          escapes like any other string prop; this is never
          dangerouslySetInnerHTML, so the payload cannot inject markup even
          though it travels as raw SVG path data. */}
      {m > 0 && (
        <svg
          className={styles.cardQr}
          viewBox={`-4 -4 ${m + 8} ${m + 8}`}
          role="img"
          aria-label={`QR code linking to ${item.title}`}
        >
          <path d={item.qr_path} fill="#000" />
        </svg>
      )}
      </div>
    </article>
  );
}
