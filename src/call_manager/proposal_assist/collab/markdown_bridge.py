"""Markdown <-> ProseMirror-JSON bridge for the collaborative-editing schema.

Two pure functions:

- :func:`markdown_to_pm` parses markdown with ``markdown-it-py`` (CommonMark +
  strikethrough + GFM tables) and walks the token stream into ProseMirror-JSON for the
  constrained :data:`SCHEMA`. A custom inline rule recognises the FULL citation
  marker (``[@key]<!-- ev:ev-… -->``) and emits a ``citation`` atom node, so the
  marker never survives as text. A BARE ``[@x9]`` (no ev comment) is left as
  plain literal text. The result is validated in two steps — node TYPES/ATTRS
  via ``SCHEMA.node_from_json`` and the CONTENT MODEL via the constructed node's
  ``.check()`` — so an out-of-schema doc raises.
- :func:`pm_to_markdown` serialises the constrained schema back to markdown.
  ``prosemirror-py-converter`` is NOT installed (and prosemirror-py ships only a
  DOM serializer), so this is a focused hand-written serializer over our small
  node/mark set. Non-final blocks emit a trailing blank line so the
  ``split_sections`` ``##``-boundary rule holds and the round-trip is idempotent.

The serializer's output is the canonical normal form: ``markdown_to_pm`` is
many-to-one (tight and loose source both normalise to the same doc), so applying
the round-trip twice is a no-op.

Raw HTML is INTENTIONALLY unrepresentable. The constrained :data:`SCHEMA`
models no raw-HTML node or mark, so ``markdown_to_pm`` *drops* every kind of
non-citation HTML it encounters: block-level HTML, inline HTML tags, and HTML
comments. The ONE HTML construct we recognise is the FULL citation marker
(``[@key]<!-- ev:ev-… -->``), consumed by ``_citation_rule`` into a ``citation``
atom before the generic HTML tokens ever fire. Consequently a MALFORMED or
PARTIAL citation marker — an orphan ``<!-- ev:… -->`` comment with no preceding
``[@key]``, or a bare unparsed fragment — does NOT match the marker rule and is
therefore dropped as ordinary HTML. (A bare ``[@key]`` with no ev comment is the
exception: it is plain text, not HTML, so it survives as literal text.) This is a
known hydration consideration: any document whose meaning depends on raw HTML, or
on a half-written citation marker, will lose that HTML on the round-trip. The
drops are deliberate, not accidental; the drop sites below restate this.
"""

from __future__ import annotations

from typing import Any

from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline

from call_manager.proposal_assist.citations.markers import MARKER_RE, build_marker

from .schema import CITATION_NODE, SCHEMA

# ---------------------------------------------------------------------------
# markdown -> ProseMirror JSON
# ---------------------------------------------------------------------------

# Inline marks we understand, keyed by the markdown-it ``*_open``/``*_close``
# token type. The value is the ProseMirror mark spec to attach.
_MARK_OPEN = {
    "strong_open": "strong",
    "em_open": "em",
    "s_open": "strikethrough",
    "link_open": "link",
}
_MARK_CLOSE = {
    "strong_close": "strong",
    "em_close": "em",
    "s_close": "strikethrough",
    "link_close": "link",
}


def _citation_rule(state: StateInline, silent: bool) -> bool:
    """markdown-it inline rule: consume a full citation marker as one atom.

    Runs before ``link``/``html_inline`` so the marker is never split into a
    ``[@key]`` text run plus an ``<!-- ev:… -->`` html_inline token.
    """
    if state.src[state.pos] != "[":
        return False
    match = MARKER_RE.match(state.src, state.pos)
    if match is None:
        return False
    if not silent:
        token = state.push("citation", "", 0)
        token.meta = {
            "citekey": match.group("citekey"),
            "ev_id": match.group("ev_id"),
        }
    state.pos = match.end()
    return True


def _make_parser() -> MarkdownIt:
    md = MarkdownIt("commonmark").enable(["strikethrough", "table"])
    # Register before "link": both trigger on "[", and we must win so the full
    # marker is consumed as a single citation atom rather than a link attempt
    # followed by an html_inline comment.
    md.inline.ruler.before("link", "citation", _citation_rule)
    return md


_PARSER = _make_parser()


