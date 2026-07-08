/**
 * Constrained ProseMirror schema for the collaborative-editing bridge.
 *
 * Nodes: doc, paragraph, heading (H1–H6), bullet_list, ordered_list,
 *        list_item, blockquote, code_block, horizontal_rule, image,
 *        hard_break, citation (inline atom), table, table_row, table_cell,
 *        table_header.
 * Marks: strong, em, strikethrough, code, link.
 *
 * This is the JS source of truth for golden generation (Task 4) and the
 * future Tiptap/ProseMirror editor.  The Python schema in
 * ``src/call_manager/proposal_assist/collab/schema.py`` must remain in
 * sync with it.
 */

import { Schema } from "prosemirror-model";
import { orderedList, bulletList, listItem } from "prosemirror-schema-list";

// ---------------------------------------------------------------------------
// Node specs
// ---------------------------------------------------------------------------

/** @type {import("prosemirror-model").NodeSpec} */
const docSpec = { content: "block+" };

/** @type {import("prosemirror-model").NodeSpec} */
const paragraphSpec = {
  content: "inline*",
  group: "block",
  parseDOM: [{ tag: "p" }],
  toDOM() { return ["p", 0]; },
};

/** @type {import("prosemirror-model").NodeSpec} */
const blockquoteSpec = {
  content: "block+",
  group: "block",
  defining: true,
  parseDOM: [{ tag: "blockquote" }],
  toDOM() { return ["blockquote", 0]; },
};

/** @type {import("prosemirror-model").NodeSpec} */
const horizontalRuleSpec = {
  group: "block",
  parseDOM: [{ tag: "hr" }],
  toDOM() { return ["hr"]; },
};

/** @type {import("prosemirror-model").NodeSpec} */
const headingSpec = {
  attrs: { level: { default: 1, validate: "number" } },
  content: "inline*",
  group: "block",
  defining: true,
  parseDOM: [
    { tag: "h1", attrs: { level: 1 } },
    { tag: "h2", attrs: { level: 2 } },
    { tag: "h3", attrs: { level: 3 } },
    { tag: "h4", attrs: { level: 4 } },
    { tag: "h5", attrs: { level: 5 } },
    { tag: "h6", attrs: { level: 6 } },
  ],
  toDOM(node) { return [`h${node.attrs.level}`, 0]; },
};

/** @type {import("prosemirror-model").NodeSpec} */
const codeBlockSpec = {
  content: "text*",
  marks: "",
  group: "block",
  code: true,
  defining: true,
  parseDOM: [{ tag: "pre", preserveWhitespace: "full" }],
  toDOM() { return ["pre", ["code", 0]]; },
};

/** @type {import("prosemirror-model").NodeSpec} */
const textSpec = { group: "inline" };

/** @type {import("prosemirror-model").NodeSpec} */
const imageSpec = {
  inline: true,
  attrs: {
    src: { validate: "string" },
    alt: { default: null, validate: "string|null" },
    title: { default: null, validate: "string|null" },
  },
  group: "inline",
  draggable: true,
  parseDOM: [{
    tag: "img[src]",
    getAttrs(dom) {
      return {
        src: dom.getAttribute("src"),
        title: dom.getAttribute("title"),
        alt: dom.getAttribute("alt"),
      };
    },
  }],
  toDOM(node) {
    return ["img", { src: node.attrs.src, alt: node.attrs.alt, title: node.attrs.title }];
  },
};

/** @type {import("prosemirror-model").NodeSpec} */
const hardBreakSpec = {
  inline: true,
  group: "inline",
  selectable: false,
  parseDOM: [{ tag: "br" }],
  toDOM() { return ["br"]; },
};

/**
 * Custom citation: inline atom carrying citekey + ev_id.
 * @type {import("prosemirror-model").NodeSpec}
 */
const citationSpec = {
  inline: true,
  atom: true,
  group: "inline",
  attrs: {
    citekey: {},
    ev_id: {},
  },
  parseDOM: [{
    tag: "span[data-citekey][data-ev-id]",
    getAttrs(dom) {
      return {
        citekey: dom.getAttribute("data-citekey"),
        ev_id: dom.getAttribute("data-ev-id"),
      };
    },
  }],
  toDOM(node) {
    return [
      "span",
      {
        "data-citekey": node.attrs.citekey,
        "data-ev-id": node.attrs.ev_id,
      },
    ];
  },
};

// List node specs from prosemirror-schema-list, augmented with group/content.
const orderedListSpec = {
  ...orderedList,
  content: "list_item+",
  group: "block",
};

