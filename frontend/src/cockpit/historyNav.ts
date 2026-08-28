import { useCallback } from "react";
import { useSearchParams } from "react-router-dom";

import type { Artifact } from "../api/proposals";

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
  return v != null && SHA_RE.test(v) ? v : null;
}

function artifactParam(sp: URLSearchParams): Artifact {
  const v = sp.get("artifact");
  return v === "spec" ? "spec" : "draft";
}

export type HistoryNav = {
  artifact: Artifact;
  /** The OLDER selection slot. */
  a: string | null;
  /** The NEWER selection slot. */
  b: string | null;
  setArtifact: (x: Artifact) => void;
  toggle: (sha: string) => void;
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

  const write = useCallback(
    (next: { artifact?: Artifact; a?: string | null; b?: string | null }) =>
      setSp((prev) => {
        const out = new URLSearchParams(prev);
        if (next.artifact !== undefined) out.set("artifact", next.artifact);
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
    (sha: string) => {
      if (sha === a) write({ a: b, b: null });
      else if (sha === b) write({ b: null });
      else if (a == null) write({ a: sha });
      else if (b == null) write({ b: sha });
      // Both full: drop the older slot and slide along, so a third click always
      // means "compare this with the one I picked most recently".
      else write({ a: b, b: sha });
    },
    [a, b, write],
  );

  return { artifact, a, b, setArtifact, toggle };
}
