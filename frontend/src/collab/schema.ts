import {
  Node,
  Mark,
  mergeAttributes,
  textblockTypeInputRule,
  wrappingInputRule,
  type Extensions,
} from "@tiptap/core";
import {
  Table as BaseTable,
  TableCell as BaseTableCell,
  TableHeader as BaseTableHeader,
  TableRow as BaseTableRow,
} from "@tiptap/extension-table";

// NOTE on required-vs-nullable attrs (intrinsic Tiptap limitation):
// collab-schema.mjs declares citation.citekey/ev_id, image.src and link.href as
// REQUIRED (no default). Tiptap CANNOT express a required attribute — getSchema
// always injects `default: null`, so these attrs are nullable here. This is a
// forced, unavoidable deviation, not an oversight; the schema-equality test
// (collabSchema.equality.test.ts) therefore does not distinguish required from
// nullable. It is inert for 3b-3: the server always materializes these attrs
// from markdown (non-null), citations are read-only, and there is no client
// authoring UI, so the null default is never exercised. When the future
// client-authored-citation slice lands, the checkpoint/markdown serializer must
// guard a null citekey (a citation created without one) before it can serialize.

const Doc = Node.create({ name: "doc", topNode: true, content: "block+" });
const Text = Node.create({ name: "text", group: "inline" });
const Paragraph = Node.create({
  name: "paragraph", group: "block", content: "inline*",
  parseHTML: () => [{ tag: "p" }], renderHTML: ({ HTMLAttributes }) => ["p", mergeAttributes(HTMLAttributes), 0],
});
const Heading = Node.create({
  name: "heading", group: "block", content: "inline*", defining: true,
  addAttributes: () => ({ level: { default: 1 } }),
  parseHTML: () => [1, 2, 3, 4, 5, 6].map((level) => ({ tag: `h${level}`, attrs: { level } })),
  renderHTML: ({ node, HTMLAttributes }) => [`h${node.attrs.level}`, mergeAttributes(HTMLAttributes), 0],
  addInputRules() {
    // "# " .. "###### " at line start → heading of that level (3b-5d.D4 floor)
    return [1, 2, 3, 4, 5, 6].map((level) =>
      textblockTypeInputRule({
        find: new RegExp(`^(#{${level}})\\s$`),
        type: this.type,
        getAttributes: { level },
      })
    );
  },
});
const Blockquote = Node.create({
  name: "blockquote", group: "block", content: "block+", defining: true,
  parseHTML: () => [{ tag: "blockquote" }], renderHTML: ({ HTMLAttributes }) => ["blockquote", mergeAttributes(HTMLAttributes), 0],
});
const HorizontalRule = Node.create({
  name: "horizontal_rule", group: "block",
  parseHTML: () => [{ tag: "hr" }], renderHTML: () => ["hr"],
});
const CodeBlock = Node.create({
  name: "code_block", group: "block", content: "text*", marks: "", code: true, defining: true,
  parseHTML: () => [{ tag: "pre", preserveWhitespace: "full" }], renderHTML: ({ HTMLAttributes }) => ["pre", ["code", mergeAttributes(HTMLAttributes), 0]],
});
const HardBreak = Node.create({
  name: "hard_break", group: "inline", inline: true, selectable: false,
  parseHTML: () => [{ tag: "br" }], renderHTML: () => ["br"],
});
const Image = Node.create({
  name: "image", group: "inline", inline: true, draggable: true,
  addAttributes: () => ({ src: { default: null }, alt: { default: null }, title: { default: null } }),
  parseHTML: () => [{ tag: "img[src]" }], renderHTML: ({ HTMLAttributes }) => ["img", mergeAttributes(HTMLAttributes)],
});
const BulletList = Node.create({
  name: "bullet_list", group: "block", content: "list_item+",
  parseHTML: () => [{ tag: "ul" }], renderHTML: ({ HTMLAttributes }) => ["ul", mergeAttributes(HTMLAttributes), 0],
  addInputRules() {
    return [wrappingInputRule({ find: /^\s*([-+*])\s$/, type: this.type })];
  },
});
const OrderedList = Node.create({
  name: "ordered_list", group: "block", content: "list_item+",
  addAttributes: () => ({ order: { default: 1 } }),
  parseHTML: () => [{ tag: "ol" }], renderHTML: ({ HTMLAttributes }) => ["ol", mergeAttributes(HTMLAttributes), 0],
  addInputRules() {
    return [
      wrappingInputRule({
        find: /^(\d+)\.\s$/,
        type: this.type,
        getAttributes: (match) => ({ order: +match[1] }),
      }),
    ];
  },
});
const ListItem = Node.create({
  name: "list_item", content: "paragraph block*", defining: true,
  parseHTML: () => [{ tag: "li" }], renderHTML: ({ HTMLAttributes }) => ["li", mergeAttributes(HTMLAttributes), 0],
});

