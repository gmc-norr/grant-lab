# Editor Toolbar — Slice 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the collab editor one toolbar carrying block type, marks, lists,
quote, link, rule and table — absorbing the buttons that today sit in two rows
across three components.

**Architecture:** A new `EditorToolbar.tsx` inside `frontend/src/collab/`,
rendered by `CollabEditor`. `CollabEditor` gains one optional `actions` prop so
each panel can put its own panel-level button (Export on draft, Check coherence
on spec) at the end of the same bar. Every control calls a **generic
`@tiptap/core` command** against the schema's own snake_case node names.

**Tech Stack:** React 19, TypeScript, Vite, Mantine 9.4.0, Tiptap 3.27.1,
Vitest 2.0.5, pnpm.

**Spec:** `docs/superpowers/specs/2026-08-26-editor-toolbar-design.md`
(Slice 2 — the floating AI popup — is NOT in this plan.)

## Global Constraints

- **The editor uses a hand-rolled ProseMirror schema, not StarterKit.**
  `toggleBulletList`, `toggleOrderedList`, `toggleBlockquote`, `toggleHeading`,
  `setParagraph` and `setHorizontalRule` **DO NOT EXIST**. This was verified by
  executing a probe against the real schema, not read from documentation. Use
  only the generic commands in the table below.
- **Node and mark names are snake_case**: `bullet_list`, `ordered_list`,
  `list_item`, `horizontal_rule`, `code_block`. Not Tiptap's camelCase defaults.
- **Do NOT run `pnpm add`.** This repo sets a 7-day `minimumReleaseAge` cooldown
  and the install will be refused even for an old version. Everything this slice
  needs is already a dependency.
- **Test command:** `pnpm vitest run <file>` from `frontend/`. Full frontend
  check before each commit: `pnpm exec tsc --noEmit` and `pnpm vitest run`.
- **`cd frontend` does not persist between tool calls.** Use absolute paths or
  re-`cd` in every command.
- **The block-type dropdown MUST gate every option on `editor.can().setNode(...)`.**
  This prevents data loss, not just an invalid state. Measured by execution:
  inside a **list item**, `can()` returns `false` but `commands.setNode("heading",…)`
  returns **`true` and lifts the text out of the list**, destroying the `<ul><li>`
  wrapper with no error. Inside a table cell the command correctly refuses. The
  command does not protect the document; the gate does.
- Do not weaken, delete, or skip any existing test.

## The command table — measured, not assumed

Every row below was **executed** against `collabExtensions` and produced the
HTML shown. Do not substitute a different command.

| Control | Command | Resulting HTML |
|---|---|---|
| Body text | `setNode("paragraph")` | `<p>word</p>` |
| Heading 3 | `setNode("heading", { level: 3 })` | `<h3 level="3">word</h3>` |
| Bold | `toggleMark("strong")` | `<p><strong>word</strong></p>` |
| Italic | `toggleMark("em")` | `<p><em>word</em></p>` |
| Strikethrough | `toggleMark("strikethrough")` | `<p><s>word</s></p>` |
| Inline code | `toggleMark("code")` | `<p><code>word</code></p>` |
| Bullet list | `toggleList("bullet_list", "list_item")` | `<ul><li><p>word</p></li></ul>` |
| Numbered list | `toggleList("ordered_list", "list_item")` | `<ol order="1"><li><p>word</p></li></ol>` |
| Blockquote | `toggleWrap("blockquote")` | `<blockquote><p>word</p></blockquote>` |
| Link | `setMark("link", { href })` | `<p><a href="https://x.se">word</a></p>` |
| Unlink | `unsetMark("link")` | `<p>word</p>` |
| Horizontal rule | `insertContent({ type: "horizontal_rule" })` | `<hr>` |
| Insert table | existing `insertTable(...)` (from `@tiptap/extension-table`) | — |

**TWO HTML GOTCHAS THAT WILL BREAK NAIVE TEST ASSERTIONS.** The schema's
`renderHTML` spreads every attribute into the tag, so:

- a heading is `<h3 level="3">`, **never** `<h3>`;
- an ordered list is `<ol order="1">`, **never** `<ol>`.

Assert with `toContain("<h3")` plus a separate content check, or match the full
string including the attribute. An assertion of `toContain("<h3>word</h3>")`
will fail against correct code.

## File Structure

| File | Responsibility |
|---|---|
| `frontend/src/collab/useEditorTick.ts` (new) | The re-render-on-transaction hook, extracted from `TableControls.tsx` so both it and the toolbar can use it. |
| `frontend/src/collab/EditorToolbar.tsx` (new) | The toolbar: block dropdown, mark buttons, list/quote/rule buttons, the link control, and slots for `SaveVersionButton` / `InsertTableButton` / panel `actions`. |
| `frontend/src/collab/EditorToolbar.test.tsx` (new) | Its tests. |
| `frontend/src/collab/TableControls.tsx` | Loses its private `useEditorTick`, imports the shared one. Otherwise unchanged. |
| `frontend/src/collab/CollabEditor.tsx` | Renders `EditorToolbar` instead of the inline `<Group>`; gains the `actions` prop. |
| `frontend/src/cockpit/DraftPanel.tsx` | Passes its Export menu as `actions`; keeps it in a row for the legacy fallback. |
| `frontend/src/cockpit/SpecPanel.tsx` | Same, with Check coherence. |

