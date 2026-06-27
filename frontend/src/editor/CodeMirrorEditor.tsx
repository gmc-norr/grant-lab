import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { markdown } from "@codemirror/lang-markdown";
import { Compartment, EditorState, Transaction } from "@codemirror/state";
import { EditorView, keymap, lineNumbers } from "@codemirror/view";
import { tags as t } from "@lezer/highlight";
import { vscodeDarkInit } from "@uiw/codemirror-theme-vscode";
import { useEffect, useRef } from "react";

import { citationPills } from "./citations";

// An H2 boundary line, mirroring the backend sectionizer `_H2_RE`
// (src/call_manager/proposal_assist/editing/sectionizer.py:20): "##" not followed
// by "#". Used by the heading-delete client guard.
const H2_LINE_RE = /^##(?!#)/;

/** Pure predicate (also unit-tested directly): would replacing `prev` with `next`
 *  delete a non-preamble section's leading "## " heading line? `requireHeading`
 *  is true for every section EXCEPT the preamble (index 0). Returning true means
 *  the transaction must be REJECTED. Mirrors save_section's SectionStructureError
 *  (save.py:196-208) for the NON-preamble case.
 *
 *  Intentional asymmetry: save_section ALSO 400s the REVERSE for the preamble
 *  (index 0) — a non-empty preamble whose first line IS an H2 (save.py:204-208).
 *  This client guard does NOT block that case (it returns false whenever
 *  requireHeading is false), so typing "## " as the first line of the preamble is
 *  left to the server's authoritative 400. That is a deliberate guard relaxation,
 *  not a correctness gap: the server is the source of truth and byte-stability is
 *  unaffected (no save happens). We do not mirror it client-side to keep the
 *  preamble freely editable until the user actually attempts to save. */
export function blockedByHeadingGuard(_prev: string, next: string, requireHeading: boolean): boolean {
  if (!requireHeading) return false;
  const firstLine = next.split("\n", 1)[0] ?? "";
  return !H2_LINE_RE.test(firstLine);
}

export type CodeMirrorEditorProps = {
  value: string;
  onChange: (next: string) => void;
  /** true for a non-preamble section: the leading "## " heading may not be deleted. */
  requireHeading: boolean;
  readOnly?: boolean;
  /** Wired FOR SP2a-4 select-to-cite; defaults to a no-op. */
  onPillClick?: (citekey: string, evId: string) => void;
  /** SP2a-4b: the current non-empty editor selection (or null when collapsed). */
  onSelectionChange?: (sel: { from: number; to: number; text: string } | null) => void;
  /** Reports the 1-based caret Ln/Col (or null) for the status bar. Default no-op. */
  onCursorChange?: (pos: { line: number; col: number } | null) => void;
  /** Citation layer (pills + select-to-cite bridge). Default true preserves the
   *  draft editor; the Spec passes false for raw, citation-free text. Read once
   *  at mount — stable per editor instance. */
  citations?: boolean;
};

// VS Code dark editor recolored to our identity. The vscode base + gutter use
// literal hex (the @uiw theme factory parses colors, so CSS vars can't be passed
// there); they equal our --app-* tokens (#1a1b1e=--app-editor, #2dd4bf=accent-bright,
// #0d3b38=selected). A teal HighlightStyle recolors markdown headings/links, and a
// thin chrome override (token-based) styles the focus ring + citation pill. jsdom
// never resolves these colors (zero rects), so the vitest suite is unaffected — the
// look is screenshot-verified.
const vscodeBase = vscodeDarkInit({
  settings: {
    background: "#1a1b1e",
    gutterBackground: "#1a1b1e",
    caret: "#2dd4bf",
    selection: "#0d3b38",
    selectionMatch: "#0d3b38",
    lineHighlight: "#ffffff0a",
  },
});
const tealHighlight = syntaxHighlighting(
  HighlightStyle.define([
    { tag: [t.heading, t.heading1, t.heading2, t.heading3], color: "#4dc4d9", fontWeight: "600" },
    { tag: [t.link, t.url], color: "#2dd4bf" },
  ]),
);
const cmChrome = EditorView.theme({
  "&": { borderRadius: "6px", fontSize: "0.95rem" },
  ".cm-content": { fontFamily: "ui-monospace, monospace" },
  "&.cm-focused": { outline: "2px solid var(--app-accent)" },
  ".cm-citation-pill": {
    backgroundColor: "var(--app-selected)",
    border: "1px solid var(--app-border-strong)",
    borderRadius: "4px",
    padding: "0 4px",
    color: "var(--app-link)",
    cursor: "pointer",
    whiteSpace: "nowrap",
  },
});

