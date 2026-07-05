// frontend/src/collab/selectionAnchor.test.ts
import { describe, it, expect } from "vitest";
import { getSchema } from "@tiptap/core";
import { EditorState, TextSelection } from "@tiptap/pm/state";
import { collabEditorExtensions } from "./CitationNode";
import { deriveClaimAnchor } from "./selectionAnchor";

const schema = getSchema(collabEditorExtensions);

/** doc: preamble para, ## A, para "Alpha claim.", ## B, para "Beta claim." */
function makeDoc() {
  const h = (level: number, t: string) => schema.nodes.heading.create({ level }, schema.text(t));
  const p = (t: string) => schema.nodes.paragraph.create(null, schema.text(t));
  return schema.nodes.doc.create(null, [
    p("preamble"), h(2, "A"), p("Alpha claim."), h(2, "B"), p("Beta claim."),
  ]);
}

function selectText(doc: ReturnType<typeof makeDoc>, needle: string) {
  let found: { from: number; to: number } | null = null;
  doc.descendants((node, pos) => {
    if (found || !node.isText || !node.text) return;
    const i = node.text.indexOf(needle);
    if (i >= 0) found = { from: pos + i, to: pos + i + needle.length };
  });
  if (!found) throw new Error("needle not found");
  const { from, to } = found as { from: number; to: number };
  const state = EditorState.create({ schema, doc });
  return state.apply(state.tr.setSelection(TextSelection.create(doc, from, to)));
}

describe("deriveClaimAnchor", () => {
  it("returns section 1 anchor for a claim in the first H2 section", () => {
    const r = deriveClaimAnchor(selectText(makeDoc(), "Alpha claim."));
    expect(r).toEqual({ sectionIndex: 1, anchor: "Alpha claim.", singleSection: true, headingOnly: false });
  });
  it("returns section 2 for a claim in the second H2 section", () => {
    const r = deriveClaimAnchor(selectText(makeDoc(), "Beta claim."));
    expect(r?.sectionIndex).toBe(2);
  });
  it("flags a heading-only selection", () => {
    const r = deriveClaimAnchor(selectText(makeDoc(), "A"));
    expect(r?.headingOnly).toBe(true);
  });
  it("returns null for an empty selection", () => {
    const doc = makeDoc();
    const state = EditorState.create({ schema, doc });
    expect(deriveClaimAnchor(state)).toBeNull();
  });
});