---

### Task 1: The toolbar — block type, marks, lists, quote, rule

**Files:**
- Create: `frontend/src/collab/useEditorTick.ts`
- Create: `frontend/src/collab/EditorToolbar.tsx`
- Create: `frontend/src/collab/EditorToolbar.test.tsx`
- Modify: `frontend/src/collab/TableControls.tsx:8-20` (remove the private hook, import the shared one)
- Modify: `frontend/src/collab/CollabEditor.tsx:83-94` (render the toolbar)

**Interfaces:**
- Consumes: `collabExtensions` from `./schema`; `SaveVersionButton` from
  `./SaveVersionButton`; `InsertTableButton` from `./TableControls`.
- Produces: `useEditorTick(editor: Editor | null): void` from
  `./useEditorTick`, and

```ts
export type EditorToolbarProps = {
  editor: Editor | null;
  canWrite: boolean;
  /** Rendered at the END of the bar. Panel-level actions (Export, Check coherence). */
  actions?: React.ReactNode;
  /** Rendered just before `actions` — Save a version + Insert table. */
  builtins?: React.ReactNode;
};
export function EditorToolbar(props: EditorToolbarProps): JSX.Element | null;
```

Task 2 adds the link control to this same file. Task 3 fills `actions`.

- [ ] **Step 1: Extract the shared hook**

Create `frontend/src/collab/useEditorTick.ts`:

```ts
import type { Editor } from "@tiptap/core";
import { useEffect, useReducer } from "react";

/** Re-render on every editor transaction so isActive()/can() stay fresh. */
export function useEditorTick(editor: Editor | null) {
  const [, bump] = useReducer((n: number) => n + 1, 0);
  useEffect(() => {
    if (!editor) return;
    editor.on("transaction", bump);
    return () => {
      editor.off("transaction", bump);
    };
  }, [editor]);
}
```

In `frontend/src/collab/TableControls.tsx`, delete the local `useEditorTick`
function (lines 10-20) and its now-unused `useEffect, useReducer` import, then
add:

```ts
import { useEditorTick } from "./useEditorTick";
```

Leave every other line of `TableControls.tsx` alone.

- [ ] **Step 2: Write the failing tests**

Create `frontend/src/collab/EditorToolbar.test.tsx`:

