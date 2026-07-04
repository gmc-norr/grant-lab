import { getSchema } from "@tiptap/core";
import { describe, expect, it } from "vitest";
// The canonical twin (raw ProseMirror schema) — reuse it directly as truth.
import { schema as canonical } from "../../scripts/collab-schema.mjs";
// Guard the extension list the EDITOR actually mounts (collabEditorExtensions =
// collabExtensions with the Citation NodeView), not the bare collabExtensions —
// so a future schema-affecting Citation override can't slip past the drift guard.
import { collabEditorExtensions } from "./CitationNode";

const tiptap = getSchema(collabEditorExtensions);

// attrs are compared by their effective default via `v.default ?? null`. This
// intentionally does NOT distinguish a REQUIRED attr (no default) from a
// nullable one (default: null), because Tiptap cannot express a required attr —
// getSchema always injects `default: null`. So citation.citekey/ev_id,
// image.src and link.href (required in collab-schema.mjs) read as nullable here.
// See the note in schema.ts. Everything else IS avoidable divergence and is
// checked: name sets, content, group, inline/atom, plus marks/defining/code/
// selectable/draggable (nodes) and inclusive/code (marks) — e.g. code_block's
// marks:"" (dropping it would let marks into a code block the server/pycrdt
// schema forbids, desyncing the bridge) and link's inclusive:false.
function nodeShape(s: any, name: string) {
  const n = s.nodes[name].spec;
  return {
    content: n.content ?? null,
    group: n.group ?? null,
    inline: !!n.inline,
    atom: !!n.atom,
    marks: n.marks ?? null,
    defining: !!n.defining,
    code: !!n.code,
    selectable: n.selectable ?? null,
    draggable: n.draggable ?? null,
    attrs: Object.fromEntries(Object.entries(n.attrs ?? {}).map(([k, v]: any) => [k, v.default ?? null])),
  };
}
function markShape(s: any, name: string) {
  const m = s.marks[name].spec;
  return {
    inclusive: m.inclusive ?? null,
    code: !!m.code,
    attrs: Object.fromEntries(Object.entries(m.attrs ?? {}).map(([k, v]: any) => [k, v.default ?? null])),
  };
}

describe("Tiptap schema ≡ collab-schema.mjs", () => {
  it("has the same node names", () => {
    expect(Object.keys(tiptap.nodes).sort()).toEqual(Object.keys(canonical.nodes).sort());
  });
  it("has the same mark names", () => {
    expect(Object.keys(tiptap.marks).sort()).toEqual(Object.keys(canonical.marks).sort());
  });
  it.each(Object.keys((canonical as any).nodes))("node %s matches structurally", (name) => {
    expect(nodeShape(tiptap, name)).toEqual(nodeShape(canonical, name));
  });
  it.each(Object.keys((canonical as any).marks))("mark %s matches structurally", (name) => {
    expect(markShape(tiptap, name)).toEqual(markShape(canonical, name));
  });
});