def _inline_to_pm(tokens: list[Any]) -> list[dict]:
    """Convert a list of inline child tokens into ProseMirror inline nodes,
    tracking the active mark stack across ``*_open``/``*_close`` pairs."""
    out: list[dict] = []
    marks: list[dict] = []  # active marks, in open order

    def with_marks(node: dict) -> dict:
        if marks:
            node = dict(node)
            node["marks"] = [dict(m) for m in marks]
        return node

    for tok in tokens:
        ttype = tok.type
        if ttype == "text":
            if tok.content:
                out.append(with_marks({"type": "text", "text": tok.content}))
        elif ttype == "citation":
            out.append(
                with_marks(
                    {
                        "type": CITATION_NODE,
                        "attrs": {
                            "citekey": tok.meta["citekey"],
                            "ev_id": tok.meta["ev_id"],
                        },
                    }
                )
            )
        elif ttype == "code_inline":
            code_marks = [dict(m) for m in marks] + [{"type": "code"}]
            out.append({"type": "text", "text": tok.content, "marks": code_marks})
        elif ttype == "softbreak":
            out.append(with_marks({"type": "text", "text": "\n"}))
        elif ttype == "hardbreak":
            out.append(with_marks({"type": "hard_break"}))
        elif ttype == "image":
            attrs = {
                "src": tok.attrs.get("src", ""),
                "alt": _image_alt(tok),
                "title": tok.attrs.get("title") or None,
            }
            out.append(with_marks({"type": "image", "attrs": attrs}))
        elif ttype in _MARK_OPEN:
            mark_type = _MARK_OPEN[ttype]
            mark: dict = {"type": mark_type}
            if mark_type == "link":
                mark["attrs"] = {
                    "href": tok.attrs.get("href", ""),
                    "title": tok.attrs.get("title") or None,
                }
            marks.append(mark)
        elif ttype in _MARK_CLOSE:
            mark_type = _MARK_CLOSE[ttype]
            # pop the most recently opened matching mark
            for i in range(len(marks) - 1, -1, -1):
                if marks[i]["type"] == mark_type:
                    marks.pop(i)
                    break
        # INTENTIONAL DROP. Any inline token we don't model — notably
        # ``html_inline`` (raw inline HTML tags, HTML comments) — is dropped on
        # purpose: the constrained schema has no raw-HTML node/mark, so there is
        # nowhere to put it. The full citation marker is already claimed upstream
        # by ``_citation_rule`` into a ``citation`` atom; what reaches here is
        # therefore non-citation HTML OR a malformed/partial citation marker
        # (e.g. an orphan ``<!-- ev:… -->`` comment with no preceding ``[@key]``).
        # Both are dropped. This is a known hydration consideration — see the
        # module docstring. We do NOT add raw-HTML support.
    return out


def _image_alt(tok: Any) -> str:
    # markdown-it stores the alt text as inline children of the image token.
    if tok.children:
        return "".join(c.content for c in tok.children)
    return tok.content or ""


def _block_to_pm(tokens: list[Any], i: int) -> tuple[dict | None, int]:
    """Parse one block starting at index ``i``; return (node, next_index).

    ``node`` is ``None`` for tokens that produce nothing (defensive)."""
    tok = tokens[i]
    ttype = tok.type

    if ttype == "heading_open":
        level = int(tok.tag[1:])  # h2 -> 2
        inline = tokens[i + 1]
        content = _inline_to_pm(inline.children or [])
        node = {"type": "heading", "attrs": {"level": level}, "content": content}
        return node, i + 3  # heading_open, inline, heading_close

    if ttype == "paragraph_open":
        inline = tokens[i + 1]
        content = _inline_to_pm(inline.children or [])
        node = {"type": "paragraph"}
        if content:
            node["content"] = content
        return node, i + 3

    if ttype == "blockquote_open":
        children, j = _blocks_until(tokens, i + 1, "blockquote_close")
        node = {"type": "blockquote", "content": children}
        return node, j + 1

    if ttype in ("bullet_list_open", "ordered_list_open"):
        list_type = "bullet_list" if ttype == "bullet_list_open" else "ordered_list"
        close = "bullet_list_close" if ttype == "bullet_list_open" else "ordered_list_close"
        items, j = _list_items(tokens, i + 1, close)
        node = {"type": list_type, "content": items}
        if list_type == "ordered_list":
            start = tok.attrs.get("start")
            node["attrs"] = {"order": int(start) if start is not None else 1}
        return node, j + 1

    if ttype == "code_block" or ttype == "fence":
        node = {
            "type": "code_block",
            "content": [{"type": "text", "text": tok.content.rstrip("\n")}]
            if tok.content.strip()
            else [],
        }
        return node, i + 1

    if ttype == "hr":
        return {"type": "horizontal_rule"}, i + 1

    if ttype == "table_open":
        return _table_to_pm(tokens, i)

    # INTENTIONAL DROP. Any block token we don't model — notably ``html_block``
    # (a raw block of HTML, including block-level HTML comments) — is dropped on
    # purpose: the constrained schema models no raw-HTML node, so there is
    # nowhere to put it. A block-level malformed/partial citation marker falls
    # here too and is likewise dropped. This is a known hydration consideration —
    # see the module docstring. We do NOT add raw-HTML support.
    return None, i + 1


