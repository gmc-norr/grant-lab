import type { SaveState } from "../cockpit/editorStore";
import type { ProviderStatus } from "./collabProvider";

export function collabSaveState(status: ProviderStatus, canWrite: boolean): SaveState {
  if (!canWrite) return "collab-readonly";
  switch (status) {
    case "connected": return "collab-synced";
    case "offline": return "collab-offline";
    case "offline-permanent": return "collab-disconnected";
    // NOT "collab-readonly". That label ("Read-only") describes a live session a
    // viewer can still read and follow — but a 1008/1009 close means the socket
    // is gone and the provider will not retry it, so the writer was being told
    // their session was fine while their keystrokes went nowhere. Its own state
    // rather than "collab-disconnected" because that one is OUR retries running
    // out; this one is the server refusing. `!canWrite` above still covers the
    // genuine viewer case.
    case "closed-policy": return "collab-closed";
    case "connecting": return "collab-connecting";
  }
  // Exhaustive on purpose: there is NO `default`. There used to be one, and it
  // meant a newly added ProviderStatus rendered as "Connecting…" — a lie tsc
  // cannot catch, because a default makes every switch type-check. With this
  // shape, adding a status without a label is a compile error.
  const unreachable: never = status;
  return unreachable;
}
