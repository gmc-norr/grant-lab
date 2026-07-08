/** 3b-5d.D4 formatting floor: author bold/italic/strike + headings/lists. */
import { Editor } from "@tiptap/core";
import { describe, expect, it } from "vitest";

import { collabExtensions } from "./schema";

function makeEditor(content = "<p></p>") {
  return new Editor({ extensions: collabExtensions, content });
}

function pressMod(editor: Editor, key: string, shift = false) {
  editor.view.dom.dispatchEvent(
    new KeyboardEvent("keydown", { key, ctrlKey: true, shiftKey: shift, bubbles: true })
  );
}

describe("mark shortcuts", () => {
  it("Mod-B toggles strong", () => {
    const e = makeEditor("<p>word</p>");
    e.commands.setTextSelection({ from: 1, to: 5 });
    pressMod(e, "b");
    expect(e.getHTML()).toContain("<strong>word</strong>");
  });
  it("Mod-I toggles em", () => {
    const e = makeEditor("<p>word</p>");
    e.commands.setTextSelection({ from: 1, to: 5 });
    pressMod(e, "i");
    expect(e.getHTML()).toContain("<em>word</em>");
  });
  it("Mod-Shift-S toggles strikethrough", () => {
    const e = makeEditor("<p>word</p>");
    e.commands.setTextSelection({ from: 1, to: 5 });
    pressMod(e, "s", true);
    expect(e.getHTML()).toContain("<s>word</s>");
  });
});

describe("block input rules", () => {
  // Input rules fire on handleTextInput — simulate typing the trigger char at
  // the end of the prefix (the established Tiptap testing technique).
  //
  // NOTE (deviation from the brief's literal snippet): the brief's `type()`
  // read `e.view.someProps.handleTextInput?.(...)` — but prosemirror-view's
  // EditorView has no `someProps` object; the real API is the method
  // `someProp(propName, f)` with a 5-arg `handleTextInput` signature
  // `(view, from, to, text, deflt) => boolean | void` (confirmed in
  // node_modules/.../prosemirror-view/dist/index.d.ts, mirroring the real
  // call site in EditorView's own DOM handler). `someProps` is `undefined`,
  // so `.handleTextInput` throws before any input-rule logic runs — this is a
  // bug in the brief's snippet, not a jsdom quirk. Fixed to call the real
  // `someProp` API with a `deflt` thunk (prosemirror-inputrules' own
  // handleTextInput never invokes it — it only returns whether a rule
  // matched — so it's a no-op unless no rule fires, in which case we dispatch
  // it ourselves, same as ProseMirror's own DOM input handler does).
  // Mechanics adapted, coverage (one failing-then-passing assertion per rule)
  // preserved unchanged.
  function type(e: Editor, text: string) {
    const { state } = e.view;
    const deflt = () => state.tr.insertText(text);
    const handled = e.view.someProp("handleTextInput", (f) =>
      f(e.view, state.selection.from, state.selection.to, text, deflt),
    );
    if (!handled) e.view.dispatch(deflt());
  }

  it("'## ' becomes an h2", () => {
    const e = makeEditor("<p></p>");
    e.commands.insertContent("##");
    e.commands.setTextSelection(3);
    type(e, " ");
    // Heading.renderHTML (unchanged, per the shape-freeze) merges the `level`
    // attr into the tag's HTMLAttributes, so the rendered tag is
    // `<h2 level="2">`, not a bare `<h2>` — match the open tag, not an exact
    // literal (the brief's literal `toContain("<h2>")` assumed no attrs are
    // rendered, which would require changing renderHTML — out of scope here).
    expect(e.getHTML()).toMatch(/<h2[ >]/);
  });
  it("'- ' becomes a bullet list", () => {
    const e = makeEditor("<p></p>");
    e.commands.insertContent("-");
    e.commands.setTextSelection(2);
    type(e, " ");
    expect(e.getHTML()).toContain("<ul>");
  });
  it("'1. ' becomes an ordered list", () => {
    const e = makeEditor("<p></p>");
    e.commands.insertContent("1.");
    e.commands.setTextSelection(3);
    type(e, " ");
    // Same rationale as the h2 case: OrderedList.renderHTML renders the
    // `order` attr, so the tag is `<ol order="1">`.
    expect(e.getHTML()).toMatch(/<ol[ >]/);
  });
});