def _blocks_until(tokens: list[Any], i: int, stop_type: str) -> tuple[list[dict], int]:
    """Collect block nodes from ``i`` until the matching ``stop_type`` token.
    Returns (nodes, index_of_stop_token)."""
    nodes: list[dict] = []
    while i < len(tokens) and tokens[i].type != stop_type:
        node, i = _block_to_pm(tokens, i)
        if node is not None:
            nodes.append(node)
    return nodes, i


def _list_items(tokens: list[Any], i: int, stop_type: str) -> tuple[list[dict], int]:
    items: list[dict] = []
    while i < len(tokens) and tokens[i].type != stop_type:
        assert tokens[i].type == "list_item_open", tokens[i].type
        children, j = _blocks_until(tokens, i + 1, "list_item_close")
        # list_item content is "paragraph block*": a non-paragraph first block
        # (fence-first, quote-first, table-first, or an empty item) fails
        # node.check(). Synthesize an empty leading paragraph; the serializer
        # DROPS it again when more blocks follow (Task 3 companion — emitting
        # it would blank-line-break the item and the block would escape the
        # list on re-parse), so the round-trip stays idempotent (spec §B).
        if not children or children[0]["type"] != "paragraph":
            children.insert(0, {"type": "paragraph"})
        items.append({"type": "list_item", "content": children})
        i = j + 1  # skip the list_item_close
    return items, i


def _table_to_pm(tokens: list[Any], i: int) -> tuple[dict, int]:
    """Consume ``table_open`` .. ``table_close`` into a strict-GFM table node.

    markdown-it emits thead/tbody wrappers with no PM counterpart — rows
    flatten directly into the table. Cells carry the CANONICAL attr form
    (explicit colspan/rowspan = 1, NO colwidth key — spec §B): the hand-built
    dict IS the output, schema defaults are never materialized into it, and
    the Yjs encoding omits nulls, so this exact shape is what ydoc_to_pm_json
    reproduces. Alignment styles on th/td are dropped on purpose (spec §2.1).
    """
    rows: list[dict] = []
    i += 1  # skip table_open
    while tokens[i].type != "table_close":
        t = tokens[i].type
        if t in ("thead_open", "thead_close", "tbody_open", "tbody_close"):
            i += 1
            continue
        assert t == "tr_open", t
        cells: list[dict] = []
        i += 1
        while tokens[i].type != "tr_close":
            cell_type = "table_header" if tokens[i].type == "th_open" else "table_cell"
            inline = tokens[i + 1]
            content = _inline_to_pm(inline.children or [])
            para: dict = {"type": "paragraph"}
            if content:
                para["content"] = content
            cells.append(
                {
                    "type": cell_type,
                    "attrs": {"colspan": 1, "rowspan": 1},
                    "content": [para],
                }
            )
            i += 3  # {th,td}_open, inline, {th,td}_close
        rows.append({"type": "table_row", "content": cells})
        i += 1  # skip tr_close
    return {"type": "table", "content": rows}, i + 1  # skip table_close


def markdown_to_pm(md: str) -> dict:
    """Parse *md* into a ProseMirror-JSON ``doc`` validated against ``SCHEMA``."""
    tokens = _PARSER.parse(md)
    content: list[dict] = []
    i = 0
    while i < len(tokens):
        node, i = _block_to_pm(tokens, i)
        if node is not None:
            content.append(node)
    doc = {"type": "doc", "content": content}
    # Validate in two steps:
    #   1. ``node_from_json`` constructs the node, checking node TYPES and ATTRS
    #      (an unknown node type or a missing required attr raises here).
    #   2. ``.check()`` then verifies the CONTENT MODEL — that every node's
    #      children satisfy its content expression (e.g. doc holds block+,
    #      list_item holds "paragraph block*"). ``node_from_json`` alone does NOT
    #      enforce this, so without the check an out-of-schema arrangement of
    #      otherwise-valid nodes would slip through.
    node = SCHEMA.node_from_json(doc)
    node.check()
    return doc


