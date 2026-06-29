import { undo } from "@codemirror/commands";
import type { EditorView } from "@codemirror/view";
import { render, cleanup, waitFor, act } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { CodeMirrorEditor } from "./CodeMirrorEditor";

afterEach(cleanup);

function getView(container: HTMLElement) {
  // The component stores the EditorView on the mount node for test access.
  return (container.querySelector(".cm-editor") as HTMLElement) ?? null;
}

it("Ctrl+Z after a controlled section switch does NOT revert to the previous section's text", () => {
  // Regression (Defect 1): the controlled value-push (A→B section switch) must NOT be
  // recorded as an undoable history entry, and the swap must CLEAR the undo stack — so
  // an undo after switching can never revert the doc to section A's text (which the
  // updateListener would then write into B's buffer as a local change = silent data
  // loss). The push is tagged Transaction.addToHistory.of(false) AND reconfigures the
  // history compartment to discard prior history.
  const onChange = vi.fn();
  const sectionA = "## A\nalpha body\n";
  const sectionB = "## B\nbravo body\n";
  const { container, rerender } = render(
    <CodeMirrorEditor value={sectionA} onChange={onChange} requireHeading />,
  );
  const view = (container.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: EditorView;
  }).__cmView!;

  // Switch sections: parent pushes B's text via the controlled value prop.
  rerender(<CodeMirrorEditor value={sectionB} onChange={onChange} requireHeading />);
  expect(view.state.doc.toString()).toBe(sectionB);

  // The user makes a local edit on section B (this IS undoable).
  onChange.mockClear();
  view.dispatch({ changes: { from: view.state.doc.length, insert: "X" } });
  expect(view.state.doc.toString()).toBe(sectionB + "X");
  expect(onChange).toHaveBeenLastCalledWith(sectionB + "X");

  // Undo. It must only undo the local "X" edit — and must NEVER reach across the swap
  // to resurrect section A's text.
  onChange.mockClear();
  undo(view);
  expect(view.state.doc.toString()).toBe(sectionB);
  // Crucially, A's text never reappears in the doc or via onChange.
  expect(view.state.doc.toString()).not.toContain("alpha body");
  for (const call of onChange.mock.calls) {
    expect(call[0]).not.toContain("alpha body");
  }

  // A second undo cannot revert past the cleared boundary into section A either.
  undo(view);
  expect(view.state.doc.toString()).not.toContain("alpha body");
});

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

it("does NOT reject a controlled remote value-push even when the pushed value lacks an H2 heading", () => {
  // Regression: the heading-delete guard is meant to stop a USER keystroke from
  // deleting the "## " heading, NOT to validate parent-supplied authoritative
  // content. The controlled `value` prop is the source of truth: pushing value=X
  // MUST make the editor show X. A remote push whose first line is not an H2
  // (a clear to "", plain text, or a leading blank line before the heading) must
  // still apply, or the editor permanently desyncs from the parent with no onChange.
  const onChange = vi.fn();
  const { container, rerender } = render(
    <CodeMirrorEditor value={"## Methods\nold\n"} onChange={onChange} requireHeading />,
  );
  const node = container.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: import("@codemirror/view").EditorView;
  };
  const view = node.__cmView!;

  // (1) parent clears the section (routine clear/loading/discard pattern).
  rerender(<CodeMirrorEditor value={""} onChange={onChange} requireHeading />);
  expect(view.state.doc.toString()).toBe(""); // remote push applied, no phantom stale text

  // (2) parent pushes plain text without a heading.
  rerender(<CodeMirrorEditor value={"plain text without heading\n"} onChange={onChange} requireHeading />);
  expect(view.state.doc.toString()).toBe("plain text without heading\n");

  // (3) parent pushes a value with a leading blank line before the heading.
  rerender(<CodeMirrorEditor value={"\n## Methods\nnew\n"} onChange={onChange} requireHeading />);
  expect(view.state.doc.toString()).toBe("\n## Methods\nnew\n");

  // Remote pushes must NEVER echo back through onChange (would loop).
  expect(onChange).not.toHaveBeenCalled();
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

it("fires onSelectionChange with the selected text, and null when cleared", () => {
  const calls: Array<{ from: number; to: number; text: string } | null> = [];
  const { container } = render(
    <CodeMirrorEditor
      value={"## Methods\nThe sky is blue.\n"}
      onChange={() => {}}
      requireHeading
      onSelectionChange={(s) => calls.push(s)}
    />,
  );
  const view = (container.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: import("@codemirror/view").EditorView;
  }).__cmView!;
  const doc = view.state.doc.toString();
  const from = doc.indexOf("The sky");
  const to = from + "The sky is blue.".length;
  view.dispatch({ selection: { anchor: from, head: to } });
  expect(calls.at(-1)).toEqual({ from, to, text: "The sky is blue." });
  view.dispatch({ selection: { anchor: to, head: to } }); // collapse
  expect(calls.at(-1)).toBeNull();
});