```tsx
import { Editor } from "@tiptap/core";
import { MantineProvider } from "@mantine/core";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EditorToolbar } from "./EditorToolbar";
import { collabExtensions } from "./schema";

function mk(content = "<p>word</p>") {
  const e = new Editor({ extensions: collabExtensions, content });
  e.commands.setTextSelection({ from: 1, to: 5 });
  return e;
}

function mount(editor: Editor, canWrite = true, actions?: React.ReactNode) {
  return render(
    <MantineProvider>
      <EditorToolbar editor={editor} canWrite={canWrite} actions={actions} />
    </MantineProvider>
  );
}

describe("EditorToolbar — commands exist", () => {
  it("every command the toolbar calls is present on this editor", () => {
    // The load-bearing guard. The schema is hand-rolled, so StarterKit's
    // convenience commands are absent; if someone swaps a call for
    // toggleBulletList() this fails loudly instead of rendering a dead button.
    const e = mk();
    for (const name of [
      "setNode", "toggleMark", "toggleList", "toggleWrap",
      "setMark", "unsetMark", "insertContent",
    ]) {
      expect(typeof (e.commands as Record<string, unknown>)[name]).toBe("function");
    }
  });
});

describe("EditorToolbar — marks", () => {
  it("bold applies strong", () => {
    const e = mk();
    mount(e);
    fireEvent.click(screen.getByTestId("tb-strong"));
    expect(e.getHTML()).toContain("<strong>word</strong>");
  });
  it("italic applies em", () => {
    const e = mk();
    mount(e);
    fireEvent.click(screen.getByTestId("tb-em"));
    expect(e.getHTML()).toContain("<em>word</em>");
  });
  it("strikethrough applies s", () => {
    const e = mk();
    mount(e);
    fireEvent.click(screen.getByTestId("tb-strikethrough"));
    expect(e.getHTML()).toContain("<s>word</s>");
  });
  it("inline code applies code", () => {
    const e = mk();
    mount(e);
    fireEvent.click(screen.getByTestId("tb-code"));
    expect(e.getHTML()).toContain("<code>word</code>");
  });
});

describe("EditorToolbar — blocks", () => {
  it("bullet list wraps in ul/li", () => {
    const e = mk();
    mount(e);
    fireEvent.click(screen.getByTestId("tb-bullet_list"));
    expect(e.getHTML()).toContain("<ul><li><p>word</p></li></ul>");
  });
  it("numbered list wraps in ol/li", () => {
    const e = mk();
    mount(e);
    fireEvent.click(screen.getByTestId("tb-ordered_list"));
    // NOTE: the schema spreads attrs into the tag, so this is <ol order="1">,
    // never a bare <ol>. Asserting "<ol>" fails against correct code.
    expect(e.getHTML()).toContain('<ol order="1">');
    expect(e.getHTML()).toContain("<li><p>word</p></li>");
  });
  it("blockquote wraps the paragraph", () => {
    const e = mk();
    mount(e);
    fireEvent.click(screen.getByTestId("tb-blockquote"));
    expect(e.getHTML()).toContain("<blockquote><p>word</p></blockquote>");
  });
  it("horizontal rule inserts an hr", () => {
    const e = mk();
    mount(e);
    fireEvent.click(screen.getByTestId("tb-horizontal_rule"));
    expect(e.getHTML()).toContain("<hr>");
  });
});

describe("EditorToolbar — block type dropdown", () => {
  it("switching to Heading 3 converts the paragraph", () => {
    const e = mk();
    mount(e);
    fireEvent.change(screen.getByTestId("tb-blocktype"), { target: { value: "h3" } });
    // Attr leaks into the tag — assert the opening tag, not "<h3>".
    expect(e.getHTML()).toContain("<h3");
    expect(e.getHTML()).toContain("word");
  });
  it("switching to Body converts a heading back to a paragraph", () => {
    const e = mk("<h3>word</h3>");
    e.commands.setTextSelection({ from: 1, to: 5 });
    mount(e);
    fireEvent.change(screen.getByTestId("tb-blocktype"), { target: { value: "p" } });
    expect(e.getHTML()).toContain("<p>word</p>");
  });
  it("reflects the block the cursor is in", () => {
    const e = mk("<h2>word</h2>");
    e.commands.setTextSelection({ from: 1, to: 5 });
    mount(e);
    expect((screen.getByTestId("tb-blocktype") as HTMLSelectElement).value).toBe("h2");
  });
});

describe("EditorToolbar — block type is gated on can()", () => {
  it("DESTROYS NOTHING in a list item: the heading options are disabled", () => {
    // Measured: inside a list item, editor.can().setNode("heading") is false but
    // commands.setNode("heading") returns TRUE and lifts the text out of the
    // list, silently destroying the <ul><li> wrapper. The gate is the only
    // thing standing between the user and that data loss.
    const e = new Editor({ extensions: collabExtensions, content: "<ul><li><p>item</p></li></ul>" });
    e.commands.setTextSelection(4);
    mount(e);
    const select = screen.getByTestId("tb-blocktype") as HTMLSelectElement;
    const h2 = Array.from(select.options).find((o) => o.value === "h2")!;
    expect(h2.disabled).toBe(true);
    // And if it were somehow chosen, the list must survive.
    fireEvent.change(select, { target: { value: "h2" } });
    expect(e.getHTML()).toContain("<ul>");
    expect(e.getHTML()).toContain("<li>");
  });

  it("disables heading options inside a table cell", () => {
    const e = new Editor({
      extensions: collabExtensions,
      content: "<table><tr><td><p>cell</p></td></tr></table>",
    });
    e.commands.setTextSelection(4);
    mount(e);
    const select = screen.getByTestId("tb-blocktype") as HTMLSelectElement;
    expect(Array.from(select.options).find((o) => o.value === "h2")!.disabled).toBe(true);
  });

  it("leaves heading options ENABLED in an ordinary paragraph", () => {
    // Guard against 'fix' by disabling everything everywhere.
    const e = mk();
    mount(e);
    const select = screen.getByTestId("tb-blocktype") as HTMLSelectElement;
    expect(Array.from(select.options).find((o) => o.value === "h2")!.disabled).toBe(false);
  });

  it("leaves heading options ENABLED inside a blockquote", () => {
    // Measured: a heading IS valid inside a blockquote; can() returns true.
    const e = new Editor({
      extensions: collabExtensions,
      content: "<blockquote><p>quoted</p></blockquote>",
    });
    e.commands.setTextSelection(3);
    mount(e);
    const select = screen.getByTestId("tb-blocktype") as HTMLSelectElement;
    expect(Array.from(select.options).find((o) => o.value === "h2")!.disabled).toBe(false);
  });
});

describe("EditorToolbar — permissions and slots", () => {
  it("every control is disabled when canWrite is false", () => {
    const e = mk();
    mount(e, false);
    for (const id of [
      "tb-blocktype", "tb-strong", "tb-em", "tb-strikethrough", "tb-code",
      "tb-bullet_list", "tb-ordered_list", "tb-blockquote", "tb-horizontal_rule",
    ]) {
      expect(screen.getByTestId(id)).toBeDisabled();
    }
  });
  it("renders the actions slot", () => {
    const e = mk();
    mount(e, true, <button data-testid="panel-action">Export</button>);
    expect(screen.getByTestId("panel-action")).toBeInTheDocument();
  });
  it("renders nothing without an editor", () => {
    const { container } = render(
      <MantineProvider>
        <EditorToolbar editor={null} canWrite />
      </MantineProvider>
    );
    expect(container).toBeEmptyDOMElement();
  });
});
```

- [ ] **Step 3: Run to verify they fail**

