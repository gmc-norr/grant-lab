# Collab editor toolbar — design

**Date:** 2026-08-26
**Status:** approved in brainstorming, not yet planned

## Problem

The collab editor (Tiptap, live on the Draft and Spec tabs) has **no formatting
UI**. Formatting exists but is discoverable only if you already know it:

- **Block structure** works by typing markdown — `# ` … `###### ` for headings,
  `- ` / `* ` / `+ ` for bullets, `1. ` for ordered lists.
- **Inline marks** work only by keyboard: `Mod-b` (strong), `Mod-i` (em),
  `Mod-Shift-s` (strikethrough). Verified: the marks declare
  `addKeyboardShortcuts` but **zero** input rules, so typing `**bold**` does
  nothing.
- Blockquote, code, code block, link, horizontal rule and image have **neither**
  a button nor a shortcut. They are reachable only by the AI, by pasting, or by
  a document that already contained them.

A user opening the editor sees a text area and two buttons (`Save a version`,
`Insert table`), with `Export ▾` on a separate row above them.

Separately, the AI selection actions (`Ask` / `Edit` / `Cite`) render as a
**static panel after `<EditorContent>`** — i.e. below the entire document. On a
real proposal that is far below the fold, so selecting text appears to do
nothing. There is no floating or bubble positioning anywhere in `collab/`
(verified: no `BubbleMenu`, `FloatingMenu`, `useFloating`, or `@floating-ui`
import).

## Goal

One toolbar above the editor carrying block type, the standard writing marks,
lists, quote, link, rule and table — plus the buttons that are already there.
And move the AI actions to a popup beside the selection.

Explicitly **not** in scope: font size, font family, colour, alignment. The
document is markdown persisted in git; markdown has heading levels, not point
sizes. Supporting arbitrary sizes would mean changing the storage format.
Image insert is also out of scope — the schema has an `image` node but no
upload endpoint is wired to it, which is its own slice.

## The commands that actually exist — read this before implementing

The editor uses a **hand-rolled ProseMirror schema** (`frontend/src/collab/schema.ts`),
not Tiptap's StarterKit. This is the single most important constraint in this
design, because it means the familiar convenience commands **do not exist**.

Measured against `@tiptap/core@3.27.1`'s type declarations:

| Available (generic core commands) | NOT available |
|---|---|
| `toggleMark`, `setMark`, `unsetMark` | `toggleBulletList` |
| `setNode`, `toggleNode` | `toggleOrderedList` |
| `toggleList`, `liftListItem`, `sinkListItem` | `toggleBlockquote` |
| `toggleWrap`, `wrapIn` | `toggleHeading`, `setParagraph` |
| `insertContent` | `setHorizontalRule` |

A plan written against `editor.commands.toggleBulletList()` would produce
buttons that silently do nothing. Every control below is specified as the
generic command with the schema's own node/mark name.

| Control | Command | Active check |
|---|---|---|
| Body text | `setNode("paragraph")` | `isActive("paragraph")` |
| Heading 1–6 | `setNode("heading", { level: n })` | `isActive("heading", { level: n })` |
| Bold | `toggleMark("strong")` | `isActive("strong")` |
| Italic | `toggleMark("em")` | `isActive("em")` |
| Strikethrough | `toggleMark("strikethrough")` | `isActive("strikethrough")` |
| Inline code | `toggleMark("code")` | `isActive("code")` |
| Bullet list | `toggleList("bullet_list", "list_item")` | `isActive("bullet_list")` |
| Numbered list | `toggleList("ordered_list", "list_item")` | `isActive("ordered_list")` |
| Blockquote | `toggleWrap("blockquote")` | `isActive("blockquote")` |
| Link | `setMark("link", { href })` / `unsetMark("link")` | `isActive("link")` |
| Horizontal rule | `insertContent({ type: "horizontal_rule" })` | n/a |
| Insert table | existing `InsertTableButton` | `isActive("table")` |

Node and mark names are the schema's, verified in `schema.ts`: `paragraph`,
`heading` (attr `level`, default 1, parses h1–h6), `strong`, `em`,
`strikethrough`, `code`, `bullet_list`, `ordered_list`, `list_item`,
`blockquote`, `horizontal_rule`, `link` (attrs `href`, `title`), `table`.
They are snake_case, not Tiptap's camelCase defaults — another reason a
copied-in toolbar would fail.

## Architecture

### Where the toolbar lives

A new `frontend/src/collab/EditorToolbar.tsx`, rendered by `CollabEditor`.

`CollabEditor` is shared by `DraftPanel` and `SpecPanel`, so both tabs get
formatting from one implementation. That is intended: the spec is also a
markdown document the user edits.

### Consolidating the existing buttons

Today the buttons are split across two components and two rows:

