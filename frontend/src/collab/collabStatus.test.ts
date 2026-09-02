import { describe, expect, it } from "vitest";

import type { ProviderStatus } from "./collabProvider";
import { collabSaveState } from "./collabStatus";

// Every status the provider can be in. Keep this list complete: it is what
// makes the "no two states share a label" check below meaningful.
const ALL: ProviderStatus[] = [
  "connecting",
  "connected",
  "offline",
  "offline-permanent",
  "closed-policy",
];

describe("collabSaveState", () => {
  it("gives the terminal state its own label, not the connecting one", () => {
    // The mapping used to end in `default: return "collab-connecting"`, so a
    // newly added status silently rendered as "Connecting…" — worse than the
    // "Offline — reconnecting…" it replaced, and invisible to tsc because a
    // default makes every switch type-check.
    expect(collabSaveState("offline-permanent", true)).toBe("collab-disconnected");
    expect(collabSaveState("offline-permanent", true)).not.toBe(
      collabSaveState("connecting", true),
    );
    expect(collabSaveState("offline-permanent", true)).not.toBe(
      collabSaveState("offline", true),
    );
  });

  it("maps every writer status to a distinct state", () => {
    const mapped = ALL.map((s) => collabSaveState(s, true));
    expect(new Set(mapped).size).toBe(ALL.length);
  });

  it("a reader is read-only whatever the socket is doing", () => {
    for (const s of ALL) expect(collabSaveState(s, false)).toBe("collab-readonly");
  });

  it("a policy close is not 'Read-only' — the socket is gone", () => {
    // "Read-only" describes a live session a viewer can still read and follow.
    // A 1008/1009 close is the server refusing and the provider not retrying,
    // so a writer was told their session was fine while their keystrokes went
    // nowhere. It gets its own state rather than borrowing collab-disconnected,
    // which means OUR retries ran out.
    expect(collabSaveState("closed-policy", true)).toBe("collab-closed");
    expect(collabSaveState("closed-policy", true)).not.toBe(
      collabSaveState("offline-permanent", true),
    );
  });
});