Run:
```
pnpm vitest run src/collab/EditorToolbar.test.tsx
```
Expected: **FAIL** — cannot resolve `./EditorToolbar`.

If instead it reports "No test files found", the path is wrong. Fix the path;
never read that as a pass.

- [ ] **Step 4: Write the toolbar**

Create `frontend/src/collab/EditorToolbar.tsx`:

```tsx
// One toolbar for the collab editor. Every control calls a GENERIC
// @tiptap/core command against the hand-rolled schema's snake_case names —
// StarterKit's toggleBulletList/toggleBlockquote/toggleHeading DO NOT EXIST
// here. See docs/superpowers/specs/2026-08-26-editor-toolbar-design.md.
import { Button, Divider, Group, NativeSelect } from "@mantine/core";
import type { Editor } from "@tiptap/core";
import type { ReactNode } from "react";

import { useEditorTick } from "./useEditorTick";

export type EditorToolbarProps = {
  editor: Editor | null;
  canWrite: boolean;
  /** Rendered at the END of the bar. Panel-level actions (Export, Check coherence). */
  actions?: ReactNode;
  /** Rendered just before `actions` — Save a version + Insert table. */
  builtins?: ReactNode;
};

const BLOCK_OPTIONS = [
  { value: "p", label: "Body text" },
  { value: "h1", label: "Heading 1" },
  { value: "h2", label: "Heading 2" },
  { value: "h3", label: "Heading 3" },
  { value: "h4", label: "Heading 4" },
  { value: "h5", label: "Heading 5" },
  { value: "h6", label: "Heading 6" },
];

const MARKS: { name: string; label: string; title: string }[] = [
  { name: "strong", label: "B", title: "Bold (Ctrl+B)" },
  { name: "em", label: "I", title: "Italic (Ctrl+I)" },
  { name: "strikethrough", label: "S", title: "Strikethrough (Ctrl+Shift+S)" },
  { name: "code", label: "</>", title: "Inline code" },
];

function currentBlock(editor: Editor): string {
  for (const level of [1, 2, 3, 4, 5, 6]) {
    if (editor.isActive("heading", { level })) return `h${level}`;
  }
  return "p";
}

export function EditorToolbar(props: EditorToolbarProps) {
  const { editor, canWrite } = props;
  useEditorTick(editor);
  if (!editor) return null;

  // Gate EVERY option on can(). This is not politeness: measured, inside a list
  // item can() is false but commands.setNode("heading") returns TRUE and lifts
  // the text out of the list, destroying the <ul><li>. The command will not
  // protect the document — only this check does.
  const canSet = (value: string) =>
    value === "p"
      ? editor.can().setNode("paragraph")
      : editor.can().setNode("heading", { level: Number(value.slice(1)) });

  const options = BLOCK_OPTIONS.map((o) => ({ ...o, disabled: !canSet(o.value) }));

  const setBlock = (value: string) => {
    if (!canSet(value)) return; // belt and braces: the option is already disabled
    if (value === "p") {
      editor.chain().focus().setNode("paragraph").run();
      return;
    }
    const level = Number(value.slice(1));
    editor.chain().focus().setNode("heading", { level }).run();
  };

  return (
    <Group gap="xs" wrap="wrap" role="toolbar" aria-label="Formatting">
      <NativeSelect
        size="xs"
        data-testid="tb-blocktype"
        aria-label="Block type"
        disabled={!canWrite}
        value={currentBlock(editor)}
        data={options}
        onChange={(e) => setBlock(e.currentTarget.value)}
      />
      <Divider orientation="vertical" />
      {MARKS.map((m) => (
        <Button
          key={m.name}
          size="compact-xs"
          variant={editor.isActive(m.name) ? "filled" : "default"}
          data-testid={`tb-${m.name}`}
          aria-label={m.title}
          title={m.title}
          disabled={!canWrite}
          onClick={() => editor.chain().focus().toggleMark(m.name).run()}
        >
          {m.label}
        </Button>
      ))}
      <Divider orientation="vertical" />
      <Button
        size="compact-xs"
        variant={editor.isActive("bullet_list") ? "filled" : "default"}
        data-testid="tb-bullet_list"
        aria-label="Bullet list"
        title="Bullet list"
        disabled={!canWrite}
        onClick={() => editor.chain().focus().toggleList("bullet_list", "list_item").run()}
      >
        •—
      </Button>
      <Button
        size="compact-xs"
        variant={editor.isActive("ordered_list") ? "filled" : "default"}
        data-testid="tb-ordered_list"
        aria-label="Numbered list"
        title="Numbered list"
        disabled={!canWrite}
        onClick={() => editor.chain().focus().toggleList("ordered_list", "list_item").run()}
      >
        1.
      </Button>
      <Button
        size="compact-xs"
        variant={editor.isActive("blockquote") ? "filled" : "default"}
        data-testid="tb-blockquote"
        aria-label="Blockquote"
        title="Blockquote"
        disabled={!canWrite}
        onClick={() => editor.chain().focus().toggleWrap("blockquote").run()}
      >
        &rdquo;
      </Button>
      <Button
        size="compact-xs"
        variant="default"
        data-testid="tb-horizontal_rule"
        aria-label="Horizontal rule"
        title="Horizontal rule"
        disabled={!canWrite}
        onClick={() =>
          editor.chain().focus().insertContent({ type: "horizontal_rule" }).run()
        }
      >
        —
      </Button>
      {props.builtins}
      {props.actions}
    </Group>
  );
}
```

