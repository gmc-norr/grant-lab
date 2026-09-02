// Headless command tests for the constrained table nodes — same pattern as
// schema.formatting.test.tsx (real Editor, no DOM mount).
import { Editor, type JSONContent } from "@tiptap/core";
import { afterEach, describe, expect, it } from "vitest";
import { collabExtensions } from "./schema";

let editor: Editor;
afterEach(() => editor?.destroy());
const mk = (content?: string) => {
  editor = new Editor({ extensions: collabExtensions, content });
  return editor;
};
// NOTE (deviations from the brief's literal snippet, both type-only —
// vitest's esbuild transform erases types, so the brief's snippet did run
// and pass at runtime; these are `tsc --noEmit` gate fixes, the frontend's
// only lint gate since there is no separate TS linter):
// 1. Tiptap 3.27's `Editor.getJSON()` now returns the strongly-typed
//    `DocumentType<...>` (content: `(NodeType | TextType)[]`) instead of the
//    old loosely-typed `JSONContent` recursive interface. `TextType` has no
//    `content` field, so `table?.content` etc. fail with "Property 'content'
//    does not exist on type ... TextType<...>". Fixed by widening back to
//    the exported loose `JSONContent` type at the one call site, same shape
//    the brief's assertions already assume.
// 2. `a?.b(...)!.c` is one optional-chain group in TS: the trailing `!`
//    narrows the *value* at that point but does not break the chain's
//    "possibly undefined" propagation through the rest of the unparenthesized
//    accesses that follow — `TS18048 'possibly undefined'` on the final
//    `.content` reads. Parenthesizing the `!`-asserted sub-expression forces
//    the evaluation boundary TS needs. Confirmed via isolated repro against
//    the same `JSONContent`-shaped type outside this file.
// Mechanics adapted, coverage (5 tests, same assertions) unchanged.
const json = (e: Editor) => e.getJSON() as JSONContent;

describe("strict-GFM table nodes", () => {
  it("insertTable creates rows x cols with a header row", () => {
    const e = mk();
    e.commands.insertTable({ rows: 3, cols: 3, withHeaderRow: true });
    const table = json(e).content?.find((n) => n.type === "table");
    expect(table?.content).toHaveLength(3);
    expect(table?.content?.[0].content?.every((c) => c.type === "table_header")).toBe(true);
    expect(table?.content?.[1].content?.every((c) => c.type === "table_cell")).toBe(true);
  });

  it("cells hold exactly one paragraph", () => {
    const e = mk();
    e.commands.insertTable({ rows: 2, cols: 2, withHeaderRow: true });
    const table = json(e).content?.find((n) => n.type === "table");
    expect(table, "no table node in the document").toBeDefined();
    const cell = table!.content![1].content![0];
    expect(cell.content).toHaveLength(1);
    expect(cell.content![0].type).toBe("paragraph");
  });

  it("pasted colspan/rowspan normalize to 1 (ragged rows get filled)", () => {
    const e = mk();
    e.commands.insertContent(
      '<table><tr><th colspan="2">h</th></tr><tr><td>a</td><td>b</td></tr></table>',
    );
    const table = json(e).content?.find((n) => n.type === "table");
    for (const row of table?.content ?? []) {
      for (const cell of row.content ?? []) {
        expect(cell.attrs?.colspan ?? 1).toBe(1);
        expect(cell.attrs?.rowspan ?? 1).toBe(1);
        expect(cell.attrs?.colwidth ?? null).toBeNull();
      }
    }
  });

  it("row/column commands work (addRowAfter / addColumnAfter / deleteTable)", () => {
    const e = mk();
    e.commands.insertTable({ rows: 2, cols: 2, withHeaderRow: true });
    e.commands.addRowAfter();
    expect(json(e).content?.find((n) => n.type === "table")?.content).toHaveLength(3);
    e.commands.addColumnAfter();
    const row0 = json(e).content?.find((n) => n.type === "table")?.content?.[0];
    expect(row0?.content).toHaveLength(3);
    e.commands.deleteTable();
    expect(json(e).content?.some((n) => n.type === "table")).toBe(false);
  });

  it("goToNextCell navigates cells (Tab contract)", () => {
    const e = mk();
    e.commands.insertTable({ rows: 2, cols: 2, withHeaderRow: true });
    expect(e.commands.goToNextCell()).toBe(true);
  });
});
