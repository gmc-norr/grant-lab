import type { SaveState } from "../cockpit/editorStore";
import type { ProviderStatus } from "./collabProvider";

export function collabSaveState(status: ProviderStatus, canWrite: boolean): SaveState {
  if (!canWrite) return "collab-readonly";
  switch (status) {
    case "connected": return "collab-synced";
    case "offline": return "collab-offline";
    case "closed-policy": return "collab-readonly";
    default: return "collab-connecting";
  }
}