export function CodeMirrorEditor({
  value,
  onChange,
  requireHeading,
  readOnly = false,
  onPillClick = () => {},
  onSelectionChange = () => {},
  onCursorChange = () => {},
  citations = true,
}: CodeMirrorEditorProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  const readOnlyComp = useRef(new Compartment());
  // history() lives in its own compartment so a controlled full-doc swap (a section
  // switch) can RESET the undo stack — reconfiguring a compartment discards the
  // extension's prior state, so a cross-section undo can never reach into the
  // previous section's text.
  const historyComp = useRef(new Compartment());
  // Keep the latest onChange/requireHeading/onPillClick without re-creating the
  // view. The pill widget captures a STABLE wrapper at mount that reads this ref,
  // so a changed onPillClick prop takes effect on the next click with no reconfigure
  // (and so widget eq can safely ignore callback identity — see citations.ts).
  const onChangeRef = useRef(onChange);
  const requireHeadingRef = useRef(requireHeading);
  const onPillClickRef = useRef(onPillClick);
  const onSelectionChangeRef = useRef(onSelectionChange);
  const onCursorChangeRef = useRef(onCursorChange);
  onChangeRef.current = onChange;
  requireHeadingRef.current = requireHeading;
  onPillClickRef.current = onPillClick;
  onSelectionChangeRef.current = onSelectionChange;
  onCursorChangeRef.current = onCursorChange;

  // Mount ONCE: build EditorView, destroy on unmount.
  useEffect(() => {
    if (!hostRef.current) return;
    const guard = EditorState.transactionFilter.of((tr) => {
      if (!tr.docChanged) return tr;
      // The controlled remote value-push (see the value effect below) is the
      // authoritative source of truth — never reject it, or the editor would
      // permanently desync from the parent's `value` with no reconciliation path
      // (and no onChange, since rejected transactions don't fire docChanged). The
      // guard only blocks USER keystrokes from deleting the leading "## " heading.
      if (tr.annotation(Transaction.remote)) return tr;
      const prev = tr.startState.doc.toString();
      const next = tr.newDoc.toString();
      if (blockedByHeadingGuard(prev, next, requireHeadingRef.current)) {
        return []; // reject the transaction; the heading line must survive
      }
      return tr;
    });
    const extensions = [
      historyComp.current.of(history()),
      keymap.of([...defaultKeymap, ...historyKeymap]),
      markdown(),
    ];
    if (citations) {
      extensions.push(
        citationPills((citekey, evId) => onPillClickRef.current?.(citekey, evId)),
      );
    }
    extensions.push(
      guard,
      vscodeBase,
      tealHighlight,
      cmChrome,
      lineNumbers(),
      EditorView.lineWrapping,
      readOnlyComp.current.of([
        EditorState.readOnly.of(readOnly),
        EditorView.editable.of(!readOnly),
      ]),
      EditorView.updateListener.of((u) => {
        if (u.selectionSet || u.docChanged) {
          const head = u.state.selection.main.head;
          const line = u.state.doc.lineAt(head);
          onCursorChangeRef.current?.({ line: line.number, col: head - line.from + 1 });
        }
        const localDocChange =
          u.docChanged && u.transactions.some((t) => !t.annotation(Transaction.remote));
        if (localDocChange) {
          onChangeRef.current(u.state.doc.toString());
        }
        // Selection bridge: only when the citation layer is on (select-to-cite is
        // draft-only). Skip Transaction.remote echoes so a controlled value push
        // never rewrites the held selection.
        if (citations) {
          const remoteEcho = u.docChanged && !localDocChange;
          if ((u.selectionSet || u.docChanged) && !remoteEcho) {
            const sel = u.state.selection.main;
            const text = u.state.sliceDoc(sel.from, sel.to);
            if (text && !u.state.facet(EditorState.readOnly)) {
              onSelectionChangeRef.current?.({ from: sel.from, to: sel.to, text });
            } else {
              onSelectionChangeRef.current?.(null);
            }
          }
        }
      }),
    );
    const view = new EditorView({
      state: EditorState.create({ doc: value, extensions }),
      parent: hostRef.current,
    });
    viewRef.current = view;
    // Report the initial caret (Ln 1, Col 1) ONCE on mount: the updateListener above
    // only fires on selection/doc CHANGES, and `new EditorView` triggers neither, so
    // without this the status bar would lack Ln/Col until the user first interacts with
    // a freshly-loaded cockpit.
    {
      const head0 = view.state.selection.main.head;
      const line0 = view.state.doc.lineAt(head0);
      onCursorChangeRef.current?.({ line: line0.number, col: head0 - line0.from + 1 });
    }
    // Expose the view for tests ONLY. Gated behind a non-production env so the
    // production bundle never leaks a fully-mutable EditorView handle onto the DOM.
    // vitest runs with MODE === "test" (and DEV === true); the vite dev server has
    // DEV === true; a production `vite build` has neither, so this is dropped.
    if (import.meta.env.MODE === "test" || import.meta.env.DEV) {
      const cmRoot = hostRef.current.querySelector(".cm-editor") as
        | (HTMLElement & { __cmView?: EditorView })
        | null;
      if (cmRoot) cmRoot.__cmView = view;
    }
    return () => {
      view.destroy();
      viewRef.current = null;
    };
    // value/readOnly handled by the dedicated effects below. onPillClick needs NO
    // effect: the mounted citationPills wrapper reads onPillClickRef.current, so the
    // latest prop is always invoked on click without reconfiguring the view.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Controlled external value: push ONLY when it diverges from the live doc, and
  // mark the transaction `remote` so the updateListener does NOT echo it back via
  // onChange (which would loop).
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const current = view.state.doc.toString();
    if (value !== current) {
      view.dispatch({
        changes: { from: 0, to: current.length, insert: value },
        // remote: don't echo back through onChange. addToHistory(false): the swap is
        // NOT a user edit, so it must not be recorded as undoable — otherwise an undo
        // after a section switch (A→B) would revert the doc to A's text as a LOCAL
        // change, which the updateListener would write into B's buffer (silent data
        // loss). Reconfiguring the history compartment additionally CLEARS the undo
        // stack so a cross-section undo can't reach into the prior doc at all.
        annotations: [Transaction.remote.of(true), Transaction.addToHistory.of(false)],
        effects: historyComp.current.reconfigure(history()),
      });
    }
  }, [value]);

  // Flip read-only without rebuilding the view.
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({
      effects: readOnlyComp.current.reconfigure([
        EditorState.readOnly.of(readOnly),
        EditorView.editable.of(!readOnly),
      ]),
    });
  }, [readOnly]);

  return <div ref={hostRef} className="cm-editor-host" />;
}