const bulletListSpec = {
  ...bulletList,
  content: "list_item+",
  group: "block",
};

const listItemSpec = {
  ...listItem,
  content: "paragraph block*",
};

// --- Strict-GFM tables (2026-07-08 collab-tables spec §A) -------------------
// Shape-identical to the Tiptap extensions in src/collab/schema.ts and the
// Python schema.py: content strings, attrs defaults, isolating, tableRole.

/** @type {import("prosemirror-model").NodeSpec} */
const tableSpec = {
  content: "table_row+",
  group: "block",
  isolating: true,
  tableRole: "table",
  parseDOM: [{ tag: "table" }],
  toDOM() { return ["table", ["tbody", 0]]; },
};

/** @type {import("prosemirror-model").NodeSpec} */
const tableRowSpec = {
  content: "(table_cell | table_header)+",
  tableRole: "row",
  parseDOM: [{ tag: "tr" }],
  toDOM() { return ["tr", 0]; },
};

const gfmCellAttrs = {
  colspan: { default: 1 },
  rowspan: { default: 1 },
  colwidth: { default: null },
};

/** @type {import("prosemirror-model").NodeSpec} */
const tableCellSpec = {
  content: "paragraph",
  attrs: { ...gfmCellAttrs },
  isolating: true,
  tableRole: "cell",
  parseDOM: [{ tag: "td" }],
  toDOM() { return ["td", 0]; },
};

/** @type {import("prosemirror-model").NodeSpec} */
const tableHeaderSpec = {
  content: "paragraph",
  attrs: { ...gfmCellAttrs },
  isolating: true,
  tableRole: "header_cell",
  parseDOM: [{ tag: "th" }],
  toDOM() { return ["th", 0]; },
};

// ---------------------------------------------------------------------------
// Mark specs
// ---------------------------------------------------------------------------

/** @type {import("prosemirror-model").MarkSpec} */
const linkSpec = {
  attrs: {
    href: { validate: "string" },
    title: { default: null, validate: "string|null" },
  },
  inclusive: false,
  parseDOM: [{
    tag: "a[href]",
    getAttrs(dom) {
      return { href: dom.getAttribute("href"), title: dom.getAttribute("title") };
    },
  }],
  toDOM(node) {
    return ["a", { href: node.attrs.href, title: node.attrs.title }, 0];
  },
};

/** @type {import("prosemirror-model").MarkSpec} */
const emSpec = {
  parseDOM: [
    { tag: "i" },
    { tag: "em" },
    { style: "font-style=italic" },
    { style: "font-style=normal", clearMark: (m) => m.type.name === "em" },
  ],
  toDOM() { return ["em", 0]; },
};

/** @type {import("prosemirror-model").MarkSpec} */
const strongSpec = {
  parseDOM: [
    { tag: "strong" },
    {
      tag: "b",
      getAttrs: (node) =>
        "font-weight:normal" in (node.style || {}) ? false : null,
    },
    { style: "font-weight=400", clearMark: (m) => m.type.name === "strong" },
    {
      style: "font-weight",
      getAttrs: (value) => (/^(bold(er)?|[5-9]\d{2,})$/.test(value) ? null : false),
    },
  ],
  toDOM() { return ["strong", 0]; },
};

/** @type {import("prosemirror-model").MarkSpec} */
const codeSpec = {
  code: true,
  parseDOM: [{ tag: "code" }],
  toDOM() { return ["code", 0]; },
};

/** @type {import("prosemirror-model").MarkSpec} */
const strikethroughSpec = {
  parseDOM: [
    { tag: "s" },
    { tag: "strike" },
    { tag: "del" },
    { style: "text-decoration=line-through" },
  ],
  toDOM() { return ["s", 0]; },
};

// ---------------------------------------------------------------------------
// Schema
// ---------------------------------------------------------------------------

export const schema = new Schema({
  nodes: {
    doc: docSpec,
    paragraph: paragraphSpec,
    blockquote: blockquoteSpec,
    horizontal_rule: horizontalRuleSpec,
    heading: headingSpec,
    code_block: codeBlockSpec,
    text: textSpec,
    image: imageSpec,
    hard_break: hardBreakSpec,
    citation: citationSpec,
    ordered_list: orderedListSpec,
    bullet_list: bulletListSpec,
    list_item: listItemSpec,
    table: tableSpec,
    table_row: tableRowSpec,
    table_cell: tableCellSpec,
    table_header: tableHeaderSpec,
  },
  marks: {
    link: linkSpec,
    em: emSpec,
    strong: strongSpec,
    code: codeSpec,
    strikethrough: strikethroughSpec,
  },
});
