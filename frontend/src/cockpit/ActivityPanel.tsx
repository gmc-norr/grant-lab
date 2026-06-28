import { Button, Group, Stack, Text } from "@mantine/core";

import { type ActivityRow, useActivity } from "../api/activity";
import styles from "./CockpitPanels.module.css";
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
        <Text role="status" c="red" size="sm">
          Could not load activity.
        </Text>
      </div>
    );
  }
  if (q.isLoading) {
    return (
      <div className="activity-panel">
        <Text c="dimmed" size="sm">
          Loading…
        </Text>
      </div>
    );
  }
  const rows: ActivityRow[] = q.data?.items ?? [];
  if (rows.length === 0) {
    return (
      <div className="activity-panel">
        <Text c="dimmed" size="sm">
          No activity yet.
        </Text>
      </div>
    );
  }
  return (
    <div className="activity-panel">
      <Stack component="ul" gap={4} style={{ listStyle: "none", padding: 0, margin: 0 }}>
        {rows.map((r) => (
          <Group
            key={r.id}
            component="li"
            gap={6}
            align="baseline"
            wrap="wrap"
            data-category={r.category}
            className={styles.activityRow}
          >
            <Text span fw={600} size="sm">
              {r.actor ?? "System"}
            </Text>
            <Text span size="sm">
              {r.summary}
            </Text>
            <Text span c="dimmed" size="sm">
              {" "}
              · {formatRelativeTime(r.created_at)}
            </Text>
            {r.target && (
              <Button
                size="xs"
                variant="subtle"
                ml="auto"
                onClick={() => onNavigate(toNav(r.target))}
              >
                Go to
              </Button>
            )}
          </Group>
        ))}
      </Stack>
    </div>
  );
}
