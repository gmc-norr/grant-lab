// frontend/src/collab/selectionAnchor.ts
import type { EditorState } from "@tiptap/pm/state";

export interface ClaimAnchor {
  sectionIndex: number;
  anchor: string;
  singleSection: boolean;
  headingOnly: boolean;
}

/** Index of the H2 "section" (0 = preamble before the first H2) that contains the
 * given top-level position, matching Python split_sections' convention. */
function sectionIndexAt(state: EditorState, pos: number): number {
  const doc = state.doc;
  const topIndex = doc.resolve(pos).index(0); // which top-level child contains pos
  let count = 0;
  for (let i = 0; i < topIndex; i++) {
    const child = doc.child(i);
    if (child.type.name === "heading" && child.attrs.level === 2) count += 1;
  }
  return count;
}

export function deriveClaimAnchor(state: EditorState): ClaimAnchor | null {
  const { from, to, empty } = state.selection;
  if (empty) return null;
  const anchor = state.doc.textBetween(from, to, "\n\n", "");
  if (!anchor) return null;
  const secFrom = sectionIndexAt(state, from);
  const secTo = sectionIndexAt(state, Math.max(from, to - 1));
  // heading-only: no non-heading textblock with real text overlaps the range.
  let headingOnly = true;
  state.doc.nodesBetween(from, to, (node) => {
    if (node.isTextblock && node.type.name !== "heading" && node.textContent.trim()) {
      headingOnly = false;
    }
  });
  return { sectionIndex: secFrom, anchor, singleSection: secFrom === secTo, headingOnly };
}