`NativeSelect` (not `Select`) is deliberate: it renders a real `<select>`, so
`fireEvent.change` in the tests drives it directly and the current block is
visible without opening anything.

- [ ] **Step 5: Run the tests**

Run:
```
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm vitest run src/collab/EditorToolbar.test.tsx
```
Expected: **all PASS**.

- [ ] **Step 6: Wire it into CollabEditor**

In `frontend/src/collab/CollabEditor.tsx`, add to the prop type:

```ts
  /** Panel-level actions rendered at the END of the toolbar. */
  actions?: React.ReactNode;
```

Add the import:

```ts
import { EditorToolbar } from "./EditorToolbar";
```

Replace the inline `<Group gap="xs">…</Group>` block (currently lines 84-94,
containing `SaveVersionButton` and `InsertTableButton`) with:

```tsx
      <EditorToolbar
        editor={editor}
        canWrite={props.canWrite}
        actions={props.actions}
        builtins={
          <>
            <SaveVersionButton
              id={props.workspaceId}
              artifact={props.artifact}
              provider={props.provider}
              canWrite={props.canWrite}
              status={props.status}
            />
            <InsertTableButton editor={editor} canWrite={props.canWrite} />
          </>
        }
      />
```

Leave `<TableMenu …>`, `<EditorContent …>` and the `TiptapSelectionToolbar`
block exactly as they are. Slice 2 moves the selection toolbar; this slice does
not touch it.

- [ ] **Step 7: Verify nothing else broke**

Run:
```
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm exec tsc --noEmit
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm vitest run
```
Expected: tsc clean; every test passes, including the pre-existing
`TableControls.test.tsx` (which now imports the extracted hook) and
`CollabEditor.test.tsx`.

- [ ] **Step 8: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/collab/useEditorTick.ts \
        frontend/src/collab/EditorToolbar.tsx \
        frontend/src/collab/EditorToolbar.test.tsx \
        frontend/src/collab/TableControls.tsx \
        frontend/src/collab/CollabEditor.tsx
git commit -m "feat(editor): one formatting toolbar above the collab editor"
```

- [ ] **Step 9: Prove the command guard bites (mutation)**

Commit first (Step 8) — this is reverted with `git checkout --`, which would
wipe uncommitted work.

Mutation: in `EditorToolbar.tsx`, replace the bullet-list handler
`editor.chain().focus().toggleList("bullet_list", "list_item").run()` with
`(editor.chain().focus() as never as { toggleBulletList: () => { run: () => void } }).toggleBulletList().run()`
— the StarterKit call that does not exist here.

Run:
```
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm vitest run src/collab/EditorToolbar.test.tsx
```
Expected: the bullet-list test **FAILS**. Record the output.

Restore:
```bash
git checkout -- frontend/src/collab/EditorToolbar.tsx
```

This is the mutation that matters most: it is the exact mistake a developer
familiar with Tiptap would make, and it must not be able to pass.

**Second mutation — remove the `can()` gate.** Change
`const options = BLOCK_OPTIONS.map((o) => ({ ...o, disabled: !canSet(o.value) }));`
to `const options = BLOCK_OPTIONS;` and delete the `if (!canSet(value)) return;`
line from `setBlock`.

Run the same command. Expected: **`DESTROYS NOTHING in a list item`** reports
FAILED, and the failure shows the `<ul><li>` gone from the HTML. That is the
data-loss path, reproduced.

Restore:
```bash
git checkout -- frontend/src/collab/EditorToolbar.tsx
```

---

### Task 2: The link control

**Files:**
- Modify: `frontend/src/collab/EditorToolbar.tsx`
- Modify: `frontend/src/collab/EditorToolbar.test.tsx`

**Interfaces:**
- Consumes: `EditorToolbarProps` and the `EditorToolbar` component from Task 1,
  unchanged. This task adds controls inside the same component; no prop changes.
- Produces: nothing new for later tasks.

Kept separate from Task 1 because it is the only control with input, validation
and a popover — a reviewer could reasonably approve Task 1 and reject this.

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/collab/EditorToolbar.test.tsx`:

