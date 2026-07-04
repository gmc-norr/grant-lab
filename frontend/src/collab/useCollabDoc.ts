import { useEffect, useRef, useState } from "react";
import * as Y from "yjs";
import { CollabProvider, type ProviderStatus } from "./collabProvider";

function wsUrl(workspaceId: string, artifact: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/api/ws/workspaces/${workspaceId}/${artifact}`;
}

export function useCollabDoc(opts: {
  workspaceId: string;
  artifact: "draft" | "spec";
  canWrite: boolean;
}) {
  const [status, setStatus] = useState<ProviderStatus>("connecting");
  const ref = useRef<{ doc: Y.Doc; provider: CollabProvider } | null>(null);
  if (ref.current === null) {
    const doc = new Y.Doc();
    const provider = new CollabProvider({
      url: wsUrl(opts.workspaceId, opts.artifact),
      doc,
      canWrite: opts.canWrite,
    });
    ref.current = { doc, provider };
  }
  useEffect(() => {
    const { provider } = ref.current!;
    const off = provider.on("status", setStatus);
    return () => {
      off();
      provider.destroy();
      ref.current!.doc.destroy();
      ref.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opts.workspaceId, opts.artifact]);
  return { doc: ref.current.doc, provider: ref.current.provider, status };
}
