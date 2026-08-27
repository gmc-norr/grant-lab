// Table creation + structure controls for the collab editor (2026-07-08
// collab-tables spec §F). The menu is a conditional strip — visible only
// while the caret is inside a table — not a cursor-anchored popover: same
// appears-when-relevant contract without the @floating-ui/dom dependency
// Tiptap v3 menus require.
import { Button, Group, Paper } from "@mantine/core";
import type { Editor } from "@tiptap/core";

import { useEditorTick } from "./useEditorTick";

/** True when the selection head sits in the FIRST row of its table. */
function inHeaderRow(editor: Editor): boolean {
  const { $from } = editor.state.selection;
  for (let d = $from.depth; d > 0; d--) {
    if ($from.node(d).type.name === "table_row") {
      return $from.index(d - 1) === 0; // row index within the table
    }
  }
  return false;
}

export function InsertTableButton(props: { editor: Editor | null; canWrite: boolean }) {
  useEditorTick(props.editor);
  if (!props.editor) return null;
  const disabled = !props.canWrite || props.editor.isActive("table");
  return (
    <Button
      size="compact-xs"
      variant="default"
      data-testid="insert-table"
      disabled={disabled}
      onClick={() =>
        props.editor
          ?.chain()
          .focus()
          .insertTable({ rows: 3, cols: 3, withHeaderRow: true })
          .run()
      }
    >
      Insert table
    </Button>
  );
}

export function TableMenu(props: { editor: Editor | null; canWrite: boolean }) {
  useEditorTick(props.editor);
  const { editor } = props;
  if (!editor || !props.canWrite || !editor.isActive("table")) return null;
  const header = inHeaderRow(editor);
  const run = (fn: (c: ReturnType<Editor["chain"]>) => ReturnType<Editor["chain"]>) => () =>
    fn(editor.chain().focus()).run();
  return (
    <Paper withBorder p={4} data-testid="table-menu" role="toolbar" aria-label="Table controls">
      <Group gap={4}>
        <Button size="compact-xs" variant="subtle" data-testid="table-add-row-above" disabled={header} onClick={run((c) => c.addRowBefore())}>Row above</Button>
        <Button size="compact-xs" variant="subtle" data-testid="table-add-row-below" onClick={run((c) => c.addRowAfter())}>Row below</Button>
        <Button size="compact-xs" variant="subtle" data-testid="table-add-col-left" onClick={run((c) => c.addColumnBefore())}>Col left</Button>
        <Button size="compact-xs" variant="subtle" data-testid="table-add-col-right" onClick={run((c) => c.addColumnAfter())}>Col right</Button>
        <Button size="compact-xs" variant="subtle" color="red" data-testid="table-delete-row" disabled={header} onClick={run((c) => c.deleteRow())}>Delete row</Button>
        <Button size="compact-xs" variant="subtle" color="red" data-testid="table-delete-col" onClick={run((c) => c.deleteColumn())}>Delete col</Button>
        <Button size="compact-xs" variant="subtle" color="red" data-testid="table-delete-table" onClick={run((c) => c.deleteTable())}>Delete table</Button>
      </Group>
    </Paper>
  );
}
