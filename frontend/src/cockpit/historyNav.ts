import { useCallback } from "react";
import { useSearchParams } from "react-router-dom";

import type { Artifact } from "../api/proposals";
import { confirmDiscardIfDirty } from "./editorStore";
import { tabFromParams } from "./cockpitNav";

/** Mirrors ports/shas.py's SHA_RE. A sha arriving from the URL is user input:
 *  the backend rejects a bad one with 422, but there is no reason to spend the
 *  request.
 *
 *  Deliberately still {7,64}, matching the backend: an ABBREVIATED sha is valid
 *  to git and resolves server-side, but every comparison in the panel is string
 *  identity against a full `%H` from the list, so it matches no row. That is one
 *  case of the general "selected sha is not in this list" state — which also
 *  covers a sha from the other artifact, or from another workspace — and the
 *  panel names that state rather than this regex trying to pre-empt one cause. */
const SHA_RE = /^[0-9a-fA-F]{7,64}$/;

function shaParam(sp: URLSearchParams, key: string): string | null {
  const v = sp.get(key);
  // Lowercased: SHA_RE accepts A-F, but git's %H is lowercase and every
  // comparison in HistoryPanel is string identity against it (indexOf,
  // includes). An uppercase link was accepted and then reported as "not in the
  // Versions list" — valid input, wrong answer.
  return v != null && SHA_RE.test(v) ? v.toLowerCase() : null;
}

function artifactParam(sp: URLSearchParams): Artifact {
  const v = sp.get("artifact");
  return v === "spec" ? "spec" : "draft";
}

export type DiffLayout = "split" | "unified";

/** Anything unrecognised normalises to `split`, matching artifactParam's
 *  posture: a URL is user input, and a typo should show the default view
 *  rather than an error. */
function layoutParam(sp: URLSearchParams): DiffLayout {
  return sp.get("layout") === "unified" ? "unified" : "split";
}

/** The selection rule, as a pure function of the current pair.
 *
 *  Shared by toggle() and selectAndOpen() so the two can never disagree about
 *  what a click means — the same reason blocks() is shared by refineRuns and
 *  pairRows in diff.ts. */
export function nextSelection(
  cur: { a: string | null; b: string | null },
  sha: string,
): { a: string | null; b: string | null } {
  if (sha === cur.a) return { a: cur.b, b: null };
  if (sha === cur.b) return { a: cur.a, b: null };
  if (cur.a == null) return { a: sha, b: cur.b };
  if (cur.b == null) return { a: cur.a, b: sha };
  // Both full: drop the older slot and slide along, so a third click always
  // means "compare this with the one I picked most recently".
  return { a: cur.b, b: sha };
}

export type HistoryNav = {
  artifact: Artifact;
  /** The OLDER selection slot. */
  a: string | null;
  /** The NEWER selection slot. */
  b: string | null;
  setArtifact: (x: Artifact) => void;
  toggle: (sha: string) => void;
  /** Apply the selection rule AND open the History tab in ONE
   *  setSearchParams write.
   *
   *  Calling toggle() then setTab() does NOT work: react-router 7.18.2 gives a
   *  functional updater the params memoised at RENDER time, not a live ref, so
   *  two writes in one handler both build from the pre-click URL and the second
   *  discards the first. Measured — see the PINNED test in historyNav.test.tsx
   *  and spec section 6.1. */
  selectAndOpen: (sha: string) => void;
  /** Drop both shas, keeping tab, artifact and layout.
   *
   *  The panel's own exit from a stale two-sha URL. Clicking the side pane
   *  cannot escape one: nextSelection either deselects the row (leaving the
   *  stale sha) or slides it back into the pair, a two-click cycle with no
   *  exit — pinned in historyNav.test.tsx. */
  clearSelection: () => void;
  /** How the comparison renders. Not cleared by artifact or selection changes —
   *  it is a display preference, not part of what is selected. */
  layout: DiffLayout;
  setLayout: (x: DiffLayout) => void;
};

/** The History tab's URL state: `?tab=history&artifact=…&a=…&b=…`.
 *
 *  Both shas live in the URL, not just the artifact, because a link to a
 *  comparison must reopen that comparison. Kept beside useCockpitNav rather
 *  than inside it: `?tab` and `?sec` are shared across the AppShell boundary,
 *  these three are the History tab's own. */
export function useHistoryNav(): HistoryNav {
  const [sp, setSp] = useSearchParams();
  const artifact = artifactParam(sp);
  const a = shaParam(sp, "a");
  const b = shaParam(sp, "b");
  const layout = layoutParam(sp);

  const write = useCallback(
    (next: { artifact?: Artifact; a?: string | null; b?: string | null; layout?: DiffLayout }) =>
      setSp((prev) => {
        const out = new URLSearchParams(prev);
        if (next.artifact !== undefined) out.set("artifact", next.artifact);
        if (next.layout !== undefined) out.set("layout", next.layout);
        for (const key of ["a", "b"] as const) {
          if (next[key] === undefined) continue;
          const v = next[key];
          if (v == null) out.delete(key);
          else out.set(key, v);
        }
        return out;
      }),
    [setSp],
  );

  const setArtifact = useCallback(
    // The selection is cleared: a sha from the draft's history is not a version
    // of the spec, and leaving it would fetch a 404.
    (x: Artifact) => write({ artifact: x, a: null, b: null }),
    [write],
  );

  const toggle = useCallback(
    (sha: string) => write(nextSelection({ a, b }, sha)),
    [a, b, write],
  );

  const selectAndOpen = useCallback(
    (sha: string) => {
      const next = nextSelection({ a: shaParam(sp, "a"), b: shaParam(sp, "b") }, sha);
      const opens = next.a != null || next.b != null;
      // Computed BEFORE the guard: a click that empties the selection changes no
      // destination, so there is nothing to discard and prompting would be a
      // prompt for nothing.
      //
      // `tabFromParams`, not the raw string: a legacy ?tab=history IS the
      // comparison, and comparing raw would prompt on every legacy link.
      //
      // The guard runs OUTSIDE setSp — window.confirm must never run inside an
      // updater, which is expected to be pure and runs twice under StrictMode.
      const alreadyThere = tabFromParams(sp) === "diff";
      if (opens && !alreadyThere && !confirmDiscardIfDirty()) return;
      setSp((prev) => {
        const out = new URLSearchParams(prev);
        // Read from `prev`, not the render closure: this is the one write that
        // carries both changes, so it must see the params it is about to
        // replace. See the PINNED two-writer test.
        const fresh = nextSelection(
          { a: shaParam(prev, "a"), b: shaParam(prev, "b") },
          sha,
        );
        for (const key of ["a", "b"] as const) {
          const v = fresh[key];
          if (v == null) out.delete(key);
          else out.set(key, v);
        }
        if (fresh.a != null || fresh.b != null) out.set("tab", "diff");
        return out;
      });
    },
    [sp, setSp],
  );

  const setLayout = useCallback((x: DiffLayout) => write({ layout: x }), [write]);

  const clearSelection = useCallback(() => write({ a: null, b: null }), [write]);

  return {
    artifact,
    a,
    b,
    layout,
    setArtifact,
    toggle,
    setLayout,
    selectAndOpen,
    clearSelection,
  };
}
