import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import * as Y from "yjs";
import { CollabProvider, type ProviderStatus } from "./collabProvider";

const MAX_DOCUMENT_REPLACEMENTS = 8;
const DOCUMENT_REPLACEMENT_BASE_MS = 100;
const DOCUMENT_REPLACEMENT_MAX_MS = 2_000;

function wsUrl(workspaceId: string, artifact: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/api/ws/workspaces/${workspaceId}/${artifact}`;
}

/**
 * Own the Y.Doc + provider lifecycle for one (workspace, artifact).
 *
 * The doc/provider are built INSIDE the effect (not lazily in render) and
 * surfaced via state. React StrictMode dev-double-mounts every effect
 * (setup → cleanup → setup); creating in render + tearing down in cleanup
 * would leave the second setup dereferencing a nulled ref. Building in the
 * effect makes each mount self-contained: cleanup destroys this mount's
 * doc/provider, and the next setup builds a fresh pair. `doc`/`provider` are
 * null on the first render (before the effect runs) — CollabEditor renders a
 * placeholder until they exist.
 */
export function useCollabDoc(opts: {
  workspaceId: string;
  artifact: "draft" | "spec";
  canWrite: boolean;
}): {
  doc: Y.Doc | null;
  provider: CollabProvider | null;
  status: ProviderStatus;
  synced: boolean;
} {
  const [state, setState] = useState<{ doc: Y.Doc; provider: CollabProvider } | null>(null);
  const [status, setStatus] = useState<ProviderStatus>("connecting");
  // Separate from status on purpose: see CollabProvider.synced.
  const [synced, setSynced] = useState(false);
  const [documentGeneration, setDocumentGeneration] = useState(0);
  const qc = useQueryClient();
  const replacementAttempts = useRef(0);
  const replacementTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { workspaceId, artifact, canWrite } = opts;
  useEffect(() => {
    replacementAttempts.current = 0;
    return () => {
      if (replacementTimer.current) clearTimeout(replacementTimer.current);
      replacementTimer.current = null;
    };
  }, [workspaceId, artifact]);
  useEffect(() => {
    const doc = new Y.Doc();
    const provider = new CollabProvider({
      url: wsUrl(workspaceId, artifact),
      doc,
      canWrite,
      onDocumentReplaced: () => {
        // A branch switch changes the document behind the stable room URL.
        // Hide the stale doc immediately; effect cleanup destroys it before a
        // fresh pair connects and hydrates the newly selected branch.
        setState(null);
        setSynced(false);
        // The Y.Doc is only half the cockpit. When ANOTHER member switches the
        // branch, this socket is closed with 4409 and the editor body follows —
        // but every TanStack read-model stays on the old branch, and nothing
        // else can refresh it: these keys have no interval, refetchOnWindowFocus
        // is off, staleTime is 20 s and the observers never unmount. The result
        // was a cockpit that agreed with itself and was wrong: the status-bar
        // pill and BranchBar named the OLD branch (offering "switch to" the one
        // you were already on), the Versions pane listed the old history, and
        // the selection toolbar ran live claims against the old sections.
        // These are the same four keys the user's OWN switch invalidates
        // (api/branches.ts); a remote switch must not be told less.
        void Promise.all([
          qc.invalidateQueries({ queryKey: ["branches", workspaceId] }),
          qc.invalidateQueries({ queryKey: ["draft", workspaceId] }),
          qc.invalidateQueries({ queryKey: ["spec", workspaceId] }),
          qc.invalidateQueries({ queryKey: ["versions", workspaceId] }),
        ]);
        // NOT cockpit-meta: it carries title/deadline/phase/role/spec_locked/
        // member_count only (web/api/workspaces.py CockpitMetaOut) — nothing a
        // branch switch changes.
        if (replacementAttempts.current >= MAX_DOCUMENT_REPLACEMENTS) {
          setStatus("offline-permanent");
          return;
        }
        const delay = Math.min(
          DOCUMENT_REPLACEMENT_BASE_MS * 2 ** replacementAttempts.current,
          DOCUMENT_REPLACEMENT_MAX_MS,
        );
        replacementAttempts.current += 1;
        setStatus("offline");
        replacementTimer.current = setTimeout(() => {
          replacementTimer.current = null;
          setStatus("connecting");
          setDocumentGeneration((generation) => generation + 1);
        }, delay);
      },
    });
    setState({ doc, provider });
    setStatus(provider.status);
    setSynced(provider.synced);
    const off = provider.on("status", setStatus);
    const offSync = provider.on("sync", (isSynced) => {
      setSynced(isSynced);
      if (isSynced) replacementAttempts.current = 0;
    });
    return () => {
      off();
      offSync();
      provider.destroy();
      doc.destroy();
      setState(null);
    };
    // canWrite is intentionally NOT a dependency: it is the CONNECT-TIME write
    // gate (the server also enforces it and reconciles permission mid-session —
    // Slice 2c). Rebuilding the doc/provider on a permission flip would destroy
    // the Y.Doc out from under the bound editor; full client-side mid-session
    // editability is a 3b-4 concern.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId, artifact, documentGeneration]);
  return { doc: state?.doc ?? null, provider: state?.provider ?? null, status, synced };
}