# ---------------------------------------------------------------------------
# ProseMirror JSON -> markdown
# ---------------------------------------------------------------------------

# Mark delimiters for the "wrap" style marks. Order matters for nesting output
# but our corpus never stacks these in a way the order changes; we emit
# innermost-last consistently.
_MARK_DELIM = {
    "strong": ("**", "**"),
    "em": ("*", "*"),
    "strikethrough": ("~~", "~~"),
    # NOTE: ``code`` is intentionally NOT here. Its delimiter is a *variable*
    # run of backticks chosen from the body text (CommonMark "code span" rule),
    # so it cannot be a fixed open/close pair. It is serialized as a whole unit
    # in ``_serialize_inline_atom`` (see ``_wrap_inline_code``); the generic
    # mark open/close logic emits nothing for it.
}

# Characters that begin a markdown construct and so must be escaped when they
# start a block of plain text, plus a couple that are escaped inline. We keep
# this deliberately minimal: enough to keep our own serialized output stable and
# re-parseable, while NOT escaping "[" so bare ``[@key]`` markers stay literal.
_INLINE_ESCAPE = {"*": "\\*", "_": "\\_", "`": "\\`", "~": "\\~", "\\": "\\\\"}


def _escape_text(text: str) -> str:
    """Escape inline-markdown-significant characters in plain text.

    We deliberately do NOT escape ``[`` so a bare ``[@x9]`` citekey marker
    survives as literal, UNESCAPED text (the bare-marker scan must still match
    it). ``*``/``_``/`` ` ``/``~``/``\\`` are escaped so emphasis-like runs in
    body text do not re-parse into marks on the next round-trip.
    """
    return "".join(_INLINE_ESCAPE.get(ch, ch) for ch in text)


def _serialize_inline(nodes: list[dict]) -> str:
    """Serialize an inline node list, opening/closing marks as the active set
    changes between adjacent nodes (mirrors the parse-time mark stack)."""
    out: list[str] = []
    active: list[dict] = []

    def close_to(target: list[dict]) -> None:
        nonlocal active
        # Close marks not present in target, from innermost outward.
        while active and active[-1] not in target:
            out.append(_close_mark(active.pop()))

    def open_from(target: list[dict]) -> None:
        nonlocal active
        for m in target:
            if m not in active:
                out.append(_open_mark(m))
                active.append(m)

    for node in nodes:
        node_marks = node.get("marks", [])
        close_to(node_marks)
        open_from(node_marks)
        out.append(_serialize_inline_atom(node))

    close_to([])
    return "".join(out)


def _open_mark(m: dict) -> str:
    delim = _MARK_DELIM.get(m["type"])
    if delim:
        return delim[0]
    if m["type"] == "link":
        return "["
    return ""


def _close_mark(m: dict) -> str:
    delim = _MARK_DELIM.get(m["type"])
    if delim:
        return delim[1]
    if m["type"] == "link":
        attrs = m.get("attrs", {})
        href = attrs.get("href", "")
        title = attrs.get("title")
        if title:
            return f']({href} "{title}")'
        return f"]({href})"
    return ""


def _wrap_inline_code(text: str) -> str:
    """Wrap *text* as a CommonMark inline code span with a backtick-safe delimiter.

    The delimiter is a run of backticks ONE LONGER than the longest run of
    backticks appearing in *text*, so a backtick inside the code never collides
    with the delimiter (the code body is otherwise emitted verbatim — code spans
    have no backslash escaping). If the body begins or ends with a backtick (or
    is entirely backticks/whitespace), a single space is padded on each side so
    the leading/trailing backtick is not eaten as part of the delimiter; one of
    those padding spaces is stripped again on parse, leaving the body intact and
    keeping the round-trip idempotent.
    """
    longest = 0
    run = 0
    for ch in text:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    fence = "`" * (longest + 1)
    body = text
    if body.startswith("`") or body.endswith("`") or body.strip("`") == "":
        body = f" {body} "
    return f"{fence}{body}{fence}"


