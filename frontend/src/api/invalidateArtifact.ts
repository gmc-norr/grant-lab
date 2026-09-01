import type { QueryClient } from "@tanstack/react-query";

/** Artifacts whose content lives in git and therefore has a version list. */
export type ArtifactKey = "draft" | "spec";

/**
 * Invalidate everything that changes when an artifact's content changes.
 *
 * Every content mutation in this app produces a git commit — that is the whole
 * storage design — so a new VERSION appears at the same moment the text does.
 * The two used to be invalidated separately, and the version list simply was
 * not: `useSaveVersion` invalidated `[artifact, id]` while the list lives under
 * `["versions", id, artifact]`, and that key appeared in no `invalidateQueries`
 * call anywhere in the frontend. With `refetchOnWindowFocus: false`, a 20 s
 * `staleTime` and no interval, the pane kept rendering the pre-checkpoint list:
 * the version the user had just created was absent and could not be selected
 * for comparison, and clicking Save again was a server-side no-op that reported
 * nothing.
 *
 * `["versions", id]` is deliberately the PREFIX, not the full key — TanStack
 * matches by prefix, so this refreshes both artifacts' lists. A branch switch
 * already did exactly this (api/branches.ts); it was the only caller that did.
 *
 * A commit also advances the current branch tip. Refreshing history without
 * refreshing branches leaves its pill on the previous commit, making the graph
 * look as though the new commit landed on another branch.
 *
 * Call this instead of invalidating the artifact key by hand. The gate in
 * `invalidateArtifact.test.ts` fails any source file that does the latter.
 */
export function invalidateArtifact(
  qc: QueryClient,
  id: string,
  artifact: ArtifactKey,
): Promise<void> {
  return Promise.all([
    qc.invalidateQueries({ queryKey: [artifact, id] }),
    qc.invalidateQueries({ queryKey: ["versions", id] }),
    qc.invalidateQueries({ queryKey: ["branches", id] }),
  ]).then(() => undefined);
}
