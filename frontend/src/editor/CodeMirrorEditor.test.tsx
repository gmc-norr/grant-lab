import { render, cleanup } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { CodeMirrorEditor } from "./CodeMirrorEditor";

afterEach(cleanup);

function getView(container: HTMLElement) {
  // The component stores the EditorView on the mount node for test access.
  return (container.querySelector(".cm-editor") as HTMLElement) ?? null;
}

it("renders the value as document bytes", () => {
  const { container } = render(
    <CodeMirrorEditor value={"## Methods\nbody.\n"} onChange={() => {}} requireHeading />,
  );
  expect(getView(container)).not.toBeNull();
  // The doc text is visible in the CM content DOM.
  expect(container.querySelector(".cm-content")?.textContent).toContain("body.");
});

it("pushes an external value change into the editor (controlled)", () => {
  const { container, rerender } = render(
    <CodeMirrorEditor value={"## Methods\na\n"} onChange={() => {}} requireHeading />,
  );
  rerender(<CodeMirrorEditor value={"## Methods\nb\n"} onChange={() => {}} requireHeading />);
  expect(container.querySelector(".cm-content")?.textContent).toContain("b");
});

it("readOnly editor is not editable", () => {
  const { container } = render(
    <CodeMirrorEditor value={"## Methods\nbody.\n"} onChange={() => {}} readOnly requireHeading />,
  );
  const content = container.querySelector(".cm-content");
  expect(content?.getAttribute("contenteditable")).toBe("false");
});

it("the heading-delete guard blocks a transaction that would remove the leading ## line", async () => {
  // We exercise the guard through the exported pure helper to keep the assertion
  // deterministic (jsdom has no real key events / caret geometry).
  const { blockedByHeadingGuard } = await import("./CodeMirrorEditor");
  // Deleting the heading line entirely:
  expect(blockedByHeadingGuard("## Methods\nbody.\n", "body.\n", true)).toBe(true);
  // Editing within the body is fine:
  expect(blockedByHeadingGuard("## Methods\nbody.\n", "## Methods\nnew body.\n", true)).toBe(false);
  // Renaming the heading (still an H2) is fine:
  expect(blockedByHeadingGuard("## Methods\nbody.\n", "## Approach\nbody.\n", true)).toBe(false);
  // Preamble (requireHeading=false) is never blocked:
  expect(blockedByHeadingGuard("preamble\n", "edited\n", false)).toBe(false);
});

it("calls onChange with the new document bytes when the doc changes", () => {
  const onChange = vi.fn();
  const { container } = render(
    <CodeMirrorEditor value={"## Methods\nbody.\n"} onChange={onChange} requireHeading />,
  );
  // Simulate a programmatic user edit by dispatching on the underlying view via
  // the exported test hook.
  const node = container.querySelector(".cm-editor") as HTMLElement & { __cmView?: import("@codemirror/view").EditorView };
  node.__cmView?.dispatch({ changes: { from: node.__cmView.state.doc.length, insert: "X" } });
  expect(onChange).toHaveBeenCalledWith("## Methods\nbody.\nX");
});

it("the LIVE transactionFilter rejects a dispatch that would delete the ## heading line", () => {
  // This pins the WIRING (transactionFilter returning [] to reject the dispatch),
  // not just the blockedByHeadingGuard predicate: a regression that forgot to wire
  // the helper into the filter, or inverted the boolean, would leave the predicate
  // tests green while the live editor happily dropped the heading.
  const { container } = render(
    <CodeMirrorEditor value={"## Methods\nbody.\n"} onChange={() => {}} requireHeading />,
  );
  const node = container.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: import("@codemirror/view").EditorView;
  };
  const view = node.__cmView!;
  // Replace the WHOLE doc with body-only (no leading ## heading) — must be rejected.
  view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: "body only\n" } });
  expect(view.state.doc.toString()).toBe("## Methods\nbody.\n"); // unchanged: filter rejected it
  // A body-only edit that KEEPS the heading must apply.
  view.dispatch({ changes: { from: view.state.doc.length, insert: "more.\n" } });
  expect(view.state.doc.toString()).toBe("## Methods\nbody.\nmore.\n");
});

it("renders an atomic citation pill via the production citationPills path and keeps the marker bytes", () => {
  // The production path (citationPills ViewPlugin) is what the component mounts.
  // This proves it actually produces a pill widget under jsdom AND that the raw
  // marker bytes stay in the doc (atomicRanges blocks caret entry, never deletes).
  const doc = "## Methods\nSee [@smith2021]<!-- ev:ev-abc123def456 --> here.\n";
  const { container } = render(
    <CodeMirrorEditor value={doc} onChange={() => {}} requireHeading />,
  );
  expect(container.querySelector(".cm-citation-pill")).not.toBeNull();
  const node = container.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: import("@codemirror/view").EditorView;
  };
  expect(node.__cmView!.state.doc.toString()).toBe(doc); // marker bytes preserved
});
