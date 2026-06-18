import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { markdown } from "@codemirror/lang-markdown";
import { Compartment, EditorState, Transaction } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
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
};

// The `--cm-*` vars below are the existing call-manager (NOT CodeMirror) design
// tokens defined in src/styles/tokens.css (the "cm" prefix is "call-manager" — a
// naming collision with CodeMirror that is purely coincidental). Do NOT redefine
// them and do NOT import tokens.css here: it is loaded globally by the app entry,
// so the running editor resolves them. The vitest tests assert document state +
// contenteditable + decoration ranges, never computed colors, so theme resolution
// is not exercised under jsdom (zero-rect polyfills) — that is fine.
const cmTheme = EditorView.theme({
  "&": {
    backgroundColor: "var(--cm-surface)",
    color: "var(--cm-ink)",
    border: "1px solid var(--cm-border)",
    borderRadius: "8px",
    fontSize: "0.95rem",
  },
  ".cm-content": { caretColor: "var(--cm-primary)", fontFamily: "ui-monospace, monospace" },
  "&.cm-focused": { outline: "2px solid var(--cm-primary-border)" },
  ".cm-citation-pill": {
    backgroundColor: "var(--cm-primary-soft)",
    border: "1px solid var(--cm-primary-border)",
    borderRadius: "4px",
    padding: "0 4px",
    color: "var(--cm-primary)",
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
}: CodeMirrorEditorProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  const readOnlyComp = useRef(new Compartment());
  // Keep the latest onChange/requireHeading/onPillClick without re-creating the
  // view. The pill widget captures a STABLE wrapper at mount that reads this ref,
  // so a changed onPillClick prop takes effect on the next click with no reconfigure
  // (and so widget eq can safely ignore callback identity — see citations.ts).
  const onChangeRef = useRef(onChange);
  const requireHeadingRef = useRef(requireHeading);
  const onPillClickRef = useRef(onPillClick);
  const onSelectionChangeRef = useRef(onSelectionChange);
  onChangeRef.current = onChange;
  requireHeadingRef.current = requireHeading;
  onPillClickRef.current = onPillClick;
  onSelectionChangeRef.current = onSelectionChange;

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
    const view = new EditorView({
      state: EditorState.create({
        doc: value,
        extensions: [
          history(),
          keymap.of([...defaultKeymap, ...historyKeymap]),
          markdown(),
          citationPills((citekey, evId) => onPillClickRef.current?.(citekey, evId)),
          guard,
          cmTheme,
          EditorView.lineWrapping,
          readOnlyComp.current.of([EditorState.readOnly.of(readOnly), EditorView.editable.of(!readOnly)]),
          EditorView.updateListener.of((u) => {
            if (u.docChanged && u.transactions.some((t) => !t.annotation(Transaction.remote))) {
              onChangeRef.current(u.state.doc.toString());
            }
            if (u.selectionSet || u.docChanged) {
              const sel = u.state.selection.main;
              const text = u.state.sliceDoc(sel.from, sel.to);
              if (text && !u.state.facet(EditorState.readOnly)) {
                onSelectionChangeRef.current?.({ from: sel.from, to: sel.to, text });
              } else {
                onSelectionChangeRef.current?.(null);
              }
            }
          }),
        ],
      }),
      parent: hostRef.current,
    });
    viewRef.current = view;
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
        annotations: Transaction.remote.of(true),
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
