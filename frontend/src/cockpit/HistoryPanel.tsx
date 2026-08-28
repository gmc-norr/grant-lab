import { Badge, Group, SegmentedControl, Stack, Text, Title } from "@mantine/core";

import type { Artifact } from "../api/proposals";
import { useVersion, useVersions } from "../api/versions";
import styles from "./CockpitPanels.module.css";
import { DiffView } from "./DiffView";
import { lineDiff, refineRuns, tooLargeToDiff } from "./diff";
import { useHistoryNav } from "./historyNav";

function VersionRowButton({
  subject,
  committedAt,
  createdBy,
  turnPrompt,
  selected,
  onClick,
}: {
  subject: string;
  committedAt: string;
  createdBy: string | null;
  turnPrompt: string | null;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onClick}
      style={{
        display: "block",
        width: "100%",
        textAlign: "left",
        background: selected ? "var(--app-sidebar)" : "transparent",
        border: "1px solid var(--app-border)",
        borderRadius: 6,
        padding: "0.4rem 0.6rem",
        cursor: "pointer",
        color: "inherit",
      }}
    >
      <Group gap="xs" wrap="nowrap">
        <Badge size="sm" variant="light">
          {createdBy ?? "not recorded"}
        </Badge>
        <Text size="sm" style={{ flex: 1 }}>
          {subject}
        </Text>
        <Text size="xs" c="dimmed">
          {new Date(committedAt).toLocaleString()}
        </Text>
      </Group>
      {/* The "why" half of goal 1. Rendered only when a turn caused this
          version; absence is ordinary, so there is no placeholder. */}
      {turnPrompt && (
        <Text size="xs" c="dimmed" mt={2}>
          because you asked: {turnPrompt}
        </Text>
      )}
    </button>
  );
}

export function HistoryPanel({ id }: { id: string }) {
  const nav = useHistoryNav();
  const list = useVersions(id, nav.artifact);
  const data = list.data;

  // Direction is part of the contract, not an accident of selection order.
  // lineDiff assigns `del` to its FIRST argument, so compare must always pass
  // older, newer. The list is newest-first, so the LATER index is the older
  // version. Selecting the two rows in the opposite order must produce an
  // identical view — that is a test, not a convention.
  const order = data?.versions.map((v) => v.commit_sha) ?? [];
  const both = nav.a != null && nav.b != null;
  const [olderSha, newerSha] =
    both && order.indexOf(nav.a as string) < order.indexOf(nav.b as string)
      ? [nav.b, nav.a]
      : [nav.a, nav.b];

  const older = useVersion(id, nav.artifact, both ? olderSha : nav.a);
  const newer = useVersion(id, nav.artifact, both ? newerSha : null);

  return (
    <Stack gap="sm" p="sm">
      <Group justify="space-between">
        <Title order={3}>Version history</Title>
        <SegmentedControl
          size="xs"
          value={nav.artifact}
          onChange={(v) => nav.setArtifact(v as Artifact)}
          data={[
            { label: "Draft", value: "draft" },
            { label: "Spec", value: "spec" },
          ]}
        />
      </Group>

      {list.isLoading && (
        <Text size="sm" c="dimmed">
          Loading versions…
        </Text>
      )}

      {/* Distinct from `history_available: false`, which is a 200 saying git had
          nothing readable. This is the request itself failing, and without it the
          panel shows an empty header — indistinguishable from "no versions".
          Every sibling panel (DraftPanel, SpecPanel, ProblemsPanel, ClaimsPanel,
          ActivityPanel, CitationsPanel, LibraryPanel) renders this state. */}
      {list.isError && (
        <Text size="sm" c="red">
          Could not load version history.
        </Text>
      )}

      {data && !data.history_available && (
        <Text size="sm" c="dimmed">
          Version history is not available for this workspace yet.
        </Text>
      )}

      {data && data.history_available && data.versions.length === 0 && (
        <Text size="sm" c="dimmed">
          No versions yet. Versions appear when you save one, or when the assistant edits the
          draft.
        </Text>
      )}

      {data && data.versions.length > 0 && (
        <Stack gap="xs">
          {data.versions.map((v) => (
            <VersionRowButton
              key={v.commit_sha}
              subject={v.subject}
              committedAt={v.committed_at}
              createdBy={v.created_by}
              turnPrompt={v.turn_prompt}
              selected={v.commit_sha === nav.a || v.commit_sha === nav.b}
              onClick={() => nav.toggle(v.commit_sha)}
            />
          ))}
          {data.truncated && (
            <Text size="xs" c="dimmed">
              Showing the 200 most recent versions.
            </Text>
          )}
        </Stack>
      )}

      {data && data.versions.length === 1 && (
        <Text size="xs" c="dimmed">
          Comparing needs two versions.
        </Text>
      )}

      {/* One selected: read it. An old version must never look editable, so it
          renders as text the way ReviewPanel renders a review file — not the
          Tiptap editor. */}
      {!both && older.data && <pre className={styles.reviewDoc}>{older.data.markdown}</pre>}
      {!both && nav.a != null && older.isError && (
        <Text size="sm" c="dimmed">
          That version could not be read.
        </Text>
      )}

      {both && (older.isLoading || newer.isLoading) && (
        <Text size="sm" c="dimmed">
          Loading both versions…
        </Text>
      )}
      {both && (older.isError || newer.isError) && (
        <Text size="sm" c="dimmed">
          One of those versions could not be read.
        </Text>
      )}
      {both &&
        older.data &&
        newer.data &&
        (tooLargeToDiff(older.data.markdown, newer.data.markdown) ? (
          <Stack gap="xs">
            <Text size="sm" c="dimmed">
              These versions are too large to compare (over 2,000 lines). Open each one on its own
              instead.
            </Text>
          </Stack>
        ) : (
          <div data-testid="version-diff">
            <DiffView rows={refineRuns(lineDiff(older.data.markdown, newer.data.markdown))} />
          </div>
        ))}
    </Stack>
  );
}
