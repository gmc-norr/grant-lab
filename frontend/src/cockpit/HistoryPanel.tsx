import { Badge, Group, SegmentedControl, Stack, Text, Title } from "@mantine/core";

import type { Artifact } from "../api/proposals";
import { useVersion, useVersions } from "../api/versions";
import styles from "./CockpitPanels.module.css";
import { DiffView } from "./DiffView";
import { lineDiff, pairRows, refineRuns, tooLargeToDiff } from "./diff";
import { SideBySideDiffView } from "./SideBySideDiffView";
import { type DiffLayout, useHistoryNav } from "./historyNav";

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
        {/* gitio caps a subject at 500 chars, and it carries a user-controlled
            section heading (editing/save.py:275). Unclamped, a long heading
            wraps the row and pushes the timestamp out of view. */}
        <Text size="sm" style={{ flex: 1 }} lineClamp={2}>
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
  const ia = order.indexOf(nav.a as string);
  const ib = order.indexOf(nav.b as string);
  // BOTH shas must be FOUND in the list before direction is knowable. Before the
  // list resolves `order` is [] and both indexOf calls return -1 — and `-1 < -1`
  // is false, which silently orders by whatever the URL happened to say. The two
  // version payloads are smaller than the list and can win the race, so a deep
  // link would render every addition as a deletion until the list landed. `ia`
  // and `ib` are also -1 for a sha that is not in this artifact's history at all.
  const ordered = both && ia >= 0 && ib >= 0;
  const [olderSha, newerSha] = ordered && ia < ib ? [nav.b, nav.a] : [nav.a, nav.b];

  // `nav.a ?? nav.b`: a hand-trimmed `?b=` with no `?a=` is otherwise a dead
  // state — the row highlights and nothing renders.
  const older = useVersion(id, nav.artifact, both ? olderSha : (nav.a ?? nav.b));
  const newer = useVersion(id, nav.artifact, both ? newerSha : null);

  // The header timestamps come from the LIST row, not the version payload:
  // VersionContent is {commit_sha, artifact, markdown} and carries no
  // committed_at by design. Degrades to the short sha alone when the row is
  // absent — a state the panel already names elsewhere.
  const labelFor = (sha: string | null) => {
    const short = (sha ?? "").slice(0, 7);
    const row = data?.versions.find((v) => v.commit_sha === sha);
    return row ? `${short} · ${new Date(row.committed_at).toLocaleString()}` : short;
  };

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
              {/* No number: it would duplicate the backend's HISTORY_LIMIT
                  across the wire and silently lie if that constant moved. */}
              Older versions exist than are shown here.
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
      {/* Gated on the same expression the FETCH uses. Gating on `nav.a` alone
          left a hand-trimmed `?b=` whose read failed rendering nothing. */}
      {!both && (nav.a ?? nav.b) != null && older.isError && (
        <Text size="sm" c="dimmed">
          That version could not be read. Pick one from the list above.
        </Text>
      )}

      {/* The selection is deliberately NOT cleared from the URL here: rewriting
          the address the user arrived on costs them the back button and hides
          which link was broken. The list below stays usable, which is what §6.4
          is actually protecting. */}
      {both && (older.isError || newer.isError) && (
        <Text size="sm" c="dimmed">
          One of those versions could not be read. Pick two from the list above.
        </Text>
      )}
      {both && !ordered && !list.isError && list.isLoading && (
        <Text size="sm" c="dimmed">
          Loading both versions…
        </Text>
      )}
      {/* The list is loaded and one of the two selections is not in it — an
          abbreviated sha, a sha from the other artifact, a stale link. Direction
          is unknowable, so say so rather than waiting on it forever. */}
      {both && !ordered && !list.isError && !list.isLoading && (
        <Text size="sm" c="dimmed">
          One of the selected versions is not in this list. Pick two from above.
        </Text>
      )}
      {both &&
        ordered &&
        older.data &&
        newer.data &&
        (tooLargeToDiff(older.data.markdown, newer.data.markdown) ? (
          <Stack gap="xs">
            <Text size="sm" c="dimmed">
              These versions are too large to compare (over 2,000 lines), so the full text of each
              is shown without highlighting.
            </Text>
            {/* Both payloads are already in hand. A note on its own is a dead
                end — the user is told the comparison failed and given nothing
                to read. Older on the left, newer on the right, same order the
                diff would have used. */}
            <Group align="flex-start" gap="sm" grow wrap="nowrap">
              <pre className={styles.reviewDoc} data-testid="version-side">
                {older.data.markdown}
              </pre>
              <pre className={styles.reviewDoc} data-testid="version-side">
                {newer.data.markdown}
              </pre>
            </Group>
          </Stack>
        ) : (
          <Stack gap="xs">
            <Group justify="flex-end">
              <SegmentedControl
                size="xs"
                aria-label="Compare layout"
                value={nav.layout}
                onChange={(v) => nav.setLayout(v as DiffLayout)}
                data={[
                  { label: "Side-by-side", value: "split" },
                  { label: "Unified", value: "unified" },
                ]}
              />
            </Group>
            {nav.layout === "unified" ? (
              <div data-testid="version-diff">
                <DiffView rows={refineRuns(lineDiff(older.data.markdown, newer.data.markdown))} />
              </div>
            ) : (
              <SideBySideDiffView
                pairs={pairRows(refineRuns(lineDiff(older.data.markdown, newer.data.markdown)))}
                olderLabel={labelFor(olderSha)}
                newerLabel={labelFor(newerSha)}
              />
            )}
          </Stack>
        ))}
    </Stack>
  );
}
