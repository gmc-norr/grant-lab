import { Node, Mark, mergeAttributes, type Extensions } from "@tiptap/core";

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
});
const OrderedList = Node.create({
  name: "ordered_list", group: "block", content: "list_item+",
  addAttributes: () => ({ order: { default: 1 } }),
  parseHTML: () => [{ tag: "ol" }], renderHTML: ({ HTMLAttributes }) => ["ol", mergeAttributes(HTMLAttributes), 0],
});
const ListItem = Node.create({
  name: "list_item", content: "paragraph block*", defining: true,
  parseHTML: () => [{ tag: "li" }], renderHTML: ({ HTMLAttributes }) => ["li", mergeAttributes(HTMLAttributes), 0],
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
const Em = Mark.create({ name: "em", parseHTML: () => [{ tag: "em" }, { tag: "i" }], renderHTML: () => ["em", 0] });
const Strong = Mark.create({ name: "strong", parseHTML: () => [{ tag: "strong" }, { tag: "b" }], renderHTML: () => ["strong", 0] });
const Code = Mark.create({ name: "code", code: true, parseHTML: () => [{ tag: "code" }], renderHTML: () => ["code", 0] });
const Strikethrough = Mark.create({
  name: "strikethrough", parseHTML: () => [{ tag: "s" }, { tag: "strike" }, { tag: "del" }], renderHTML: () => ["s", 0],
});

export const collabExtensions: Extensions = [
  Doc, Text, Paragraph, Heading, Blockquote, HorizontalRule, CodeBlock, HardBreak,
  Image, BulletList, OrderedList, ListItem, Citation, Link, Em, Strong, Code, Strikethrough,
];
