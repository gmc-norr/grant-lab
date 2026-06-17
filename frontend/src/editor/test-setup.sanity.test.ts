import { EditorState, Transaction } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { afterEach, expect, it } from "vitest";

let view: EditorView | undefined;

afterEach(() => {
  view?.destroy();
  view = undefined;
});

it("an EditorView mounts under vitest+jsdom and round-trips its doc bytes", () => {
  const parent = document.createElement("div");
  document.body.appendChild(parent);
  view = new EditorView({
    state: EditorState.create({ doc: "## Methods\nbody.\n" }),
    parent,
  });
  expect(view.state.doc.toString()).toBe("## Methods\nbody.\n");
});

it("Transaction.remote exists (the controlled-loop annotation Task 6 relies on)", () => {
  // Task 6's controlled value<->onChange loop-avoidance hinges on
  // `Transaction.remote` (an Annotation instance): the updateListener filters it
  // out of onChange, and the external-value effect tags its dispatch with it. The
  // @codemirror packages aren't installed until Task 3, so nothing statically
  // checks this symbol earlier; this guard turns the assumption into a checked
  // one (mirrors the Decoration.replace import-guard in citations.test.ts). If a
  // future @codemirror/state renamed/dropped it, this fails LOUDLY here rather
  // than as a silent onChange loop.
  expect(typeof Transaction.remote).toBe("object");
});
