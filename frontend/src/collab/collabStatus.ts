import type { SaveState } from "../cockpit/editorStore";
import type { ProviderStatus } from "./collabProvider";

export function collabSaveState(status: ProviderStatus, canWrite: boolean): SaveState {
  if (!canWrite) return "collab-readonly";
  switch (status) {
    case "connected": return "collab-synced";
    case "offline": return "collab-offline";
    case "offline-permanent": return "collab-disconnected";
    case "closed-policy": return "collab-readonly";
    case "connecting": return "collab-connecting";
  }
  // Exhaustive on purpose: there is NO `default`. There used to be one, and it
  // meant a newly added ProviderStatus rendered as "Connecting…" — a lie tsc
  // cannot catch, because a default makes every switch type-check. With this
  // shape, adding a status without a label is a compile error.
  const unreachable: never = status;
  return unreachable;
}
