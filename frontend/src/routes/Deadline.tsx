import { Text } from "@mantine/core";

type DeadlineState = "closed" | "soon" | "watch" | "open";

// Parity with the retired Jinja `deadline_label`: a past deadline is "Closed"
// (dim), <=14 days urgent (bad), <=30 days a watch (warn), else plain (muted).
// The instant is shown as its UTC calendar date.
export function deadlineView(iso: string): { state: DeadlineState; date: string } {
  const date = iso.slice(0, 10);
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return { state: "closed", date };
  const days = Math.floor(ms / 86_400_000);
  if (days <= 14) return { state: "soon", date };
  if (days <= 30) return { state: "watch", date };
  return { state: "open", date };
}

// Classify a localised YYYY-MM-DD date string (Europe/Stockholm from the API).
// Open/closed is decided by comparing the local deadline date string against
// today's date string in Europe/Stockholm — so "today in Stockholm" is still
// open regardless of how far past UTC midnight we are (Stockholm is UTC+1/+2,
// so a deadline dated today is open until midnight Stockholm time, not UTC).
// `days` is ADDITIVE — the two existing callers destructure {state, date} and are
// untouched. It is RETURNED rather than recomputed in TriageCard because the
// Stockholm-calendar-day rule below is subtle enough to have needed its own
// regression test, and a second copy of it would drift.
//
// `instant` (optional) is the exact deadline the backend closes the call at --
// EU calls at 17:00 Brussels. When given, it alone decides CLOSED once it has
// passed, so a call that closed earlier today reads "Closed" the same afternoon
// instead of "today" until midnight (beta review 2026-10-31 F5). The calendar
// date still decides the label, the bands and `days`; without an instant the
// date-only rule above is unchanged (the public board has only the date).
export function deadlineLocalView(
  localDate: string,
  instant?: string | null,
): {
  state: DeadlineState;
  date: string;
  days: number;
} {
  // Today's date in Europe/Stockholm as a YYYY-MM-DD string (sv-SE gives that format).
  const todayStockholm = new Date().toLocaleDateString("sv-SE", { timeZone: "Europe/Stockholm" });
  // Days-remaining: deadline Stockholm midnight UTC minus now.  We use the Intl
  // formatter to find Stockholm's UTC-offset on that date (handles DST), then
  // back-compute the UTC instant for midnight Stockholm time.
  //
  // Simpler approximation that is accurate enough for <=14/<=30 day bands:
  // parse the deadline as the UTC-midnight of that date string, then subtract
  // today's Stockholm-midnight to get the number of Stockholm-calendar-days left.
  const deadlineUtcMidnight = new Date(localDate + "T00:00:00Z").getTime();
  const todayUtcMidnight = new Date(todayStockholm + "T00:00:00Z").getTime();
  const days = Math.floor((deadlineUtcMidnight - todayUtcMidnight) / 86_400_000);
  // String comparison works correctly for ISO date strings (lexicographic =
  // chronological). It, not `days`, still decides closed — moving that decision
  // onto the arithmetic is what the DST regression test above forbids.
  if (instant && new Date(instant).getTime() <= Date.now()) {
    return { state: "closed", date: localDate, days };
  }
  if (localDate < todayStockholm) return { state: "closed", date: localDate, days };
  if (days <= 14) return { state: "soon", date: localDate, days };
  if (days <= 30) return { state: "watch", date: localDate, days };
  return { state: "open", date: localDate, days };
}

// Domain deadline-urgency colours (parity with the retired Deadline CSS module):
// closed=dim, soon=bad+bold, watch=warn, open=muted.
const DEADLINE_TEXT: Record<DeadlineState, { c: string; fw?: number }> = {
  closed: { c: "var(--app-dim)" },
  soon: { c: "var(--app-bad)", fw: 600 },
  watch: { c: "var(--app-warn)" },
  open: { c: "var(--app-muted)" },
};

export function Deadline({ iso }: { iso: string }) {
  const { state, date } = deadlineView(iso);
  return (
    <Text span data-deadline-state={state} {...DEADLINE_TEXT[state]}>
      {state === "closed" ? `Closed · ${date}` : date}
    </Text>
  );
}

// The clock time a deadline instant falls at in Stockholm, "HH:MM" -- what a
// researcher needs on the last day ("today, closes 17:00").
export function closingTimeStockholm(instant: string): string {
  return new Date(instant).toLocaleTimeString("sv-SE", {
    timeZone: "Europe/Stockholm",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function DeadlineLocal({
  localDate,
  isoFallback,
}: {
  localDate: string | null;
  isoFallback: string;
}) {
  if (localDate) {
    const { state, date } = deadlineLocalView(localDate, isoFallback);
    return (
      <Text span data-deadline-state={state} {...DEADLINE_TEXT[state]}>
        {state === "closed" ? `Closed · ${date}` : date}
      </Text>
    );
  }
  return <Deadline iso={isoFallback} />;
}
