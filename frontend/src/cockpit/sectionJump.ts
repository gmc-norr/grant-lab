// "Jump to section N" as an EVENT, not as state.
//
// The active section already lives in the URL (`?sec`, cockpitNav.ts) and the
// legacy ArtifactSectionEditor reads it to swap which box is editable. The
// collab editor cannot use that: the whole draft is ONE Tiptap document, so
// there is nothing to swap — it has to scroll. And scrolling is an event, not
// state: after you scroll away by hand, `?sec` still says 3, so clicking
// "Methods" again must jump you back even though nothing in the URL changed.
// A URL value cannot express "again". This tiny channel can.
//
// The request survives until an editor consumes it, so a click that also
// switches tab (the Outline is visible on the scorecard/review tabs, where it
// lists Draft sections) still lands after the editor mounts.

type Listener = (index: number) => void;

const listeners = new Set<Listener>();
let pending: number | null = null;

/** Ask the mounted artifact editor to scroll section *index* into view. */
export function requestSectionJump(index: number): void {
  pending = index;
  for (const l of listeners) l(index);
}

export function subscribeSectionJump(fn: Listener): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** Consume a request made before the editor mounted. Returns null if none. */
export function takePendingSectionJump(): number | null {
  const i = pending;
  pending = null;
  return i;
}

/** Test seam: drop any unconsumed request so cases cannot leak into each other. */
export function resetSectionJump(): void {
  pending = null;
}
