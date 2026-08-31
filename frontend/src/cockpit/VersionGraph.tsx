import { Menu, Popover, SegmentedControl, Text } from "@mantine/core";
import { useEffect, useMemo, useRef, useState } from "react";

import { useBranches } from "../api/branches";
import type { Artifact } from "../api/proposals";
import { useVersions, type VersionRow } from "../api/versions";
import { BranchBar } from "./BranchBar";
import { useHistoryNav } from "./historyNav";
import { PaneHeader } from "./panes/PaneHeader";
import { LANE_W, VersionLane } from "./VersionLane";
import { assignLanes, type LaneRow } from "./versionLanes";
import styles from "./VersionGraph.module.css";

/** "2h", "3d" — a coarse age, because the exact stamp is in the hover card and a
 *  row has about 40px for this. */
export function shortAge(iso: string, now: number): string {
  const secs = Math.max(0, (now - new Date(iso).getTime()) / 1000);
  if (secs < 60) return "now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
  if (secs < 86400 * 30) return `${Math.floor(secs / 86400)}d`;
  return `${Math.floor(secs / (86400 * 30))}mo`;
}

/** One version row: lane strip, branch pills, the selectable middle, and a row
 *  actions menu.
 *
 *  The row is a flex list of SIBLINGS, not one big button. The pills and the
 *  actions trigger are buttons, and a button inside a button is invalid HTML.
 *
 *  Each row needs its own Popover `opened` state and delay timer, which is why
 *  this is a component rather than inlined into the `.map`. */
function VersionGraphRow({
  v,
  selected,
  onSelect,
  laneRow,
  columns,
  pills,
  currentBranch,
  onSwitch,
  onBranchFrom,
  branchesUnavailable,
  now,
}: {
  v: VersionRow;
  selected: boolean;
  onSelect: () => void;
  laneRow: LaneRow;
  columns: number;
  /** Branch names whose tip is this commit. */
  pills: string[];
  currentBranch: string | undefined;
  onSwitch: (name: string) => void;
  onBranchFrom: () => void;
  /** The branch list could not be read. Branching needs it -- it is what says
   *  which names are already taken -- so the item is offered but inert. */
  branchesUnavailable: boolean;
  now: number;
}) {
  const [opened, setOpened] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
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

  // A controlled Popover, NOT a HoverCard: HoverCardTarget binds only
  // onMouseEnter/onMouseLeave, so anything living in it is invisible to
  // keyboard and screen-reader users.
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
    <li
      className={styles.row}
      onContextMenu={(e) => {
        // Without preventDefault the browser's own menu opens on top of ours.
        e.preventDefault();
        setMenuOpen(true);
      }}
    >
      <VersionLane row={laneRow} columns={columns} role={v.created_by} selected={selected} />
      {pills.map((name) => {
        const isCurrent = name === currentBranch;
        return (
          <button
            key={name}
            type="button"
            className={styles.pill}
            data-current={isCurrent}
            disabled={isCurrent}
            aria-label={isCurrent ? name : `Switch to ${name}`}
            onClick={() => onSwitch(name)}
          >
            ⑂ {name}
          </button>
        );
      })}
      <Popover position="right-start" withinPortal shadow="md" width={340} opened={opened}>
        <Popover.Target>
          <button
            type="button"
            className={styles.button}
            // The sha leaves the visible row but NOT the accessible name.
            // Subjects repeat -- "collab checkpoint: draft" appears on every
            // checkpoint -- so the subject alone cannot name a row, and a
            // screen-reader user would hear the same label several times with
            // no way to tell the versions apart.
            aria-label={`${sha7} ${v.subject}`}
            aria-pressed={selected}
            onClick={onSelect}
            onMouseEnter={handleMouseEnter}
            onMouseLeave={handleMouseLeave}
            onFocus={handleFocus}
            onBlur={handleBlur}
          >
            {/* No sha column. At the pane's 276px default the sha plus a
                branch pill left about 60px for the subject, which rendered as
                "col…" -- measured on a real screenshot. VS Code's source
                control graph does not show it either: it lives in the hover
                card and in the row menu's "Copy commit id". */}
            <span className={styles.subject}>{v.subject}</span>
            <span className={styles.when}>{shortAge(v.committed_at, now)}</span>
          </button>
        </Popover.Target>
        {/* What will not fit at 276px lives here. `right-start` puts it beside
            the pane; Mantine's floating middleware flips it when there is no
            room to the right. `role="region"` because a bare <pre> or <div>
            has no role that accepts an accessible name. */}
        <Popover.Dropdown data-testid={`version-card-${sha7}`}>
          <Text size="sm" fw={600}>
            {v.subject}
          </Text>
          <Text size="xs" c="dimmed" mt={2}>
            {v.created_by ?? "not recorded"} · {sha7} ·{" "}
            {new Date(v.committed_at).toLocaleString()}
          </Text>
          {/* The "why". Null for every reconciled row and every commit with no
              DB row, so absence is ordinary — render nothing, not a
              placeholder. */}
          {v.turn_prompt && (
            <Text size="xs" mt={6}>
              because you asked: {v.turn_prompt}
            </Text>
          )}
        </Popover.Dropdown>
      </Popover>
      {/* Always in the DOM, only visually quiet until hover/focus: a control
          that exists only while the mouse is over it cannot be reached by
          keyboard. Controlled so the row's right-click can open it too. */}
      <Menu position="bottom-end" withinPortal opened={menuOpen} onChange={setMenuOpen}>
        <Menu.Target>
          <button type="button" className={styles.kebab} aria-label={`Actions for ${sha7}`}>
            ⋯
          </button>
        </Menu.Target>
        <Menu.Dropdown>
          <Menu.Item onClick={onBranchFrom} disabled={branchesUnavailable}>
            Branch from here…
          </Menu.Item>
          <Menu.Item onClick={() => navigator.clipboard?.writeText(v.commit_sha)}>
            Copy commit id
          </Menu.Item>
        </Menu.Dropdown>
      </Menu>
    </li>
  );
}