// Strict-GFM constrained tables (2026-07-08 collab-tables spec §A/§E):
// snake_case names to match the server schema; cells hold ONE paragraph;
// attr-level parseHTML pins colspan/rowspan/colwidth to their defaults so a
// pasted Word/HTML merge normalizes to 1x1 cells (prosemirror-tables'
// fixTables pads the resulting ragged rows); resizing off — colwidth can
// never be set. The server serializer's degrade law is the backstop.
const gfmCellAttributes = () => ({
  colspan: { default: 1, parseHTML: () => 1 },
  rowspan: { default: 1, parseHTML: () => 1 },
  colwidth: { default: null, parseHTML: () => null },
});
const Table = BaseTable.extend({ name: "table", content: "table_row+" }).configure({
  resizable: false,
});
const TableRow = BaseTableRow.extend({
  name: "table_row",
  content: "(table_cell | table_header)+",
});
const TableHeader = BaseTableHeader.extend({
  name: "table_header",
  content: "paragraph",
  addAttributes: gfmCellAttributes,
});
const TableCell = BaseTableCell.extend({
  name: "table_cell",
  content: "paragraph",
  addAttributes: gfmCellAttributes,
});

export const Citation = Node.create({
  name: "citation", group: "inline", inline: true, atom: true,
  addAttributes: () => ({ citekey: { default: null }, ev_id: { default: null } }),
  parseHTML: () => [{ tag: "span[data-citekey][data-ev-id]" }],
  renderHTML: ({ HTMLAttributes }) => ["span", mergeAttributes(HTMLAttributes)],
  // NodeView (pill) is attached in Task 4 via addNodeView.
});

const Link = Mark.create({
  name: "link", inclusive: false,
  addAttributes: () => ({ href: { default: null }, title: { default: null } }),
  parseHTML: () => [{ tag: "a[href]" }], renderHTML: ({ HTMLAttributes }) => ["a", mergeAttributes(HTMLAttributes), 0],
});
const Em = Mark.create({
  name: "em", parseHTML: () => [{ tag: "em" }, { tag: "i" }], renderHTML: () => ["em", 0],
  addKeyboardShortcuts() {
    return { "Mod-i": () => this.editor.commands.toggleMark(this.name) };
  },
});
const Strong = Mark.create({
  name: "strong", parseHTML: () => [{ tag: "strong" }, { tag: "b" }], renderHTML: () => ["strong", 0],
  addKeyboardShortcuts() {
    return { "Mod-b": () => this.editor.commands.toggleMark(this.name) };
  },
});
const Code = Mark.create({ name: "code", code: true, parseHTML: () => [{ tag: "code" }], renderHTML: () => ["code", 0] });
const Strikethrough = Mark.create({
  name: "strikethrough", parseHTML: () => [{ tag: "s" }, { tag: "strike" }, { tag: "del" }], renderHTML: () => ["s", 0],
  addKeyboardShortcuts() {
    return { "Mod-Shift-s": () => this.editor.commands.toggleMark(this.name) };
  },
});

export const collabExtensions: Extensions = [
  Doc, Text, Paragraph, Heading, Blockquote, HorizontalRule, CodeBlock, HardBreak,
  Image, BulletList, OrderedList, ListItem, Citation, Link, Em, Strong, Code, Strikethrough,
  Table, TableRow, TableHeader, TableCell,
];
