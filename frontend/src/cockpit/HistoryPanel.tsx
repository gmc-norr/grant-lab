import { Button, Group, SegmentedControl, Stack, Text, Title } from "@mantine/core";
import { useId } from "react";

import type { Artifact } from "../api/proposals";
import { useVersion, useVersions } from "../api/versions";
import styles from "./CockpitPanels.module.css";
import { DiffView } from "./DiffView";
import { lineDiff, pairRows, refineRuns, tooLargeToDiff } from "./diff";
import { SideBySideDiffView } from "./SideBySideDiffView";
import { type DiffLayout, useHistoryNav } from "./historyNav";

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
  // `nav.a !== nav.b`: the same sha in both slots is not a comparison. It cannot
  // arise from clicking — nextSelection deselects a row that is already
  // selected — so it comes from a hand-edited or truncated link, and diffing a
  // document against itself renders an empty diff that explains nothing. Treated
  // as the single selection it really is.
  const both = nav.a != null && nav.b != null && nav.a !== nav.b;
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

  // One place decides whether comparing is even possible. Three messages used to
  // say "pick two from the side pane" unconditionally; against a one-version
  // history all three are unfollowable, because the pane holds one row. `both`
  // comes from the URL, not from clicking, so a shared or stale link reaches the
  // recovery messages in a workspace that never had two versions.
  //
  // Zero and one need DIFFERENT words, not one "cannot compare" message: an
  // empty workspace told "only one version" contradicts the "No versions yet"
  // line rendered directly above it.
  const shown = data?.versions.length ?? 0;
  const canCompare = shown > 1;
  const pickAdvice = canCompare
    ? "Pick two from the Versions list in the side pane."
    : shown === 1
      ? "This workspace has only one version, so there is nothing to compare it with."
      : "This workspace has no versions to compare.";

  // The lone selection, and whether it is a sha this artifact's history actually
  // contains. A stale `?a=` is NOT "the only version this workspace has" — the
  // workspace's one version is a different sha, and saying otherwise names the
  // wrong thing.
  const single = both ? null : (nav.a ?? nav.b);
  // Membership is UNKNOWABLE until the list lands. `order` is [] while it is in
  // flight and when the request failed, which made `singleInList` false for a
  // perfectly valid sha — so a read that failed first was reported as a stale
  // link, printed beside "Loading versions…". Same class of race as `ordered`.
  // `history_available` too: a 200 saying git was unreadable carries
  // `versions: []`, and treating that as authoritative made the panel claim a
  // selection was absent and the workspace empty — neither of which is known.
  const listReady = !list.isError && data != null && data.history_available;
  const singleInList = single != null && order.includes(single);
  const singleAbsent = listReady && single != null && !singleInList;

  // useId, not a literal: two HistoryPanels on one page would otherwise share an
  // id and aria-labelledby would resolve to the first one for both.
  const olderPaneId = useId();
  const newerPaneId = useId();

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
          aria-label="Which artifact"
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

      {/* NEITHER selected — not "fewer than two". One selected renders as a
          read-only document below, and that behaviour is kept.

          Split on the list length: "pick two" is unfollowable against a
          one-version history, because the side pane has exactly one row to
          click. Both branches point at the same place; only the instruction
          differs. */}
      {nav.a == null && nav.b == null && shown === 1 && (
        <Text size="sm" c="dimmed">
          Only one version so far. Select it in the Versions list in the side pane to read it —
          comparing needs a second one.
        </Text>
      )}
      {nav.a == null && nav.b == null && canCompare && (
        <Text size="sm" c="dimmed">
          Pick two versions from the Versions list in the side pane to compare them.
        </Text>
      )}

      {/* One selected: read it. An old version must never look editable, so it
          renders as text the way ReviewPanel renders a review file — not the
          Tiptap editor. */}
      {!both && older.data && <pre className={styles.reviewDoc}>{older.data.markdown}</pre>}
      {/* The LIST has landed and the payload has not. Without this the panel is
          a header over blank space, which reads as "nothing here" rather than
          "wait" — the list's own loading message is already gone by now. */}
      {!both && single != null && older.isLoading && (
        <Text size="sm" c="dimmed">
          Loading that version…
        </Text>
      )}
      {/* Gated on the same expression the FETCH uses. Gating on `nav.a` alone
          left a hand-trimmed `?b=` whose read failed rendering nothing. */}
      {!both && single != null && older.isError && singleAbsent && (
        <Stack gap="xs" align="flex-start">
          <Text size="sm" c="dimmed">
            That version is not in the Versions list — the link may be stale, or point at the
            other artifact.
          </Text>
          <ClearComparison onClear={nav.clearSelection} pair={both} />
        </Stack>
      )}
      {/* No "pick one from the list": that instruction does not do what it says.
          From {a: unreadable, b: null} the next click yields
          {a: unreadable, b: clicked} — a comparison CONTAINING the unreadable
          version, not the version the user clicked. Clearing first is the only
          route to reading another one, so the control is the advice. */}
      {!both && single != null && older.isError && !singleAbsent && (
        <Stack gap="xs" align="flex-start">
          <Text size="sm" c="dimmed">That version could not be read.</Text>
          <ClearComparison onClear={nav.clearSelection} pair={both} />
        </Stack>
      )}

      {/* The selection is deliberately NOT cleared from the URL here: rewriting
          the address the user arrived on costs them the back button and hides
          which link was broken. The side pane's list stays usable, which is what §6.4
          is actually protecting. */}
      {/* Gated on `ordered`, so this and the not-in-list branch below partition
          the `both` failure space instead of overlapping. Ungated, a stale sha
          whose read ALSO failed rendered both messages and two Clear buttons.
          Reached with two DISTINCT listed shas — `both` requires them to differ
          — one of which cannot be read. */}
      {both && ordered && (older.isError || newer.isError) && (
        <Stack gap="xs" align="flex-start">
          {/* No "pick two" here. Both shas are in the list and one of them
              cannot be read, so with exactly two versions no readable pair
              exists and the instruction cannot be followed however many times
              it is tried. The control is the action. */}
          <Text size="sm" c="dimmed">
            One of those versions could not be read.
          </Text>
          <ClearComparison onClear={nav.clearSelection} pair={both} />
        </Stack>
      )}
      {/* Two separate waits, and both must say so: the LIST may still be in
          flight (direction is not yet knowable), or the list is in hand and the
          two payloads are not. The second left the panel blank. */}
      {((both && !ordered && !list.isError && list.isLoading) ||
        (both &&
          ordered &&
          !older.isError &&
          !newer.isError &&
          (older.isLoading || newer.isLoading))) && (
        <Text size="sm" c="dimmed">
          Loading both versions…
        </Text>
      )}
      {/* The list is loaded and one of the two selections is not in it — an
          abbreviated sha, a sha from the other artifact, a stale link. Direction
          is unknowable, so say so rather than waiting on it forever. */}
      {both && !ordered && listReady && !list.isLoading && (
        <Stack gap="xs" align="flex-start">
          <Text size="sm" c="dimmed">
            One of the selected versions is not in the Versions list. {pickAdvice}
          </Text>
          {!canCompare && <ClearComparison onClear={nav.clearSelection} pair={both} />}
        </Stack>
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
                diff would have used — and SAID, not just commented: two bare
                <pre> blocks carry no order, least of all to a screen reader
                that lands on the pane rather than on the text above it.

                minWidth: 0 on each column — a flex item's minimum is its
                content's max-content width, so a long unbroken token would push
                the pane wider than its share instead of scrolling inside it. */}
            <Group align="flex-start" gap="sm" grow wrap="nowrap">
              <Stack gap={4} style={{ minWidth: 0 }}>
                <Text size="xs" c="dimmed" id={olderPaneId} data-testid="version-side-header-older">
                  older · {labelFor(olderSha)}
                </Text>
                {/* role="region": a bare <pre> has no role that accepts a
                    name, so aria-labelledby on it resolves to nothing. With the
                    role the heading becomes the pane's accessible name. */}
                <pre
                  className={styles.reviewDoc}
                  data-testid="version-side"
                  role="region"
                  aria-labelledby={olderPaneId}
                >
                  {older.data.markdown}
                </pre>
              </Stack>
              <Stack gap={4} style={{ minWidth: 0 }}>
                <Text size="xs" c="dimmed" id={newerPaneId} data-testid="version-side-header-newer">
                  newer · {labelFor(newerSha)}
                </Text>
                <pre
                  className={styles.reviewDoc}
                  data-testid="version-side"
                  role="region"
                  aria-labelledby={newerPaneId}
                >
                  {newer.data.markdown}
                </pre>
              </Stack>
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

/** The way out of a selection the user cannot fix by clicking.
 *
 *  Clicking the side pane cannot escape a stale pair — nextSelection either
 *  deselects the row (leaving the stale sha) or slides it back into the pair, a
 *  two-click cycle with no exit, pinned in historyNav.test.tsx.
 *
 *  Offered on three states: a stale single selection that is not in the list; an
 *  ordered pair where a read failed (with two versions there is no readable pair
 *  left); and a not-in-list pair in a history too short to compare. Not offered
 *  where picking again genuinely works.
 *
 *  The URL is still not rewritten on the user's behalf: they arrived on this
 *  link, and silently changing the address costs them the back button and hides
 *  which link was broken. This makes the correction theirs to make. */
function ClearComparison({ onClear, pair }: { onClear: () => void; pair: boolean }) {
  return (
    <Button size="xs" variant="default" onClick={onClear}>
      {/* `!pair` means there is no comparison — naming one in the visible label
          and the accessible name describes state that does not exist. */}
      {pair ? "Clear the comparison" : "Clear selection"}
    </Button>
  );
}