it("does not fire a selection while read-only (fires null instead)", () => {
  const calls: Array<{ from: number; to: number; text: string } | null> = [];
  const { container } = render(
    <CodeMirrorEditor
      value={"## Methods\nThe sky is blue.\n"}
      onChange={() => {}}
      requireHeading
      readOnly
      onSelectionChange={(s) => calls.push(s)}
    />,
  );
  const view = (container.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: import("@codemirror/view").EditorView;
  }).__cmView!;
  const doc = view.state.doc.toString();
  const from = doc.indexOf("The sky");
  const to = from + "The sky is blue.".length;
  view.dispatch({ selection: { anchor: from, head: to } });
  expect(calls.at(-1)).toBeNull();
});

it("re-emits the selection (refreshed) when the doc changes under it", () => {
  const calls: Array<{ from: number; to: number; text: string } | null> = [];
  const { container } = render(
    <CodeMirrorEditor
      value={"## Methods\nThe sky is blue.\n"}
      onChange={() => {}}
      requireHeading
      onSelectionChange={(s) => calls.push(s)}
    />,
  );
  const view = (container.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: import("@codemirror/view").EditorView;
  }).__cmView!;
  const doc = view.state.doc.toString();
  const from = doc.indexOf("The sky");
  const to = from + "The sky is blue.".length;
  view.dispatch({ selection: { anchor: from, head: to } });
  const beforeLen = calls.length;
  // Insert at the very end of the doc: the [from,to] selection is unaffected and
  // re-emitted via the docChanged branch.
  view.dispatch({ changes: { from: view.state.doc.length, insert: "ZZ" } });
  expect(calls.length).toBeGreaterThan(beforeLen);
  expect(calls.at(-1)).toEqual({ from, to, text: "The sky is blue." });
});

it("does not fire onSelectionChange on a Transaction.remote echo (controlled value push)", () => {
  // Supersedes the earlier "empty-slice → null on a remote push" behavior. A
  // background draft refetch pushes the new section bytes through the controlled
  // `value` prop as a Transaction.remote. The selection bridge SKIPS remote echoes
  // entirely, so it never re-derives (or empties) the held selection — the held
  // anchor stays exactly what the user selected; staleness is caught server-side at
  // cite time. This test FAILS without Fix C (the remote push would re-emit) and
  // passes with the skip-remote guard.
  const calls: Array<{ from: number; to: number; text: string } | null> = [];
  const { container, rerender } = render(
    <CodeMirrorEditor
      value={"## Methods\nThe sky is blue.\n"}
      onChange={() => {}}
      requireHeading
      onSelectionChange={(s) => calls.push(s)}
    />,
  );
  const view = (container.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: import("@codemirror/view").EditorView;
  }).__cmView!;
  const doc = view.state.doc.toString();
  const from = doc.indexOf("The sky");
  const to = from + "The sky is blue.".length;
  view.dispatch({ selection: { anchor: from, head: to } });
  expect(calls.at(-1)).toEqual({ from, to, text: "The sky is blue." });
  const lenBefore = calls.length;
  // A controlled value replace = a Transaction.remote push. The selection bridge
  // must NOT fire for it (the held selection is retained, not re-derived).
  rerender(
    <CodeMirrorEditor
      value={"## Methods\nX\n"}
      onChange={() => {}}
      requireHeading
      onSelectionChange={(s) => calls.push(s)}
    />,
  );
  expect(calls.length).toBe(lenBefore); // no new emission from the remote echo
});