```tsx
describe("EditorToolbar — link", () => {
  it("applies an https href to the selection", () => {
    const e = mk();
    mount(e);
    fireEvent.click(screen.getByTestId("tb-link"));
    fireEvent.change(screen.getByTestId("tb-link-input"), {
      target: { value: "https://vinnova.se" },
    });
    fireEvent.click(screen.getByTestId("tb-link-apply"));
    expect(e.getHTML()).toContain('<a href="https://vinnova.se">word</a>');
  });

  it("accepts mailto:", () => {
    const e = mk();
    mount(e);
    fireEvent.click(screen.getByTestId("tb-link"));
    fireEvent.change(screen.getByTestId("tb-link-input"), {
      target: { value: "mailto:a@b.se" },
    });
    fireEvent.click(screen.getByTestId("tb-link-apply"));
    expect(e.getHTML()).toContain('href="mailto:a@b.se"');
  });

  it("REFUSES a javascript: href and sets no mark", () => {
    // This one is load-bearing: the href is written verbatim into markdown and
    // persisted in git, so a javascript: URL would round-trip into the
    // rendered document.
    const e = mk();
    mount(e);
    fireEvent.click(screen.getByTestId("tb-link"));
    fireEvent.change(screen.getByTestId("tb-link-input"), {
      target: { value: "javascript:alert(1)" },
    });
    fireEvent.click(screen.getByTestId("tb-link-apply"));
    expect(e.getHTML()).not.toContain("javascript:");
    expect(e.getHTML()).not.toContain("<a ");
    expect(screen.getByTestId("tb-link-error")).toBeInTheDocument();
  });

  it("removes an existing link", () => {
    const e = mk('<p><a href="https://x.se">word</a></p>');
    e.commands.setTextSelection({ from: 1, to: 5 });
    mount(e);
    fireEvent.click(screen.getByTestId("tb-link"));
    fireEvent.click(screen.getByTestId("tb-link-remove"));
    expect(e.getHTML()).toContain("<p>word</p>");
    expect(e.getHTML()).not.toContain("<a ");
  });

  it("is disabled with an empty selection (a link mark needs a range)", () => {
    const e = new Editor({ extensions: collabExtensions, content: "<p>word</p>" });
    e.commands.setTextSelection({ from: 3, to: 3 });
    mount(e);
    expect(screen.getByTestId("tb-link")).toBeDisabled();
  });

  it("is disabled when canWrite is false", () => {
    const e = mk();
    mount(e, false);
    expect(screen.getByTestId("tb-link")).toBeDisabled();
  });
});
```

- [ ] **Step 2: Run to verify they fail**

Run:
```
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm vitest run src/collab/EditorToolbar.test.tsx
```
Expected: the six link tests **FAIL** — no element with `tb-link`. Every test
from Task 1 still PASSES.

- [ ] **Step 3: Add the link control**

In `frontend/src/collab/EditorToolbar.tsx`, extend the imports:

```tsx
import { Button, Divider, Group, NativeSelect, Popover, Text, TextInput } from "@mantine/core";
import { useState } from "react";
```

Add above the component:

```tsx
/** Only these schemes may be written into the document.
 *
 * The href goes verbatim into markdown that is committed to git and rendered
 * later, so a `javascript:` URL would survive the round trip. Allowlist, never
 * blocklist. */
const SAFE_SCHEME = /^(https?:\/\/|mailto:)/i;
```

Inside the component, above the `return`:

```tsx
  const [linkOpen, setLinkOpen] = useState(false);
  const [linkHref, setLinkHref] = useState("");
  const [linkError, setLinkError] = useState(false);
  const selectionEmpty = editor.state.selection.empty;

  const applyLink = () => {
    const href = linkHref.trim();
    if (!SAFE_SCHEME.test(href)) {
      setLinkError(true);
      return;
    }
    setLinkError(false);
    editor.chain().focus().setMark("link", { href }).run();
    setLinkOpen(false);
    setLinkHref("");
  };

  const removeLink = () => {
    editor.chain().focus().unsetMark("link").run();
    setLinkOpen(false);
    setLinkHref("");
    setLinkError(false);
  };
```

Then insert this immediately before `{props.builtins}`:

```tsx
      <Popover opened={linkOpen} onChange={setLinkOpen} position="bottom" withArrow>
        <Popover.Target>
          <Button
            size="compact-xs"
            variant={editor.isActive("link") ? "filled" : "default"}
            data-testid="tb-link"
            aria-label="Link"
            title="Link"
            disabled={!canWrite || selectionEmpty}
            onClick={() => setLinkOpen((o) => !o)}
          >
            🔗
          </Button>
        </Popover.Target>
        <Popover.Dropdown>
          <Group gap="xs" align="flex-end">
            <TextInput
              size="xs"
              label="URL"
              placeholder="https://…"
              data-testid="tb-link-input"
              value={linkHref}
              onChange={(e) => setLinkHref(e.currentTarget.value)}
            />
            <Button size="compact-xs" data-testid="tb-link-apply" onClick={applyLink}>
              Apply
            </Button>
            <Button
              size="compact-xs"
              variant="default"
              data-testid="tb-link-remove"
              onClick={removeLink}
            >
              Remove
            </Button>
          </Group>
          {linkError && (
            <Text size="xs" c="red" mt="xs" data-testid="tb-link-error">
              Only http://, https:// and mailto: links are allowed.
            </Text>
          )}
        </Popover.Dropdown>
      </Popover>
```

- [ ] **Step 4: Run the tests**

Run:
```
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm vitest run src/collab/EditorToolbar.test.tsx
```
Expected: **all PASS**, Task 1's included.

- [ ] **Step 5: Verify the whole frontend**