| Button | Lives in | Applies to |
|---|---|---|
| `Save a version` | `CollabEditor` | both |
| `Insert table` | `CollabEditor` | both |
| `Export ▾` | `DraftPanel` (outside the editor) | draft only |
| `Check coherence` | `SpecPanel` (outside the editor) | spec only |

`Save a version` and `Insert table` move into `EditorToolbar`.

`Export` and `Check coherence` are **panel-level** actions, not editor actions,
and each exists on only one tab. Rather than teach the editor about them,
`CollabEditor` gains one new optional prop:

```ts
type CollabEditorProps = {
  workspaceId: string;
  artifact: "draft" | "spec";
  canWrite: boolean;
  /** Panel-level actions rendered at the END of the toolbar. */
  actions?: React.ReactNode;
  /** Optional props for the draft selection toolbar (draft-only) */
  head?: string;
  sections?: { sha: string; text: string }[];
  canEdit?: boolean;
};
```

`DraftPanel` passes its `Export ▾` menu as `actions`; `SpecPanel` passes its
`Check coherence` button. The editor stays ignorant of what those actions do.

**Each panel must keep its own row for the two states where the slot cannot
render** (Sol review finding 1, confirmed):

1. **The legacy fallback.** Both panels render their action row *outside* the
   `collab_enabled ? <CollabEditor/> : <ArtifactSectionEditor/>` branch, so it
   serves the legacy editor too. Passing it only as `actions` would silently
   remove Export whenever collab is off.
2. **While connecting.** `CollabEditor` returns a bare `Connecting…` div until
   `doc`, `provider` and `me.user` all resolve (`CollabEditor.tsx:41-47`) — it
   never reaches `CollabEditorInner`, so the slot is not rendered at all.
   Export would blink out of existence on every mount.

The panel therefore builds the action node **once** and renders it in one of two
places: inside `actions` when collab is enabled *and* connected, in its own
`<Group>` otherwise. Same node, two positions.

**Also move the status messages, not just the buttons** (finding 6, confirmed):
the current rows also render `exportMsg` (`DraftPanel.tsx:59-63`) and the
completed/failed coherence text (`SpecPanel.tsx:90-99`). These travel with their
button — they are that action's feedback and are useless anywhere else.

The prop is named `actions`. A reviewer suggested `toolbarActions`; `actions` is
kept because the prop already sits on a component whose only bar is the toolbar,
and the doc comment states where it renders.

### Layout

```
[ Body ▾ ] │ B  I  S  <> │  •—  1.  " │  🔗  ─  ⊞ │ Save a version   <actions>
```

One `Group` with dividers. Order is fixed; it does not reflow per artifact.
On the spec tab `<actions>` is `Check coherence`; on the draft tab it is
`Export ▾`.

### Disabled and active state

Every control is disabled when `canWrite` is false. `Insert table` keeps its
existing extra condition (also disabled inside a table).

Active state uses `editor.isActive(...)` per the table above. The toolbar must
re-render on selection and document change: reuse the existing
`useEditorTick(editor)` hook that `TableControls.tsx` already uses for exactly
this. Do not add a new subscription mechanism.

**It is currently private** — `TableControls.tsx:11` declares
`function useEditorTick` with no `export` (finding 7). Extract it to
`frontend/src/collab/useEditorTick.ts` and have both components import it,
rather than duplicating the hook or exporting it from a component module.

### The block-type dropdown

A Mantine `Select` with seven entries (not `Menu`: `Select` shows the current
value in the control itself, which is the whole point here — the user must be
able to see what block they are in without opening it):
`Body text`, `Heading 1` … `Heading 6`. Its displayed value is derived from `isActive`, checked
heading-first then falling back to `Body text`. Choosing `Body text` while in a
heading calls `setNode("paragraph")`, which converts the block back — this is
the only way to un-heading a line, since there is no markdown "unheading" you
can type.

**Every option must be gated on `editor.can().setNode(...)`, and this is not
defensive politeness — it prevents data loss.** Measured by executing
`setNode("heading", { level: 2 })` in each context:

| Context | `can()` | `commands.setNode()` | Result |
|---|---|---|---|
| paragraph | true | true | `<h2 level="2">plain</h2>` |
| inside a blockquote | true | true | `<blockquote><h2 …>quoted</h2></blockquote>` |
| table cell | false | **false** | unchanged — a safe no-op |
| **list item** | **false** | **true** | **`<h2 level="2">item</h2>` — the `<ul><li>` wrapper is GONE** |

`table_cell` and `table_header` declare `content: "paragraph"` and `list_item`
declares `content: "paragraph block*"` (`schema.ts:93`, `:116-122`), so a
heading is invalid in both. But the two behave differently and only one is
safe: in a table cell the command refuses, while **in a list item the command
returns `true` and lifts the content out of the list**, destroying the list
structure without any error.