def _serialize_inline_atom(node: dict) -> str:
    ntype = node["type"]
    if ntype == "text":
        # code-marked text is its own self-contained span: the ``code`` mark is
        # NOT a fixed-delimiter mark (see ``_MARK_DELIM``), so we emit the whole
        # backtick-delimited span here with a body-safe delimiter run. The body
        # is verbatim (code spans have no backslash escaping).
        marks = node.get("marks", [])
        if any(m["type"] == "code" for m in marks):
            return _wrap_inline_code(node["text"])
        return _escape_text(node["text"])
    if ntype == CITATION_NODE:
        attrs = node["attrs"]
        return build_marker(attrs["citekey"], attrs["ev_id"])
    if ntype == "hard_break":
        return "\\\n"
    if ntype == "image":
        attrs = node.get("attrs", {})
        alt = attrs.get("alt") or ""
        src = attrs.get("src", "")
        title = attrs.get("title")
        if title:
            return f'![{alt}]({src} "{title}")'
        return f"![{alt}]({src})"
    return ""


def _serialize_block(node: dict) -> str:
    """Serialize one block node to markdown text (no trailing block separator)."""
    ntype = node["type"]
    if ntype == "paragraph":
        body = _serialize_inline(node.get("content", []))
        return body
    if ntype == "heading":
        level = node.get("attrs", {}).get("level", 1)
        body = _serialize_inline(node.get("content", []))
        return f"{'#' * level} {body}"
    if ntype == "code_block":
        body = _code_block_text(node)
        return f"```\n{body}\n```" if body else "```\n```"
    if ntype == "horizontal_rule":
        return "---"
    if ntype == "blockquote":
        inner = _serialize_blocks(node.get("content", []))
        return "\n".join("> " + line if line else ">" for line in inner.split("\n"))
    if ntype in ("bullet_list", "ordered_list"):
        return _serialize_list(node)
    return ""


def _code_block_text(node: dict) -> str:
    content = node.get("content", [])
    return "".join(c.get("text", "") for c in content)


def _serialize_list(node: dict) -> str:
    """Serialize a bullet/ordered list. Items are separated by single newlines
    (tight list); nested blocks are indented by two spaces."""
    ordered = node["type"] == "ordered_list"
    start = node.get("attrs", {}).get("order", 1) if ordered else None
    lines: list[str] = []
    for idx, item in enumerate(node.get("content", [])):
        marker = f"{start + idx}. " if ordered else "- "
        pad = " " * len(marker)
        blocks = item.get("content", [])
        item_md = _serialize_item_blocks(blocks)
        item_lines = item_md.split("\n")
        for k, line in enumerate(item_lines):
            if k == 0:
                lines.append(marker + line)
            else:
                lines.append((pad + line) if line else line)
    return "\n".join(lines)


def _serialize_item_blocks(blocks: list[dict]) -> str:
    """Serialize the blocks inside a single list item.

    Consecutive blocks in a list item need a blank line between them to remain
    distinct on re-parse — without it CommonMark folds a following paragraph
    into the previous one as a soft line-break (silent content loss, since
    ``list_item`` content is ``paragraph block*``). The ONE exception is a
    following list: a list marker interrupts a paragraph on its own, so it stays
    tight — which also keeps single-paragraph + nested-list items byte-identical.
    """
    parts: list[str] = []
    for i, b in enumerate(blocks):
        is_list = b["type"] in ("bullet_list", "ordered_list")
        if i > 0 and not is_list:
            parts.append("")  # blank-line separator -> distinct block on re-parse
        parts.append(_serialize_list(b) if is_list else _serialize_block(b))
    return "\n".join(parts)


def _serialize_blocks(blocks: list[dict]) -> str:
    """Serialize a sequence of top-level (or blockquote) blocks, separating
    them with a single blank line. No trailing newline."""
    return "\n\n".join(_serialize_block(b) for b in blocks)


def pm_to_markdown(doc: dict) -> str:
    """Serialize a ProseMirror ``doc`` (our constrained schema) to markdown.

    Every block is followed by a trailing newline (and blocks are separated by a
    blank line) so the ``split_sections`` ``##``-boundary rule is satisfied and
    the round-trip is idempotent.
    """
    blocks = doc.get("content", [])
    if not blocks:
        return ""
    body = _serialize_blocks(blocks)
    # Single trailing newline: every block ends a line, and split_sections keeps
    # bytes exactly, so the canonical form always ends in exactly one "\n".
    return body + "\n"