Run:
```
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm exec tsc --noEmit
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm vitest run
```
Expected: tsc clean; every test passes.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/collab/EditorToolbar.tsx frontend/src/collab/EditorToolbar.test.tsx
git commit -m "feat(editor): link control with a scheme allowlist"
```

- [ ] **Step 7: Prove the scheme guard bites (mutation)**

Mutation: change `SAFE_SCHEME` to `/.*/`.

Run:
```
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm vitest run src/collab/EditorToolbar.test.tsx
```
Expected: `REFUSES a javascript: href and sets no mark` reports **FAILED**.

Restore:
```bash
git checkout -- frontend/src/collab/EditorToolbar.tsx
```

---

### Task 3: Fold the panel actions into the bar

**Files:**
- Modify: `frontend/src/cockpit/DraftPanel.tsx:44-64`
- Modify: `frontend/src/cockpit/SpecPanel.tsx:82-100`
- Test: `frontend/src/cockpit/DraftPanel.test.tsx` (create if absent)

**Interfaces:**
- Consumes: `CollabEditor`'s `actions?: React.ReactNode` prop, added in Task 1.
- Produces: nothing.

**TWO TRAPS IN THIS TASK — read before editing.**

**Trap 1 — the legacy fallback.** In both panels the action row sits **outside**
the `collab_enabled ? <CollabEditor/> : <ArtifactSectionEditor/>` branch, so it
currently serves *both* editors. Moving it into `actions` without care
**silently removes Export from the legacy fallback** — a real regression that no
existing test covers, because that path is only taken when collab is off.

**Trap 2 — the connecting state.** `CollabEditor` returns a bare `Connecting…`
div until `doc`, `provider` and `me.user` all resolve
(`CollabEditor.tsx:41-47`). It never reaches `CollabEditorInner`, so **the
`actions` slot is not rendered at all while connecting** — Export would blink
out of existence on every mount. The panel cannot see that state directly, so it
must not depend on it: `actions` is a bonus placement, never the only one.

The fix for both: extract the row into a local variable and render it in **one
of two places** — inside `actions` when collab is enabled, and in its own
`<Group>` otherwise. Same node, two positions. The connecting flash is accepted
as a brief duplicate-free gap only if the panel's own row covers it; if you
cannot arrange that, keep the panel row **always** and treat `actions` as
additive. State which you chose in the task report.

**Also move the status text, not just the buttons.** The current rows render
`exportMsg` (`DraftPanel.tsx:59-63`) and the completed/failed coherence text
(`SpecPanel.tsx:90-99`). These are that action's feedback and must travel with
their button, or the user gets a button whose result appears nowhere.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/cockpit/DraftPanel.test.tsx` (or append if it exists):

```tsx
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DraftPanel } from "./DraftPanel";

// vi.mock is HOISTED to the top of the module — it cannot be called inside a
// helper function. Declare the mock once here and flip a mutable flag per test.
const state = { collab: true };

vi.mock("../api/editor", () => ({
  useDraft: () => ({
    data: {
      head: "abc123",
      can_edit: true,
      can_write: true,
      collab_enabled: state.collab,
      sections: [{ sha: "s1", text: "word", index: 0, heading: "Intro" }],
    },
    isLoading: false,
    refetch: vi.fn(),
  }),
  useSaveSection: () => ({ mutate: vi.fn(), isPending: false }),
}));

// The editor itself is not under test here — only WHERE Export renders.
vi.mock("../collab/CollabEditor", () => ({
  CollabEditor: (p: { actions?: React.ReactNode }) => <div>{p.actions}</div>,
}));
vi.mock("../editor/ArtifactSectionEditor", () => ({
  ArtifactSectionEditor: () => <div data-testid="legacy-editor" />,
}));

function mount() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MantineProvider>
        <DraftPanel id="11111111-1111-1111-1111-111111111111" />
      </MantineProvider>
    </QueryClientProvider>
  );
}

describe("DraftPanel — Export placement", () => {
  beforeEach(() => {
    state.collab = true;
  });

  it("offers Export when collab is ON (inside the toolbar)", () => {
    mount();
    expect(screen.getByText(/Export/)).toBeInTheDocument();
  });

  it("STILL offers Export when collab is OFF (legacy editor)", () => {
    // Regression guard: the action row used to live outside the collab branch
    // and served both editors. Passing it only as CollabEditor's `actions`
    // would silently drop it from the fallback.
    state.collab = false;
    mount();
    expect(screen.getByTestId("legacy-editor")).toBeInTheDocument();
    expect(screen.getByText(/Export/)).toBeInTheDocument();
  });
});
```

If the real `../api/editor` module exports more than these two hooks and
something in `DraftPanel`'s tree needs them, add the missing exports to the mock
factory — do not switch to a partial mock that re-imports the real module, since
that pulls a live query client into the test. The assertion that matters is only
"Export is present in both branches". **Do not delete the second test to make
the first pass.**

- [ ] **Step 2: Run to verify the second test fails**

Run:
```
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm vitest run src/cockpit/DraftPanel.test.tsx
```
Expected: both PASS **before** the refactor (Export is currently outside the
branch, so it shows in both). This test is a **guard against the refactor**, not
a driver for it — its job is to fail in Step 4 if you move Export carelessly.
Record that both passed; that is the baseline.