/** The cockpit side pane's Versions group, drawn as a commit graph with a lane
 *  per branch and the branch bar on top.
 *
 *  Git-sourced, via api/versions, and now spanning every branch: a lane needs
 *  the commits that are ON the other branch. Which branch you are on is shown
 *  by which tip carries the filled pill. */
export function VersionGraph({ id }: { id: string }) {
  const nav = useHistoryNav();
  const list = useVersions(id, nav.artifact);
  const branches = useBranches(id);
  const data = list.data;
  const rows = data?.versions ?? [];

  const [branchFrom, setBranchFrom] = useState<{ sha: string; subject: string } | null>(null);
  const [switchTo, setSwitchTo] = useState<string | null>(null);

  // One age baseline for the whole render, so two rows cannot disagree about
  // what "now" is.
  const now = Date.now();

  const lanes = useMemo(
    () => assignLanes(rows.map((v) => ({ sha: v.commit_sha, parents: v.parents }))),
    [rows],
  );
  // ONE width for every strip. Sizing each row by its own `columns` would slide
  // the pills and subject sideways as the graph narrows.
  const columns = Math.max(1, ...lanes.map((l) => l.columns));

  const tips = branches.data?.tips ?? {};
  // A branch whose tip commit does not touch the selected artifact has no row
  // to sit on and therefore no pill. It is still listed in the branch menu.
  const pillsFor = (sha: string) => Object.keys(tips).filter((name) => tips[name] === sha);

  return (
    <>
      <PaneHeader title="Versions" count={rows.length || undefined}>
        {/* Lives here, not in the comparison: this decides WHOSE history the
            list below shows, so it belongs on the list. */}
        <SegmentedControl
          size="xs"
          aria-label="Which artifact"
          value={nav.artifact}
          onChange={(v) => nav.setArtifact(v as Artifact)}
          data={[
            { label: "Draft", value: "draft" },
            { label: "Spec", value: "spec" },
          ]}
        />
      </PaneHeader>
      <BranchBar
        id={id}
        branchFrom={branchFrom}
        onBranchFromHandled={() => setBranchFrom(null)}
        switchTo={switchTo}
        onSwitchHandled={() => setSwitchTo(null)}
      />
      {/* Each state gets its own words. "Could not load" and "not available" and
          "none yet" are three different situations, and a single empty message
          for all three tells the user nothing about which. */}
      {list.isError && <p className={styles.note}>Could not load version history.</p>}
      {!list.isError && data && !data.history_available && (
        <p className={styles.note}>Version history is not available yet.</p>
      )}
      {!list.isError && data?.history_available && rows.length === 0 && (
        <p className={styles.note}>No versions yet.</p>
      )}

      {rows.length > 0 && (
        <ul className={styles.list} aria-label="Versions" style={{ ["--lane-w" as string]: `${LANE_W}px` }}>
          {rows.map((v, i) => (
            <VersionGraphRow
              key={v.commit_sha}
              v={v}
              laneRow={lanes[i]}
              columns={columns}
              pills={pillsFor(v.commit_sha)}
              currentBranch={branches.data?.current}
              onSwitch={setSwitchTo}
              onBranchFrom={() => setBranchFrom({ sha: v.commit_sha, subject: v.subject })}
              branchesUnavailable={branches.isError}
              now={now}
              selected={v.commit_sha === nav.a || v.commit_sha === nav.b}
              onSelect={() => nav.selectAndOpen(v.commit_sha)}
            />
          ))}
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