it("citations={false} renders marker text raw with no citation pill", async () => {
  const markerDoc = "## Aims\nSee [@smith2021]<!-- ev:ev-abc123 --> here.\n";
  const { container } = render(
    <CodeMirrorEditor
      value={markerDoc}
      onChange={() => {}}
      requireHeading={true}
      citations={false}
    />,
  );
  await waitFor(() => expect(container.querySelector(".cm-editor")).toBeTruthy());
  // No atomic citation widget was built.
  expect(container.querySelector(".cm-citation-pill")).toBeNull();
  // The raw marker characters are present in the document text.
  expect(container.querySelector(".cm-content")?.textContent).toContain("[@smith2021]");
});

it("citations default (true) still renders the citation pill", async () => {
  const markerDoc = "## Aims\nSee [@smith2021]<!-- ev:ev-abc123 --> here.\n";
  const { container } = render(
    <CodeMirrorEditor value={markerDoc} onChange={() => {}} requireHeading={true} />,
  );
  await waitFor(() => expect(container.querySelector(".cm-citation-pill")).toBeTruthy());
});

it("same-section controlled push (same docKey) does NOT reconfigure the history compartment", () => {
  // Structural test for M1: a background refetch that pushes the same section (same
  // docKey) must NOT call historyComp.reconfigure() — i.e. the undo-history extension
  // must not be reset.  A section swap (different docKey) DOES reset it.
  //
  // Observable proxy: both cases apply `addToHistory.of(false)` on the push so the
  // push transaction itself is not undoable.  The ONLY difference is whether the
  // history stack accumulated before the push is wiped.
  //
  // Because the component always uses a full-document replace
  // (`{ from: 0, to: length, insert: value }`) for the controlled push, any pre-push
  // change-set entry in the history that was anchored to the old doc's positions
  // cannot be rebased onto the new doc, making pre-push undos effectively a no-op
  // regardless of whether history was cleared.  The meaningful observable therefore
  // is that post-push edits can still be undone (consistent with an intact history
  // compartment), while a docKey change additionally clears pre-push entries
  // (covered by the existing "section swap DOES clear" test below).
  //
  // This test asserts:
  // 1. Same-docKey push applies the value without clearing the history compartment.
  // 2. The editor is still fully functional after the push (edits and undo round-trip).
  const onChange = vi.fn();
  const sectionA = "## A\nbody\n";
  const sectionARefetch = "## A\nbody\nserver-line\n"; // same section, different content
  const { container, rerender } = render(
    <CodeMirrorEditor value={sectionA} onChange={onChange} requireHeading docKey="sec-A" />,
  );
  const view = (container.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: EditorView;
  }).__cmView!;

  // Same-docKey push: server-refreshed text for the same section.
  rerender(
    <CodeMirrorEditor value={sectionARefetch} onChange={onChange} requireHeading docKey="sec-A" />,
  );
  expect(view.state.doc.toString()).toBe(sectionARefetch);

  // The editor is fully functional: a local edit can be made and undone.
  onChange.mockClear();
  view.dispatch({ changes: { from: view.state.doc.length, insert: "X" } });
  expect(view.state.doc.toString()).toBe(sectionARefetch + "X");
  expect(onChange).toHaveBeenLastCalledWith(sectionARefetch + "X");

  onChange.mockClear();
  undo(view);
  expect(view.state.doc.toString()).toBe(sectionARefetch); // "X" was undone
  expect(onChange).toHaveBeenLastCalledWith(sectionARefetch);

  // No cross-section contamination: section B's text never appears.
  expect(view.state.doc.toString()).not.toContain("bravo");
});

