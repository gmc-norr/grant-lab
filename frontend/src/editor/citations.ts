import { RangeSet, type Range } from "@codemirror/state";
import {
  Decoration,
  type DecorationSet,
  EditorView,
  ViewPlugin,
  type ViewUpdate,
  WidgetType,
} from "@codemirror/view";

// Ported VERBATIM from the backend MARKER_RE
// (src/grant_lab/proposal_assist/citations/markers.py:23). A full marker is
// `[@<citekey>]<!-- ev:ev-<>=6 hex> -->`. The `g` flag is required for the
// scanning loop; named groups mirror the Python ones. A hand-typed BARE `[@key]`
// (no ev comment) is intentionally NOT matched, so it stays plain editable text.
export const MARKER_RE =
  /\[@(?<citekey>[A-Za-z0-9_-]+)\]<!-- ev:(?<ev_id>ev-[0-9a-f]{6,}) -->/g;

export type MarkerRange = { from: number; to: number; citekey: string; evId: string };

/** All full-marker spans in `text`, in document order. Pure; no editor state. */
export function markerRanges(text: string): MarkerRange[] {
  const out: MarkerRange[] = [];
  const re = new RegExp(MARKER_RE); // fresh lastIndex per call
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    out.push({
      from: m.index,
      to: m.index + m[0].length,
      citekey: m.groups!.citekey,
      evId: m.groups!.ev_id,
    });
  }
  return out;
}

/** The atomic, non-typeable citation pill. Renders the citekey; the ev comment
 *  stays in the underlying doc bytes (this widget only REPLACES the rendering).
 *  `onClick` is wired FOR SP2a-4 select-to-cite — stubbed to no-op here. */
export class CitationWidget extends WidgetType {
  constructor(
    readonly citekey: string,
    readonly evId: string,
    readonly onClick: (citekey: string, evId: string) => void,
  ) {
    super();
  }

  // Two widgets are equal (CM6 reuses the DOM) iff their identity fields match.
  // We deliberately do NOT compare onClick: the live component (Task 6) always
  // passes ONE stable wrapper `(c, e) => onPillClickRef.current?.(c, e)` whose
  // function identity never changes, and the *current* callback is read through a
  // ref at click time. So a changed onPillClick prop need NOT rebuild the pill DOM
  // — comparing only citekey/evId is correct and avoids needless widget churn.
  eq(other: CitationWidget): boolean {
    return other.citekey === this.citekey && other.evId === this.evId;
  }

  toDOM(): HTMLElement {
    const el = document.createElement("span");
    el.className = "cm-citation-pill";
    el.dataset.evId = this.evId;
    el.textContent = `@${this.citekey}`;
    el.setAttribute("role", "button");
    el.setAttribute("aria-label", `citation ${this.citekey}`);
    el.addEventListener("mousedown", (e) => {
      // mousedown (not click) so CM6 doesn't move the caret into the atomic range first.
      e.preventDefault();
      this.onClick(this.citekey, this.evId);
    });
    return el;
  }

  // Return false so the editor handles pointer events normally; caret entry is
  // blocked by atomicRanges and the toDOM mousedown preventDefault, not by this hook.
  ignoreEvent(): boolean {
    return false;
  }
}

/** Factory: a ViewPlugin that maintains the pill DecorationSet AND registers the
 *  same set as atomicRanges (cursor can't enter; partial edits can't split it).
 *  `onPillClick` defaults to a no-op (SP2a-4 wires real select-to-cite). Builds
 *  from the shared `decoSet` helper (declared below; hoisted) so its placement is
 *  byte-identical to `citationField`. */
export function citationPills(
  onPillClick: (citekey: string, evId: string) => void = () => {},
) {
  return ViewPlugin.fromClass(
    class {
      decorations: DecorationSet;
      constructor(view: EditorView) {
        this.decorations = decoSet(view.state.doc.toString(), onPillClick);
      }
      update(u: ViewUpdate) {
        if (u.docChanged || u.viewportChanged) {
          this.decorations = decoSet(u.view.state.doc.toString(), onPillClick);
        }
      }
    },
    {
      decorations: (v) => v.decorations,
      // Make the same spans atomic so the caret skips them and a partial keystroke
      // can't split a marker (the bytes are preserved; only caret entry is blocked).
      provide: (plugin) =>
        EditorView.atomicRanges.of((view) => view.plugin(plugin)?.decorations ?? RangeSet.empty),
    },
  );
}

/** Build the replace-decoration set over an arbitrary `doc` string. The single
 *  source of truth for pill placement — both the ViewPlugin (`citationPills`) and
 *  the StateField (`citationField`) call this, so their spans are byte-identical. */
function decoSet(
  doc: string,
  onClick: (citekey: string, evId: string) => void,
): DecorationSet {
  const ranges: Range<Decoration>[] = [];
  for (const r of markerRanges(doc)) {
    ranges.push(
      Decoration.replace({
        widget: new CitationWidget(r.citekey, r.evId, onClick),
        inclusive: false,
      }).range(r.from, r.to),
    );
  }
  return RangeSet.of(ranges, /* sort */ true);
}

/** A StateField mirror of the pill decorations used by the unit tests so they can
 *  read the DecorationSet synchronously without a live viewport. It provides BOTH
 *  `decorations` AND `atomicRanges` so a test mounting ONLY `citationField` gets
 *  full atomic-pill behavior (caret skips the marker) deterministically — it does
 *  NOT rely on any undocumented default atomicity of a replace widget. The live
 *  component (Task 6) uses `citationPills()` (the ViewPlugin) instead, which also
 *  provides `atomicRanges` and rebuilds on viewport changes for large docs. */
import { StateField } from "@codemirror/state";

export const citationField = StateField.define<DecorationSet>({
  create: (state) => decoSet(state.doc.toString(), () => {}),
  update: (value, tr) => (tr.docChanged ? decoSet(tr.state.doc.toString(), () => {}) : value),
  provide: (f) => [
    EditorView.decorations.from(f),
    EditorView.atomicRanges.of((view) => view.state.field(f, false) ?? RangeSet.empty),
  ],
});
