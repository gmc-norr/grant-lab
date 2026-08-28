import { Popover, Text } from "@mantine/core";
import { useEffect, useRef, useState } from "react";

import { useVersions, type VersionRow } from "../api/versions";
import { useHistoryNav } from "./historyNav";
import { PaneHeader } from "./panes/PaneHeader";
import styles from "./VersionGraph.module.css";

/** One version row with a controlled Popover that opens on hover (250ms) or keyboard focus.
 *
 *  Each row needs its own `opened` state and delay timer, which is why the row
 *  is extracted into its own component rather than inlined into the `.map`. */
function VersionGraphRow({ v, selected, onSelect }: { v: VersionRow; selected: boolean; onSelect: () => void }) {
  const [opened, setOpened] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Clear any pending open timer on unmount to avoid a state update on an
  // unmounted component.
  useEffect(() => {
    return () => {
      if (timerRef.current != null) {
        clearTimeout(timerRef.current);
      }
    };
  }, []);

  const sha7 = v.commit_sha.slice(0, 7);

  function handleMouseEnter() {
    timerRef.current = setTimeout(() => {
      setOpened(true);
    }, 250);
  }

  function handleMouseLeave() {
    if (timerRef.current != null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    setOpened(false);
  }

  function handleFocus() {
    if (timerRef.current != null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    setOpened(true);
  }

  function handleBlur() {
    setOpened(false);
  }

  return (
    <li className={styles.row}>
      <Popover
        position="right-start"
        withinPortal
        shadow="md"
        width={340}
        opened={opened}
      >
        <Popover.Target>
          <button
            type="button"
            className={styles.button}
            aria-pressed={selected}
            onClick={onSelect}
            onMouseEnter={handleMouseEnter}
            onMouseLeave={handleMouseLeave}
            onFocus={handleFocus}
            onBlur={handleBlur}
          >
            <span className={styles.lane} aria-hidden="true">
              <span className={styles.dot} data-role={v.created_by ?? "none"} />
            </span>
            <span className={styles.sha}>{sha7}</span>
            <span className={styles.subject}>{v.subject}</span>
          </button>
        </Popover.Target>
        {/* What will not fit at 276px lives here. `right-start` puts it
            beside the pane; Mantine's floating middleware flips it when
            there is no room to the right (a widened pane, a narrow
            window), so no manual positioning is needed. */}
        <Popover.Dropdown data-testid={`version-card-${sha7}`}>
          <Text size="sm" fw={600}>
            {v.subject}
          </Text>
          <Text size="xs" c="dimmed" mt={2}>
            {v.created_by ?? "not recorded"} · {sha7} ·{" "}
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
        </Popover.Dropdown>
      </Popover>
    </li>
  );
}

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
    <>
      <PaneHeader title="Versions" count={rows.length || undefined} />
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
              <VersionGraphRow
                key={v.commit_sha}
                v={v}
                selected={selected}
                onSelect={() => nav.selectAndOpen(v.commit_sha)}
              />
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
    </>
  );
}
