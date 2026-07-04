import { NodeViewWrapper, ReactNodeViewRenderer, type ReactNodeViewProps } from "@tiptap/react";
import { Citation, collabExtensions } from "./schema";

function Pill(props: ReactNodeViewProps) {
  const citekey = props.node.attrs.citekey as string | null;
  return (
    <NodeViewWrapper as="span" className="citation-pill" data-testid="citation-pill">
      [{citekey}]
    </NodeViewWrapper>
  );
}

/** The schema-level Citation from Task 2, augmented with a read-only NodeView. */
export const CitationWithView = Citation.extend({
  addNodeView: () => ReactNodeViewRenderer(Pill),
});

/**
 * Full extension list for the CollabEditor: identical schema to `collabExtensions`,
 * but the `citation` node carries a React NodeView pill instead of the bare schema node.
 * Defined here (not in schema.ts) to avoid a circular import:
 *   schema.ts → CitationNode.tsx → schema.ts  (would see Citation=undefined at init time).
 */
export const collabEditorExtensions = [
  ...collabExtensions.filter((e) => e.name !== "citation"),
  CitationWithView,
];
