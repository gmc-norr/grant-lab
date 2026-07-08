import { Editor, type JSONContent } from "@tiptap/core";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { afterEach, describe, expect, it } from "vitest";
import { collabExtensions } from "./schema";
import { InsertTableButton, TableMenu } from "./TableControls";

let editor: Editor;
afterEach(() => editor?.destroy());
const mk = (content?: string) => {
  editor = new Editor({ extensions: collabExtensions, content });
  return editor;
};
const ui = (node: React.ReactNode) => render(<MantineProvider>{node}</MantineProvider>);
// Tiptap 3.27's Editor.getJSON() returns the strongly-typed DocumentType
// (content: (NodeType | TextType)[]) rather than the old loose recursive
// JSONContent; TextType has no `content` field, so `table?.content` fails
// tsc. Widen back to the exported loose JSONContent at the one call site —
// same fix as schema.tables.test.tsx's `json()` helper (tsc-only deviation,
// runtime shape is identical to the brief's assertions).
const json = (e: Editor) => e.getJSON() as JSONContent;

describe("InsertTableButton", () => {
  it("inserts a 3x3 table with header row", async () => {
    const e = mk();
    ui(<InsertTableButton editor={e} canWrite />);
    await userEvent.click(screen.getByTestId("insert-table"));
    const table = json(e).content?.find((n) => n.type === "table");
    expect(table?.content).toHaveLength(3);
    expect(table?.content?.[0].content).toHaveLength(3);
    expect(table?.content?.[0].content?.[0].type).toBe("table_header");
  });

  it("is disabled when read-only and when already inside a table", () => {
    const e = mk();
    e.commands.insertTable({ rows: 2, cols: 2, withHeaderRow: true });
    // caret is inside the just-inserted table
    const { rerender } = ui(<InsertTableButton editor={e} canWrite />);
    expect(screen.getByTestId("insert-table")).toBeDisabled();
    rerender(<MantineProvider><InsertTableButton editor={mk()} canWrite={false} /></MantineProvider>);
    expect(screen.getByTestId("insert-table")).toBeDisabled();
  });
});

describe("TableMenu", () => {
  it("renders nothing when the caret is outside a table", () => {
    ui(<TableMenu editor={mk("<p>x</p>")} canWrite />);
    expect(screen.queryByTestId("table-menu")).toBeNull();
  });

  it("shows controls inside a table and adds a row below", async () => {
    const e = mk();
    e.commands.insertTable({ rows: 2, cols: 2, withHeaderRow: true });
    ui(<TableMenu editor={e} canWrite />);
    expect(screen.getByTestId("table-menu")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("table-add-row-below"));
    expect(json(e).content?.find((n) => n.type === "table")?.content).toHaveLength(3);
  });

  it("disables Row above / Delete row while in the header row", () => {
    const e = mk();
    e.commands.insertTable({ rows: 2, cols: 2, withHeaderRow: true });
    // insertTable leaves the caret in the FIRST (header) cell
    ui(<TableMenu editor={e} canWrite />);
    expect(screen.getByTestId("table-add-row-above")).toBeDisabled();
    expect(screen.getByTestId("table-delete-row")).toBeDisabled();
  });

  it("delete table removes the table entirely", async () => {
    const e = mk();
    e.commands.insertTable({ rows: 2, cols: 2, withHeaderRow: true });
    ui(<TableMenu editor={e} canWrite />);
    await userEvent.click(screen.getByTestId("table-delete-table"));
    expect(json(e).content?.some((n) => n.type === "table")).toBe(false);
  });
});
