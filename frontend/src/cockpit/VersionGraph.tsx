import { HoverCard, Text } from "@mantine/core";

import { useVersions } from "../api/versions";
import { useHistoryNav } from "./historyNav";
import { SidebarGroup } from "./SidebarGroup";
import styles from "./VersionGraph.module.css";

/** The cockpit side pane's Versions group, drawn as a commit graph.
 *
 *  Owns its own SidebarGroup (title + count) so the version data has exactly
 *  one reader — the group header's count and the rows cannot disagree.
 *
 *  Git-sourced, via api/versions. The DB-sourced list this replaces could omit
 *  versions in principle: four routine commit paths write no artifact_versions
 *  row. On live data the two agreed exactly, so this is a layout and
 *  duplication change, not a correctness fix. */
export function VersionGraph({ id }: { id: string }) {
  const nav = useHistoryNav();
  const list = useVersions(id, nav.artifact);
  const data = list.data;
  const rows = data?.versions ?? [];

  return (
    <SidebarGroup title="Versions" count={rows.length || undefined} defaultOpen={false}>
      {/* Each state gets its own words. "Could not load" and "not available"
          and "none yet" are three different situations, and a single empty
          message for all three tells the user nothing about which. */}
      {list.isError && <p className={styles.note}>Could not load version history.</p>}
      {!list.isError && data && !data.history_available && (
        <p className={styles.note}>Version history is not available yet.</p>
      )}
      {!list.isError && data?.history_available && rows.length === 0 && (
        <p className={styles.note}>No versions yet.</p>
      )}

      {rows.length > 0 && (
        <ul className={styles.list} aria-label="Versions">
          {rows.map((v) => {
            const selected = v.commit_sha === nav.a || v.commit_sha === nav.b;
            return (
              <li key={v.commit_sha} className={styles.row}>
                <HoverCard
                  position="right-start"
                  withinPortal
                  openDelay={250}
                  shadow="md"
                  width={340}
                >
                  <HoverCard.Target>
                    <button
                      type="button"
                      className={styles.button}
                      aria-pressed={selected}
                      onClick={() => nav.selectAndOpen(v.commit_sha)}
                    >
                      <span className={styles.lane} aria-hidden="true">
                        <span className={styles.dot} data-role={v.created_by ?? "none"} />
                      </span>
                      <span className={styles.sha}>{v.commit_sha.slice(0, 7)}</span>
                      <span className={styles.subject}>{v.subject}</span>
                    </button>
                  </HoverCard.Target>
                  {/* What will not fit at 276px lives here. `right-start` puts it
                      beside the pane; Mantine's floating middleware flips it when
                      there is no room to the right (a widened pane, a narrow
                      window), so no manual positioning is needed. */}
                  <HoverCard.Dropdown data-testid={`version-card-${v.commit_sha.slice(0, 7)}`}>
                    <Text size="sm" fw={600}>
                      {v.subject}
                    </Text>
                    <Text size="xs" c="dimmed" mt={2}>
                      {v.created_by ?? "not recorded"} · {v.commit_sha.slice(0, 7)} ·{" "}
                      {new Date(v.committed_at).toLocaleString()}
                    </Text>
                    {/* The "why". Null for every reconciled row and every commit
                        with no DB row, so absence is ordinary — render nothing,
                        not a placeholder. */}
                    {v.turn_prompt && (
                      <Text size="xs" mt={6}>
                        because you asked: {v.turn_prompt}
                      </Text>
                    )}
                  </HoverCard.Dropdown>
                </HoverCard>
              </li>
            );
          })}
        </ul>
      )}

      {data?.truncated && (
        <p className={styles.note}>
          {/* No number: it would duplicate the backend's HISTORY_LIMIT across
              the wire and silently lie if that constant moved. */}
          Older versions exist than are shown here.
        </p>
      )}
    </SidebarGroup>
  );
}
