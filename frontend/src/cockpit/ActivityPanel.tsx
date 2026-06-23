import { type ActivityRow, useActivity } from "../api/activity";
import type { NavTarget, OnNavigate } from "./nav";

// null -> undefined normalization. The param stays nullable and uses `?.` because
// TS drops the `r.target &&` narrowing inside the onClick closure at the call site
// below. The generated target Literals already match TopTab/BottomTab, so the
// previous `as TopTab | undefined` casts were redundant and are gone.
function toNav(target: ActivityRow["target"]): NavTarget {
  return {
    top_tab: target?.top_tab ?? undefined,
    bottom_tab: target?.bottom_tab ?? undefined,
  };
}

function formatRelativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const secs = Math.round((Date.now() - then) / 1000);
  if (secs < 45) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

export function ActivityPanel({ id, onNavigate }: { id: string; onNavigate: OnNavigate }) {
  const q = useActivity(id);

  if (q.isError) {
    return (
      <div className="activity-panel">
        <p role="status" className="activity-error">
          Could not load activity.
        </p>
      </div>
    );
  }
  if (q.isLoading) {
    return (
      <div className="activity-panel">
        <p className="activity-loading">Loading…</p>
      </div>
    );
  }
  const rows: ActivityRow[] = q.data?.items ?? [];
  if (rows.length === 0) {
    return (
      <div className="activity-panel">
        <p className="activity-empty">No activity yet.</p>
      </div>
    );
  }
  return (
    <div className="activity-panel">
      <ul className="activity-list">
        {rows.map((r) => (
          <li key={r.id} className={`activity-row category-${r.category}`}>
            <span className="activity-actor">{r.actor ?? "System"}</span>
            <span className="activity-summary">{r.summary}</span>
            <span className="activity-time"> · {formatRelativeTime(r.created_at)}</span>
            {r.target && (
              <button
                type="button"
                className="activity-act"
                onClick={() => onNavigate(toNav(r.target))}
              >
                Go to
              </button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
