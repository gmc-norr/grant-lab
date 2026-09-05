"""Constrained ProseMirror schema for the collaborative-editing bridge.

Nodes: doc, paragraph, heading (H1–H6), bullet_list, ordered_list,
       list_item, blockquote, code_block, horizontal_rule, image,
       hard_break, citation (inline atom), table, table_row, table_cell,
       table_header.
Marks: strong, em, strikethrough, code, link.

The Python schema is the server-side half of a matching pair — the JS
schema in ``frontend/scripts/collab-schema.mjs`` is the other half.
Both must describe the same nodes/marks/attrs.
"""

from prosemirror.model import Schema
from prosemirror.model.schema import MarkSpec, NodeSpec
from prosemirror.schema.basic.schema_basic import marks as basic_marks
from prosemirror.schema.basic.schema_basic import nodes as basic_nodes
from prosemirror.schema.list.schema_list import add_list_nodes

CITATION_NODE = "citation"

# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

# Start from the basic node set (doc, paragraph, blockquote,
# horizontal_rule, heading, code_block, text, image, hard_break).
# The basic schema already defines heading with level 1–6 parseDOM rules.
_nodes: dict[str, NodeSpec] = dict(basic_nodes)

# Add list nodes (bullet_list, ordered_list, list_item) from the list
# schema helper, giving lists the "block" group and list items the
# content "paragraph block*".
_nodes = add_list_nodes(_nodes, item_content="paragraph block*", list_group="block")

# Custom citation: inline atom carrying citekey + ev_id.
_nodes[CITATION_NODE] = NodeSpec(
    inline=True,
    atom=True,
    group="inline",
    attrs={
        "citekey": {},
        "ev_id": {},
    },
    parseDOM=[
        {
            "tag": "span[data-citekey][data-ev-id]",
            "getAttrs": lambda dom: {
                "citekey": dom.get("data-citekey"),
                "ev_id": dom.get("data-ev-id"),
            },
        }
    ],
    toDOM=lambda node: [
        "span",
        {
            "data-citekey": node.attrs["citekey"],
            "data-ev-id": node.attrs["ev_id"],
        },
    ],
)

# ---------------------------------------------------------------------------
# Tables (strict GFM — 2026-07-08 collab-tables spec §A)
# ---------------------------------------------------------------------------
# Cells hold exactly ONE paragraph (inline-only in practice); colspan/rowspan/
# colwidth exist because prosemirror-tables requires them — all defaulted, all
# pinned to the defaults by the client. tableRole/isolating are inert for the
# Python side (no editor here) but keep the pair description identical to
# frontend/scripts/collab-schema.mjs.
_TABLE_CELL_ATTRS = {
    "colspan": {"default": 1},
    "rowspan": {"default": 1},
    "colwidth": {"default": None},
}

_nodes["table"] = NodeSpec(
    content="table_row+",
    group="block",
    isolating=True,
    tableRole="table",
    parseDOM=[{"tag": "table"}],
    toDOM=lambda node: ["table", ["tbody", 0]],
)
_nodes["table_row"] = NodeSpec(
    content="(table_cell | table_header)+",
    tableRole="row",
    parseDOM=[{"tag": "tr"}],
    toDOM=lambda node: ["tr", 0],
)
_nodes["table_cell"] = NodeSpec(
    content="paragraph",
    attrs=dict(_TABLE_CELL_ATTRS),
    isolating=True,
    tableRole="cell",
    parseDOM=[{"tag": "td"}],
    toDOM=lambda node: ["td", 0],
)
_nodes["table_header"] = NodeSpec(
    content="paragraph",
    attrs=dict(_TABLE_CELL_ATTRS),
    isolating=True,
    tableRole="header_cell",
    parseDOM=[{"tag": "th"}],
    toDOM=lambda node: ["th", 0],
)

# ---------------------------------------------------------------------------
# Marks
# ---------------------------------------------------------------------------

# Start from basic marks (link, em, strong, code) — all required.
_marks: dict[str, MarkSpec] = dict(basic_marks)

# Add strikethrough (not in prosemirror-schema-basic).
_marks["strikethrough"] = MarkSpec(
    parseDOM=[
        {"tag": "s"},
        {"tag": "strike"},
        {"tag": "del"},
        {"style": "text-decoration=line-through"},
    ],
    toDOM=lambda _, __: ["s", 0],
)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA: Schema = Schema({"nodes": _nodes, "marks": _marks})
