import styles from "./Deadline.module.css";

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
export function deadlineLocalView(localDate: string): { state: DeadlineState; date: string } {
  // Today's date in Europe/Stockholm as a YYYY-MM-DD string (sv-SE gives that format).
  const todayStockholm = new Date().toLocaleDateString("sv-SE", { timeZone: "Europe/Stockholm" });
  // String comparison works correctly for ISO date strings (lexicographic = chronological).
  if (localDate < todayStockholm) return { state: "closed", date: localDate };
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
  if (days <= 14) return { state: "soon", date: localDate };
  if (days <= 30) return { state: "watch", date: localDate };
  return { state: "open", date: localDate };
}

export function Deadline({ iso }: { iso: string }) {
  const { state, date } = deadlineView(iso);
  return (
    <span className={styles[state]} data-deadline-state={state}>
      {state === "closed" ? `Closed · ${date}` : date}
    </span>
  );
}

// Renders the Europe/Stockholm local deadline date from the API field
// `deadline_local_date` (YYYY-MM-DD). Falls back to the raw UTC iso string if
// the localised field is null.
export function DeadlineLocal({
  localDate,
  isoFallback,
}: {
  localDate: string | null;
  isoFallback: string;
}) {
  if (localDate) {
    const { state, date } = deadlineLocalView(localDate);
    return (
      <span className={styles[state]} data-deadline-state={state}>
        {state === "closed" ? `Closed · ${date}` : date}
      </span>
    );
  }
  // Fallback to UTC slice when server omits the localised field.
  return <Deadline iso={isoFallback} />;
}
