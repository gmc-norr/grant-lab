import { useSyncExternalStore } from "react";

export type SaveState = "idle" | "saving" | "saved" | "dirty" | "error";
export type Cursor = { line: number; col: number };
export type EditorSnapshot = { dirty: number[]; cursor: Cursor | null; save: SaveState };

const EMPTY: EditorSnapshot = { dirty: [], cursor: null, save: "idle" };
let snapshot: EditorSnapshot = EMPTY;
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}
function sameNums(a: number[], b: number[]): boolean {
  return a.length === b.length && a.every((n, i) => n === b[i]);
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

// MUST return a stable reference while unchanged — useSyncExternalStore re-reads on
// every render and would loop on a fresh object each time.
export function getSnapshot(): EditorSnapshot {
  return snapshot;
}

/** Synchronous, non-reactive read: does the mounted editor hold unsaved buffers?
 *  Used by the unsaved-changes guard (beforeunload / router blocker / tab-switch
 *  confirm) — those need the value imperatively, outside React's render cycle. */
export function hasUnsavedEdits(): boolean {
  return snapshot.dirty.length > 0;
}

/** Guard an action (e.g. an internal artifact-tab switch) that would unmount the
 *  active editor and discard its unsaved buffers. Returns true (proceed) when the
 *  editor is clean OR the user confirms discarding; false to abort. The artifact-tab
 *  switch is NOT a router navigation, so the router blocker can't see it — this is
 *  its dedicated guard. */
export function confirmDiscardIfDirty(): boolean {
  if (!hasUnsavedEdits()) return true;
  return window.confirm(
    "You have unsaved edits in this section. Switch and discard them?",
  );
}

/** Publish the dirty section indices for the mounted artifact editor. A
 *  same-contents set does NOT emit (the buffers effect fires on every keystroke). */
export function setDirty(indices: number[]): void {
  const sorted = [...indices].sort((a, b) => a - b);
  if (sameNums(sorted, snapshot.dirty)) return;
  snapshot = { ...snapshot, dirty: sorted };
  emit();
}

export function setCursor(cursor: Cursor | null): void {
  const c = snapshot.cursor;
  if (c === cursor) return;
  if (c && cursor && c.line === cursor.line && c.col === cursor.col) return;
  snapshot = { ...snapshot, cursor };
  emit();
}

export function setSave(save: SaveState): void {
  if (snapshot.save === save) return;
  snapshot = { ...snapshot, save };
  emit();
}

/** Reset to empty — call on editor unmount so a stale cursor/save/dirty never
 *  leaks to the status bar of a non-cockpit view or the next artifact. */
export function resetEditorStore(): void {
  if (snapshot === EMPTY) return;
  snapshot = EMPTY;
  emit();
}

export function useEditorStore(): EditorSnapshot {
  return useSyncExternalStore(subscribe, getSnapshot);
}

// Selector subscriptions. setCursor/setSave spread a NEW top-level snapshot but keep
// the `dirty` array reference (setDirty only swaps it when contents change), so a
// getSnapshot that returns snapshot.dirty is referentially STABLE across caret-only
// updates — the sidebar that reads it does not re-render on every keystroke.
function getDirty(): number[] {
  return snapshot.dirty;
}
/** Subscribe to ONLY the dirty section set. Stable across cursor/save changes. */
export function useEditorDirty(): number[] {
  return useSyncExternalStore(subscribe, getDirty);
}