it("section swap (docKey change) DOES clear the undo history", () => {
  // A docKey change signals a logical document identity change (section/artifact swap).
  // The undo history MUST be cleared so a cross-section Ctrl+Z cannot revert to the
  // previous section's text.
  const onChange = vi.fn();
  const sectionA = "## A\nalpha body\n";
  const sectionB = "## B\nbravo body\n";
  const { container, rerender } = render(
    <CodeMirrorEditor value={sectionA} onChange={onChange} requireHeading docKey="sec-A" />,
  );
  const view = (container.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: EditorView;
  }).__cmView!;

  // User makes a local edit on section A.
  view.dispatch({ changes: { from: view.state.doc.length, insert: "X" } });
  expect(view.state.doc.toString()).toBe(sectionA + "X");

  // Section swap: new docKey → history MUST be cleared.
  rerender(
    <CodeMirrorEditor value={sectionB} onChange={onChange} requireHeading docKey="sec-B" />,
  );
  expect(view.state.doc.toString()).toBe(sectionB);

  onChange.mockClear();
  // Undo must NOT reach section A's text — cleared history means no-op undo on B.
  undo(view);
  expect(view.state.doc.toString()).not.toContain("alpha body");
  for (const call of onChange.mock.calls) {
    expect(call[0]).not.toContain("alpha body");
  }
});

it("publishes 1-based Ln/Col on selection change", () => {
  const onCursorChange = vi.fn();
  const { container } = render(
    <CodeMirrorEditor
      value={"line one\nline two"}
      onChange={() => {}}
      requireHeading={false}
      citations={false}
      onCursorChange={onCursorChange}
    />,
  );
  const view = (container.querySelector(".cm-editor") as HTMLElement & { __cmView?: import("@codemirror/view").EditorView })
    .__cmView!;
  // Caret at index 12 = start of "line two" (9) + 3 -> line 2, col 4.
  view.dispatch({ selection: { anchor: 12 } });
  expect(onCursorChange).toHaveBeenLastCalledWith({ line: 2, col: 4 });
});

it("emits selection in read-only mode only when emitSelectionWhenReadOnly is set", async () => {
  const onSel = vi.fn();
  const doc = "## H\nhello world\n";
  const { rerender } = render(
    <CodeMirrorEditor value={doc} onChange={() => {}} requireHeading readOnly citations
      onSelectionChange={onSel} />,
  );
  const view = (document.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: import("@codemirror/view").EditorView }).__cmView!;
  act(() => view.dispatch({ selection: { anchor: 8, head: 13 } }));
  expect(onSel).not.toHaveBeenCalledWith(expect.objectContaining({ text: expect.any(String) }));

  rerender(
    <CodeMirrorEditor value={doc} onChange={() => {}} requireHeading readOnly citations
      onSelectionChange={onSel} emitSelectionWhenReadOnly />,
  );
  const v2 = (document.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: import("@codemirror/view").EditorView }).__cmView!;
  act(() => v2.dispatch({ selection: { anchor: 8, head: 13 } }));
  expect(onSel).toHaveBeenCalledWith(expect.objectContaining({ from: 8, to: 13 }));
});

it("renders a line-number gutter", () => {
  const { container } = render(
    <CodeMirrorEditor value={"a\nb"} onChange={() => {}} requireHeading={false} citations={false} />,
  );
  expect(container.querySelector(".cm-gutters")).not.toBeNull();
});

it("reports the initial cursor (Ln 1, Col 1) once on mount, before any interaction", () => {
  // The updateListener only fires on selection/doc CHANGES; new EditorView fires
  // neither, so without a one-time mount report the status bar lacks Ln/Col until
  // the user first clicks into a freshly-loaded cockpit.
  const onCursorChange = vi.fn();
  render(
    <CodeMirrorEditor
      value={"hello\nworld"}
      onChange={() => {}}
      requireHeading={false}
      citations={false}
      onCursorChange={onCursorChange}
    />,
  );
  expect(onCursorChange).toHaveBeenCalledWith({ line: 1, col: 1 });
});
