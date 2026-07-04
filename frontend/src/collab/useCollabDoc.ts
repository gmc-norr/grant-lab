import { useEffect, useState } from "react";
import * as Y from "yjs";
import { CollabProvider, type ProviderStatus } from "./collabProvider";

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
}): { doc: Y.Doc | null; provider: CollabProvider | null; status: ProviderStatus } {
  const [state, setState] = useState<{ doc: Y.Doc; provider: CollabProvider } | null>(null);
  const [status, setStatus] = useState<ProviderStatus>("connecting");
  const { workspaceId, artifact, canWrite } = opts;
  useEffect(() => {
    const doc = new Y.Doc();
    const provider = new CollabProvider({ url: wsUrl(workspaceId, artifact), doc, canWrite });
    setState({ doc, provider });
    setStatus(provider.status);
    const off = provider.on("status", setStatus);
    return () => {
      off();
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
  }, [workspaceId, artifact]);
  return { doc: state?.doc ?? null, provider: state?.provider ?? null, status };
}