So the command does not protect the document. The dropdown must disable a
heading option when `editor.can().setNode("heading", { level })` is false, and
disable `Body text` when `editor.can().setNode("paragraph")` is false. A test
must cover the list-item case specifically — it is the one that silently eats
structure.

### Link

Clicking `🔗` with a selection opens a small Mantine `Popover` with one text
input and Apply / Remove. Apply calls `setMark("link", { href })`, Remove calls
`unsetMark("link")`. With no selection the button is disabled — a link mark
needs a range.

The `href` is inserted verbatim into markdown. Reject anything that is not
`http://`, `https://`, or `mailto:` — a `javascript:` href would round-trip
through git into the rendered document.

## The AI selection popup

`TiptapSelectionToolbar` keeps all of its current logic — the claim anchoring,
`askReady` / `writeReady` gating, the disclosure-ack retry — and changes only
where it is drawn.

**No new dependency is needed**, despite `TableControls.tsx`'s note about
avoiding `@floating-ui/dom`. Verified:

- `@floating-ui/dom@1.7.6` and `@floating-ui/react@0.27.19` are already in the
  pnpm store (pulled in by Mantine);
- `@mantine/core@9.4.0` is a direct dependency and ships `Popover`, which does
  the positioning;
- `prosemirror-view@1.41.9` provides `coordsAtPos()` for the selection's screen
  rectangle.

This matters because the repo has a **7-day pnpm cooldown** (`minimumReleaseAge`)
that blocks `pnpm add` even for old versions. A design needing a new package
would stall.

Mechanism: on selection change, compute the rectangle from
`editor.view.coordsAtPos(from)` and `coordsAtPos(to)`, position a zero-size
anchor `<div>` at it, and render the existing toolbar content inside a Mantine
`Popover` attached to that anchor.

**One `coordsAtPos` sample is not enough** (finding 5). Those coordinates are
viewport-relative and go stale whenever the content moves without the selection
changing — scrolling the editor's container, resizing the cockpit's resizable
pane, or a reflow that changes line wrapping. Slice 2 must recompute on the
editor's `transaction` event **and** on `scroll` of the nearest scrollable
ancestor **and** on a `ResizeObserver` over the editor element, tearing all
three down on unmount. Its tests must cover a scroll and a pane resize, not just
a selection change.

The existing behaviour is unchanged by this slice. **Revision 2 corrects two
descriptions that were wrong** (Sol review findings 2 and 4, both confirmed
against the code):

| Selection | What actually happens |
|---|---|
| empty | `deriveClaimAnchor` returns `null` → nothing rendered (`selectionAnchor.ts:26`) |
| inside a heading | a claim **IS** returned, with `headingOnly: true` (`selectionAnchor.ts:32-38`). The popup **appears**; `Ask` is enabled if anchorable, `Edit` is disabled because `writeReady = settled && anchorable && !claim.headingOnly` |
| anchor text not unique in the section | `anchorable` false → `askReady` false **and** `writeReady` false. `Ask`, `Edit` **and `Cite`** are all disabled — `Cite` is `disabled={!writeReady}` at `TiptapSelectionToolbar.tsx:169` |

The earlier draft claimed headings produce no anchor, and that `Cite` stayed
available on a non-unique anchor. Both were false. Slice 2 changes placement
only, so these are the behaviours its tests must assert.

## Testing

Vitest, matching the existing `collab/*.test.tsx` files.

**Toolbar** — for each control: build an `Editor` over `collabExtensions` (as
`schema.formatting.test.tsx` already does), click the button, assert the
resulting document. Assert the button reflects active state. Assert every
control is disabled when `canWrite` is false.

The load-bearing test is a **command-existence** one: assert each command this
toolbar calls is present on `editor.commands`. That is what would have caught
the `toggleBulletList` trap, and it fails loudly rather than producing a button
that does nothing.

**Block dropdown** — heading → Body converts to a paragraph; Body → Heading 3
produces `level: 3`; the displayed value tracks cursor position.

**Link** — Apply sets `href`; Remove clears it; `javascript:` is rejected and
sets no mark.

**Popup** — the existing `TiptapSelectionToolbar.test.tsx` assertions must keep
passing unchanged, since only placement changes. Add: it is not rendered for an
empty selection, and it renders for a body-text selection.

## Slices

Two, in order. They are independent and each is shippable alone.

**Slice 1 — the toolbar.** `EditorToolbar.tsx`, the `actions` prop, moving
`Save a version` / `Insert table` in, and moving `Export` / `Check coherence`
from the panels into the slot. No change to the AI actions.

**Slice 2 — the floating popup.** Re-place `TiptapSelectionToolbar` in a Mantine
`Popover` anchored at the selection coordinates. No change to its logic.

Slice 2 is the fiddlier half — coordinates, scroll containers, and the editor
being inside a resizable pane. Keeping it separate means Slice 1 is not held up
by it.
