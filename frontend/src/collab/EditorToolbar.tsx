// One toolbar for the collab editor. Every control calls a GENERIC
// @tiptap/core command against the hand-rolled schema's snake_case names —
// StarterKit's toggleBulletList/toggleBlockquote/toggleHeading DO NOT EXIST
// here. See docs/superpowers/specs/2026-08-26-editor-toolbar-design.md.
import { Button, Divider, Group, NativeSelect, Popover, Text, TextInput } from "@mantine/core";
import { useState } from "react";
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

/** Only these schemes may be written into the document.
 *
 * The href goes verbatim into markdown that is committed to git and rendered
 * later, so a `javascript:` URL would survive the round trip. Allowlist, never
 * blocklist. */
const SAFE_SCHEME = /^(https?:\/\/|mailto:)/i;

function currentBlock(editor: Editor): string {
  for (const level of [1, 2, 3, 4, 5, 6]) {
    if (editor.isActive("heading", { level })) return `h${level}`;
  }
  return "p";
}

export function EditorToolbar(props: EditorToolbarProps) {
  const { editor, canWrite } = props;
  useEditorTick(editor);

  const [linkOpen, setLinkOpen] = useState(false);
  const [linkHref, setLinkHref] = useState("");
  const [linkError, setLinkError] = useState(false);

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
      <Popover
        opened={linkOpen}
        onChange={setLinkOpen}
        position="bottom"
        withArrow
        withinPortal={false}
      >
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
      {props.builtins}
      {props.actions}
    </Group>
  );
}
