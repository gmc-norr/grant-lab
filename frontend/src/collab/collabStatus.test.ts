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
});
