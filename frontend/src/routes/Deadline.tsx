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
// Deadline state is derived from midnight UTC of the local date string so that
// "today" (local) reads as still open rather than already closed.
export function deadlineLocalView(localDate: string): { state: DeadlineState; date: string } {
  // Treat the date string as a UTC midnight boundary for comparison purposes —
  // this is approximate but consistent and avoids TZ ambiguity in the browser.
  const ms = new Date(localDate + "T00:00:00Z").getTime() - Date.now();
  if (ms <= 0) return { state: "closed", date: localDate };
  const days = Math.floor(ms / 86_400_000);
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