- [ ] **Step 3: Refactor DraftPanel**

In `frontend/src/cockpit/DraftPanel.tsx`, extract the export row into a
variable above the `return`:

```tsx
  const exportActions = (
    <>
      <Menu position="bottom-start">
        <Menu.Target>
          <Button variant="default" size="xs">
            Export ▾
          </Button>
        </Menu.Target>
        <Menu.Dropdown>
          <Menu.Item onClick={() => void onExport("raw")}>Markdown</Menu.Item>
          <Menu.Item onClick={() => void onExport("composed")}>
            Markdown + citations & bibliography
          </Menu.Item>
        </Menu.Dropdown>
      </Menu>
      {exportMsg && (
        <Text role="status" size="sm" c="dimmed">
          {exportMsg}
        </Text>
      )}
    </>
  );
```

Delete the `<Group gap="sm">…</Group>` that used to hold it. Then pass it to the
collab editor, and keep a row for the fallback:

```tsx
      {draft.data.collab_enabled ? (
        <CollabEditor
          key={`collab-draft:${id}`}
          workspaceId={id}
          artifact="draft"
          canWrite={draft.data.can_write}
          head={head}
          sections={sections}
          canEdit={can_edit}
          actions={exportActions}
        />
      ) : (
        <>
          <Group gap="sm">{exportActions}</Group>
          <ArtifactSectionEditor
            key={`draft:${id}`}
            workspaceId={id}
            queryKey={["draft", id]}
            head={head}
            canEdit={can_edit}
            sections={sections}
            save={save}
            refetch={draft.refetch}
            citations
            openPeek={openPeek}
          />
        </>
      )}
```

- [ ] **Step 4: Run the tests — BOTH must still pass**

Run:
```
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm vitest run src/cockpit/DraftPanel.test.tsx
```
Expected: **both PASS**. If `STILL offers Export when collab is OFF` fails, the
fallback lost its Export — fix the branch, do not change the test.

- [ ] **Step 5: Do the same for SpecPanel**

In `frontend/src/cockpit/SpecPanel.tsx`, extract the coherence row (the
`<Group gap="sm" align="center">` containing the `Check coherence` button and
its two status `<Text>` elements) into:

```tsx
  const coherenceActions = (
    <>
      <Button
        size="xs"
        disabled={checking || startCoherence.isPending}
        onClick={() => startCoherence.mutate()}
      >
        {checking ? "Checking coherence…" : "Check coherence"}
      </Button>
      {runStatus === "completed" && coherence.data && (
        <Text role="status" size="sm" c="dimmed">
          Last check: {coherence.data.n_proposals} proposal(s)
        </Text>
      )}
      {runStatus === "failed" && (
        <Text role="status" size="sm" c="red">
          {coherence.data?.error ?? "Coherence check failed."}
        </Text>
      )}
    </>
  );
```

Pass `actions={coherenceActions}` to `CollabEditor`, and wrap the
`ArtifactSectionEditor` branch in a fragment with
`<Group gap="sm" align="center">{coherenceActions}</Group>` before it — exactly
the shape used for DraftPanel in Step 3.

Leave the lock banner (`🔒 Spec locked…` and the Lock/Unlock buttons) where it
is. It is a document-state banner, not a toolbar action, and it must stay
visible in both branches on its own row.

- [ ] **Step 6: Verify the whole frontend**

Run:
```
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm exec tsc --noEmit
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm vitest run
```
Expected: tsc clean; every test passes.

- [ ] **Step 7: Build, to be sure the bundle is shippable**

Run:
```
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm build
```
Expected: exit 0. This repo serves a **built** SPA from
`src/call_manager/web/spa/`, so a type-clean tree that fails to build would ship
nothing. Do **not** run the `rsync` that deploys it — that is a separate,
deliberate step.

- [ ] **Step 8: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/cockpit/DraftPanel.tsx \
        frontend/src/cockpit/SpecPanel.tsx \
        frontend/src/cockpit/DraftPanel.test.tsx
git commit -m "feat(cockpit): fold Export and Check coherence into the editor toolbar"
```

- [ ] **Step 9: Prove the regression guard bites (mutation)**

Mutation: in `DraftPanel.tsx`, delete `<Group gap="sm">{exportActions}</Group>`
from the non-collab branch — the exact careless move this task warns about.

Run:
```
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm vitest run src/cockpit/DraftPanel.test.tsx
```
Expected: `STILL offers Export when collab is OFF` reports **FAILED**.

Restore:
```bash
git checkout -- frontend/src/cockpit/DraftPanel.tsx
```

---

## Out of scope for this plan

- **The floating AI popup** (Slice 2 of the spec). `TiptapSelectionToolbar`
  keeps rendering below `<EditorContent>`; this plan does not touch it.
- **Image insert.** The schema has an `image` node but no upload endpoint is
  wired to it.
- **Font size, family, colour, alignment.** The document is markdown in git;
  markdown has heading levels, not point sizes.
- **Deploying the bundle.** `rsync -a --delete frontend/dist/
  src/call_manager/web/spa/` is how a build reaches the server, and it is a
  deliberate separate step, not part of any task here.
