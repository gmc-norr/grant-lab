import { EditorSelection, EditorState } from "@codemirror/state";
import { Decoration, EditorView } from "@codemirror/view";
import { afterEach, describe, expect, it } from "vitest";

import { citationField, MARKER_RE, markerRanges } from "./citations";

let view: EditorView | undefined;

afterEach(() => {
  view?.destroy();
  view = undefined;
});

function mount(doc: string): EditorView {
  const parent = document.createElement("div");
  document.body.appendChild(parent);
  view = new EditorView({ state: EditorState.create({ doc, extensions: [citationField] }), parent });
  return view;
}

const FULL = "See [@smith2021]<!-- ev:ev-abc123 --> here.\n";
const BARE = "See [@smith2021] here.\n";

describe("MARKER_RE", () => {
  it("matches a full marker and captures citekey + ev_id", () => {
    const m = new RegExp(MARKER_RE).exec(FULL);
    expect(m).not.toBeNull();
    expect(m!.groups!.citekey).toBe("smith2021");
    expect(m!.groups!.ev_id).toBe("ev-abc123");
  });
  it("matches a PRODUCTION-LENGTH 12-hex ev id (new_ev_id = ev-<uuid.hex[:12]>)", () => {
    // Real ledger ev ids are 12 hex; the {6,} floor must still accept them.
    const m = new RegExp(MARKER_RE).exec("x [@a]<!-- ev:ev-abc123def456 --> y");
    expect(m).not.toBeNull();
    expect(m!.groups!.ev_id).toBe("ev-abc123def456");
  });
  it("does NOT match an ev id shorter than the 6-hex floor", () => {
    // 5 hex must be rejected so the {6,} quantifier can't silently drift.
    expect(new RegExp(MARKER_RE).exec("x [@a]<!-- ev:ev-abc12 --> y")).toBeNull();
  });
  it("does NOT match a bare [@key] with no ev comment", () => {
    expect(new RegExp(MARKER_RE).exec(BARE)).toBeNull();
  });
});

describe("markerRanges", () => {
  it("returns the [start,end) byte span of each full marker", () => {
    const ranges = markerRanges(FULL);
    expect(ranges).toEqual([{ from: 4, to: 4 + "[@smith2021]<!-- ev:ev-abc123 -->".length, citekey: "smith2021", evId: "ev-abc123" }]);
  });
  it("ignores bare markers", () => {
    expect(markerRanges(BARE)).toEqual([]);
  });
});

describe("citationField decorations", () => {
  it("places exactly one decoration over the full marker range", () => {
    const v = mount(FULL);
    const decos = v.state.field(citationField);
    const spans: Array<[number, number]> = [];
    decos.between(0, v.state.doc.length, (from, to) => {
      spans.push([from, to]);
    });
    const start = FULL.indexOf("[@");
    const end = start + "[@smith2021]<!-- ev:ev-abc123 -->".length;
    expect(spans).toEqual([[start, end]]);
  });

  it("the marker bytes remain in the document (round-trip preserved)", () => {
    const v = mount(FULL);
    expect(v.state.doc.toString()).toBe(FULL);
  });

  it("does NOT decorate a bare [@key]", () => {
    const v = mount(BARE);
    const decos = v.state.field(citationField);
    let count = 0;
    decos.between(0, v.state.doc.length, () => {
      count += 1;
    });
    expect(count).toBe(0);
  });

  it("the marker range is atomic: moving right from just before the pill jumps PAST it", () => {
    const v = mount(FULL);
    const start = FULL.indexOf("[@");
    const end = start + "[@smith2021]<!-- ev:ev-abc123 -->".length;
    // Put the cursor immediately before the marker, then move right one char.
    v.dispatch({ selection: EditorSelection.cursor(start) });
    const moved = v.moveByChar(v.state.selection.main, true); // forward
    expect(moved.head).toBe(end); // skipped the whole atomic marker, not start+1
  });
});

// Sanity: Decoration.replace is the mechanism we rely on (atomic pill widget).
it("Decoration is importable (guards against an API drift in @codemirror/view)", () => {
  expect(typeof Decoration.replace).toBe("function");
});
