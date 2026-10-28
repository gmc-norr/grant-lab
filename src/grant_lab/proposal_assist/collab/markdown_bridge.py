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
  ``table`` nodes serialize always-bordered with the first row as header
  (spec §C). Since review 2026-10-26 (H5) it also writes a link/image
  destination in the ``<…>`` form when the bare form would re-target it (a space,
  an unbalanced paren), escapes ``"`` and the backslash in a title, escapes a leading
  ``&`` that would be decoded as an entity (in body text AND in a destination or
  title, which CommonMark also runs ``unescapeAll`` over), blank-lines a nested
  ordered list whose start is not 1 (it cannot otherwise interrupt the paragraph
  above it), and alternates the marker character between adjacent same-type lists
  (which CommonMark would otherwise merge). The emphasis-adjacency class
  (``**Note:**This``) has NO CommonMark form and is still lossy; the kernel's
  ``_refuse_unsafe_section`` guard refuses to rewrite a section containing it.
  The parser's ``normalizeLink`` override keeps a destination verbatim EXCEPT
  control characters, which stay percent-encoded so a tab-split scheme cannot slip
  past ``validateLink`` (see :func:`_verbatim_link`).

The serializer's output is the canonical normal form: ``markdown_to_pm`` is
many-to-one (tight and loose source both normalise to the same doc), so applying
the round-trip twice is a no-op.

Raw HTML is INTENTIONALLY unrepresentable, and since 2026-08-30 the parser is
built with ``html=False`` so markdown-it never TOKENISES it in the first place:
``<TP53>``, ``<COHORT_NAME>`` and ``<a.b@x.se>`` are literal text that survives
the round trip. Previously they were emitted as ``html_inline`` and dropped by
the walker, silently deleting gene symbols from a grant proposal. An orphan
``<!-- ev:… -->`` with no ``[@key]`` now SURVIVES as visible literal text rather
than vanishing — visible junk is preferable to silent loss.

The older statement, kept because it still describes the SCHEMA: The constrained :data:`SCHEMA`
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

import re
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline

from grant_lab.proposal_assist.citations.markers import MARKER_RE, build_marker

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


# A control character in a link destination. Everything else is kept verbatim by
# _verbatim_link; these cannot be, because they are what a scheme bypass hides in.
_LINK_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _verbatim_link(url: str) -> str:
    """``normalizeLink`` replacement: keep the destination byte-for-byte EXCEPT
    control characters, which are percent-encoded.

    markdown-it's default percent-encodes a link destination (and punycodes the
    host), so an href the EDITOR stored with a space —
    ``https://hospital.sharepoint.com/sites/Research Group/plan.docx``, which the
    toolbar's SAFE_SCHEME check lets through — came back as ``…/Research%20Group/…``
    and the kernel's section rewrite therefore re-targeted a link nobody touched
    (review 2026-10-26 H5). The serializer now writes such a destination in the
    ``<…>`` form, which is only a faithful round trip if the parser hands the
    bytes back unchanged.

    **Why control characters are the one exception.** markdown-it calls
    ``validateLink`` on the POST-normalize destination
    (``markdown_it/rules_inline/link.py``: ``href = state.md.normalizeLink(res.str)``
    then ``if state.md.validateLink(href)``), and ``validateLink``'s own docstring
    says the url "should be normalized at this point, and existing entities
    decoded" — its ``BAD_PROTO_RE`` is a plain
    ``^(vbscript|javascript|file|data):`` PREFIX match. The default
    ``normalizeLink`` ends in ``mdurl.encode``, and THAT percent-encode is what
    neutralised a tab-obfuscated scheme. A pure identity override removed the
    mitigation: ``parse_link_destination``'s ``<…>`` branch rejects only ``\\n``,
    ``<`` and an unescaped ``>``, so a literal TAB survives into the destination,
    and ``unescapeAll`` decodes ``&Tab;`` / ``&NewLine;`` into one even in the BARE
    form. ``BAD_PROTO_RE`` then does not match ``java\\tscript:``, the Link mark's
    ``renderHTML`` (``frontend/src/collab/schema.ts``) has NO protocol allowlist —
    ``SAFE_SCHEME`` gates only toolbar link CREATION, not hydration from git
    markdown — and a browser strips TAB/LF/CR before resolving the scheme, so
    ``javascript:alert(1)`` ran (review 2026-10-26 fix pass, IMPORTANT 1).

    Encoding ONLY ``[\\x00-\\x1f\\x7f]`` keeps the round trip intact: a space is
    ``\\x20`` and stays verbatim, and ``_link_destination`` already percent-encodes
    ``\\r``/``\\n`` on the way out, so both sides agree. A destination that really
    held another control character no longer round-trips, which the kernel guard
    refuses rather than rewrites.
    """
    return _LINK_CONTROL_RE.sub(lambda m: f"%{ord(m.group(0)):02X}", url)


def _make_parser() -> MarkdownIt:
    # html=False: `<TP53>`, `<COHORT_NAME>`, `<a.b@x.se>` are LITERAL TEXT, not
    # inline HTML to be dropped. With html=True markdown-it emits html_inline for
    # them and the walker discards it, silently deleting gene symbols and
    # placeholders from a grant proposal. The citation rule below is registered on
    # the inline ruler and is unaffected — the FULL marker still wins over the
    # generic parse (asserted by test_the_full_citation_marker_still_becomes_one_atom).
    md = MarkdownIt("commonmark", {"html": False}).enable(["strikethrough", "table"])
    md.normalizeLink = _verbatim_link  # type: ignore[method-assign]
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

    def distinct_marks() -> list[dict]:
        # CommonMark nests same-type emphasis (``*a *b* c*`` is em inside em)
        # and markdown-it emits both opens, but a ProseMirror node may carry
        # each mark once: the duplicate raised "Invalid collection of marks"
        # from node.check() and bricked the artifact on hydration (review
        # 2026-09-02 §2.3 class). The first occurrence wins.
        seen: set[tuple] = set()
        kept: list[dict] = []
        for m in marks:
            key = _mark_key(m)
            if key not in seen:
                seen.add(key)
                kept.append(dict(m))
        return kept

    def with_marks(node: dict) -> dict:
        if marks:
            node = dict(node)
            node["marks"] = distinct_marks()
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
            code_marks = distinct_marks() + [{"type": "code"}]
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
        # blockquote content is "block+". A bare ">" line — exactly what an
        # EMPTY editor blockquote (Tiptap: blockquote[paragraph()]) serializes
        # to — has no children and failed node.check(), which made every
        # rehydration of such a document raise (review 2026-09-02 §2.3).
        # Synthesize the empty paragraph, as the list-item rule below does;
        # the serializer emits ">" for it again, so the round-trip is idempotent.
        if not children:
            children = [{"type": "paragraph"}]
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


def _parse_blocks(md: str) -> list[dict]:
    """Walk the token stream into top-level PM blocks (no validation)."""
    tokens = _PARSER.parse(md)
    content: list[dict] = []
    i = 0
    while i < len(tokens):
        node, i = _block_to_pm(tokens, i)
        if node is not None:
            content.append(node)
    return content


def markdown_to_pm_blocks(md: str) -> list[dict]:
    """The top-level PM blocks *md* parses to — ``[]`` for a document with no
    representable block at all.

    Some real markdown is made ENTIRELY of constructs the token walker drops, and
    link-reference definitions are the common one: a file holding only
    ``[^1]: https://doi…`` produces zero blocks, ``doc`` requires ``block+``, and
    ``markdown_to_pm`` therefore raised ``ValueError: Invalid content for node
    doc`` — which made every room open on such a file fail (1011 reconnect loop)
    and ``canonical_artifact_markdown`` raise for every consumer (review
    2026-10-26, A6 LOW; a 20,000-input fuzz found 51 raises, all this one class).
    Callers that must not raise go through ``markdown_to_pm_or_empty``, which maps
    this ``[]`` onto the canonical empty doc. Validation still runs whenever there
    IS content, so ``markdown_to_pm``'s contract is unchanged."""
    content = _parse_blocks(md)
    if content:
        _validate(content)
    return content


def _validate(content: list[dict]) -> dict:
    """Validate a top-level block list and return it as a ``doc`` node.

    Two steps:
      1. ``node_from_json`` constructs the node, checking node TYPES and ATTRS
         (an unknown node type or a missing required attr raises here).
      2. ``.check()`` then verifies the CONTENT MODEL — that every node's
         children satisfy its content expression (e.g. doc holds block+,
         list_item holds "paragraph block*"). ``node_from_json`` alone does NOT
         enforce this, so without the check an out-of-schema arrangement of
         otherwise-valid nodes would slip through.
    """
    doc = {"type": "doc", "content": content}
    SCHEMA.node_from_json(doc).check()
    return doc


def markdown_to_pm(md: str) -> dict:
    """Parse *md* into a ProseMirror-JSON ``doc`` validated against ``SCHEMA``.

    Raises ``ValueError`` for markdown with no representable block (``doc`` is
    ``block+``) — blank input, and the link-reference-definition-only class
    described on :func:`markdown_to_pm_blocks`."""
    return _validate(_parse_blocks(md))


# ---------------------------------------------------------------------------
# ProseMirror JSON -> markdown
# ---------------------------------------------------------------------------

# Mark delimiters for the "wrap" style marks. Nesting ORDER is decided by
# ``_serialize_inline`` from how far each mark persists, never from the order
# the marks arrive in: Yjs stores marks as an unordered attr dict and the reader
# re-sorts them into schema order (link < em < strong), which used to put a
# short ``em`` OUTSIDE a longer ``strong`` and widen it on every checkpoint
# (review 2026-09-02 §2.2).
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

# Alternate delimiters, used ONLY when a mark is re-opened right after a close
# that ends in the same character. Two crossing ranges (``a`` bold, ``b`` bold
# and italic, ``c`` italic) must close italic+bold and re-open italic; with
# ``*`` for both that emits one ``****`` run, and CommonMark's rule of three
# then refuses to match the bold at all (``**a*b****c*`` parses as literal
# ``**``). ``_`` keeps the runs apart. It is not used for ordinary opens because
# ``_`` cannot open or close inside a word, where ``*`` can.
_ALT_DELIM = {
    "strong": ("__", "__"),
    "em": ("_", "_"),
}

# Characters that begin a markdown construct and so must be escaped when they
# start a block of plain text, plus a couple that are escaped inline. We keep
# this deliberately minimal: enough to keep our own serialized output stable and
# re-parseable, while NOT escaping "[" so bare ``[@key]`` markers stay literal.
_INLINE_ESCAPE = {"*": "\\*", "_": "\\_", "`": "\\`", "~": "\\~", "\\": "\\\\"}

# Plain text that LOOKS like markdown re-parses as markdown, and the text is
# gone: ``[x](y)`` becomes a link whose text is ``x``, ``<https://x.se>`` an
# autolink, and a line starting ``2024. Results`` an ordered list numbered
# 2024 (second-opinion round on the 2026-09-02 review; all reproduced). The
# checkpoint's dry-run reader cannot see any of these — they parse fine. So
# the serializer escapes the ONE character that turns each construct on, and
# nothing more, to keep already-correct documents byte-identical.
_AUTOLINK_RE = re.compile(
    r"<(?:[A-Za-z][A-Za-z0-9+.\-]{1,31}:[^\s<>]*|[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+)>"
)
# Constructs that are only recognised at the START of a line. ``*``, ``_``,
# ``~`` and the backtick are already escaped by the per-character pass, so a
# ``* `` bullet, a ``***`` rule, a ``___`` rule and a fence cannot occur here.
_BLOCK_START_RE = re.compile(
    r"""^(?:
        \#{1,6}(?=[ \t]|$)                      # ATX heading
      | >                                       # blockquote
      | [-+](?=[ \t]|$)                         # bullet
      | (?:-[ \t]*){3,}$                        # thematic break
      | (?:=+|-+)[ \t]*$                        # setext underline
      | \|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$   # table delimiter row
    )""",
    re.X,
)
_ORDERED_START_RE = re.compile(r"^(\d{1,9})([.)])(?=[ \t]|$)")
_REFDEF_START_RE = re.compile(r"^(\[[^\]]*\]):(?=[ \t]|$)")
# An entity-like run. CommonMark DECODES a recognised named/numeric entity, so
# the literal text ``&amp;`` a researcher typed came back as ``&`` and ``&copy;``
# as ``©`` — in a section nobody edited (review 2026-10-26 H5). Escaping the
# leading ``&`` keeps it literal. The pattern is deliberately WIDER than the HTML5
# entity table: over-escaping a non-entity like ``&foo;`` is harmless (``\&foo;``
# reads back as ``&foo;`` and re-serializes identically, so the round trip stays
# idempotent), while under-escaping loses characters.
_ENTITY_RE = re.compile(r"&(?:[A-Za-z][A-Za-z0-9]{1,31}|#[0-9]{1,8}|#[xX][0-9A-Fa-f]{1,6});")


def _escape_entity_runs(text: str) -> str:
    """Backslash-escape the ``&`` that opens every entity-like run in *text*.

    :func:`_escape_text` makes the same escape inside its char-by-char inline walk
    (it must know where it is in the text); :func:`_link_destination` and
    :func:`_link_title` escape a whole body at once and share this."""
    return _ENTITY_RE.sub(lambda m: "\\" + m.group(0), text)


def _escape_block_start(line: str) -> str:
    if _BLOCK_START_RE.match(line):
        return "\\" + line
    m = _ORDERED_START_RE.match(line)
    if m:
        return f"{m.group(1)}\\{m.group(2)}{line[m.end():]}"
    m = _REFDEF_START_RE.match(line)
    if m:
        return f"{m.group(1)}\\:{line[m.end():]}"
    return line


def _escape_text(text: str, *, at_line_start: bool = False) -> str:
    """Escape markdown-significant characters in plain text.

    We deliberately do NOT escape ``[`` so a bare ``[@x9]`` citekey marker
    survives as literal, UNESCAPED text (the bare-marker scan must still match
    it). ``*``/``_``/`` ` ``/``~``/``\\`` are escaped so emphasis-like runs in
    body text do not re-parse into marks on the next round-trip. ``](`` and an
    autolink's ``<`` are escaped so literal link syntax stays text, ``&`` is
    escaped when it begins an entity-like run (CommonMark would DECODE it), and a
    line-start construct (heading, bullet, ordered-list number, quote, rule,
    setext underline, table delimiter row, reference definition) is escaped at
    the start of every line inside the text and, when ``at_line_start``, at the
    start of the text itself.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "_":
            # CommonMark opens/closes `_` emphasis only at a word boundary, so an
            # underscore RUN with alphanumerics on both sides can never be emphasis
            # and must not be escaped: `sample\_id` on disk makes `grep sample_id`
            # return nothing, and `## Aims\_and\_Objectives` renames the section
            # whose heading is its identity. Verified against markdown-it: `a_b_c`
            # parses as a single text token, while `a*b*c` DOES parse as emphasis —
            # which is why the asterisk keeps its escape.
            j = i
            while j < n and text[j] == "_":
                j += 1
            before = text[i - 1] if i else ""
            after = text[j] if j < n else ""
            if before.isalnum() and after.isalnum():
                out.append(text[i:j])
                i = j
                continue
            out.append("\\_" * (j - i))
            i = j
            continue
        if ch == "&" and _ENTITY_RE.match(text, i):
            # An entity-like run would be DECODED on re-parse (``&amp;`` -> ``&``,
            # ``&copy;`` -> ``©``). Escape the one character that turns it on.
            out.append("\\&")
            i += 1
            continue
        out.append(_INLINE_ESCAPE.get(ch, ch))
        i += 1
    escaped = "".join(out).replace("](", "]\\(")
    escaped = _AUTOLINK_RE.sub(lambda m: "\\" + m.group(0), escaped)
    lines = escaped.split("\n")
    return "\n".join(
        _escape_block_start(line) if (k or at_line_start) else line for k, line in enumerate(lines)
    )


def _mark_key(mark: dict) -> tuple:
    """Value identity for a mark, so runs can be grouped and intersected."""
    attrs = mark.get("attrs") or {}
    return (mark["type"], tuple(sorted(attrs.items(), key=lambda kv: kv[0])))


def _persists_for(mark_key: tuple, nodes: list[dict], i: int) -> int:
    """How many of the nodes AFTER ``i`` carry ``mark_key`` without a gap."""
    n = 0
    for node in nodes[i + 1 :]:
        if mark_key not in {_mark_key(m) for m in node.get("marks", [])}:
            break
        n += 1
    return n


def _is_plain_text(node: dict) -> bool:
    """A text node whose whitespace may be moved across a delimiter: code-marked
    text is a verbatim span, so its whitespace is content."""
    return node["type"] == "text" and not any(m["type"] == "code" for m in node.get("marks", []))


def _serialize_inline(
    nodes: list[dict], *, single_line: bool = False, line_start: bool = True
) -> str:
    """Serialize an inline node list, opening/closing marks as the active set
    changes between adjacent nodes.

    **Nesting.** Marks are opened outermost-first by how far they PERSIST into
    the following nodes, so a mark that ends sooner always nests inside one
    that continues — whatever order the marks arrived in (Yjs stores them as an
    unordered attr dict; the reader's schema order put a short ``em`` OUTSIDE a
    longer ``strong`` and widened it on every checkpoint — review 2026-09-02
    §2.2). At a boundary, every active mark from the OUTERMOST one the next
    node lacks is closed, innermost first, and the still-wanted inner ones are
    re-opened. A close-and-re-open therefore happens only for genuinely
    crossing ranges.

    **Whitespace.** CommonMark will not pair a delimiter run that is followed
    by whitespace (as an opener) or preceded by it (as a closer), so
    ``**bold **tail`` is not bold at all: re-parsing yields four literal
    asterisks, which are then escaped, and the formatting is destroyed
    permanently in a document nobody edited. The same shape arrives from the
    CRDT — y-prosemirror drops marks on inline atoms, so a bold citation splits
    its run and strands ``**bold **`` behind it. Whitespace at the edge of a
    text node is therefore held back and written only once it is known what
    follows: OUTSIDE any delimiter that closes or opens at that boundary, INSIDE
    everything that stays open (``**a *b* c**``, not ``**a** *b* **c**``). This
    is decided here, after nesting, because a crossing re-open is a delimiter
    too — a pre-pass over the node list could not see it. Nodes without a
    fixed-delimiter mark at either edge come out byte-identical.

    ``single_line`` degrades ``hard_break`` (and a newline inside text) to a
    single space — a line break would split a table row or end a heading (spec
    §C; review 2026-09-02 §2.3). ``line_start=False`` says the text does not
    begin a line of the document (a heading's ``## `` or a cell's ``| `` comes
    first), so a leading ``1. `` or ``#`` is not a list or heading and must not
    be escaped: ``## 1. Aims`` is the section's identity and stays byte-identical."""
    out: list[str] = []
    active: list[dict] = []  # open marks, outermost first
    opened_with: list[tuple[str, str]] = []  # (open, close) each active mark used
    pend = ""  # edge whitespace held back until we know what follows it
    last_close = ""  # the close delimiter written last; "" once anything follows
    # line_start: nothing but whitespace written since the last newline (or, when
    # the caller says so, since a prefix such as "## " that is not text).

    def emit(s: str) -> None:
        nonlocal last_close, line_start
        if not s or (not out and not s.strip()):
            # Block-leading whitespace has no markdown form (the reader drops
            # it, and four spaces would make an indented code block), so the
            # canonical form omits it.
            return
        out.append(s)
        last_close = ""
        if s.endswith("\n"):
            line_start = True
        elif s.strip():
            line_start = False

    def close(pairs: list[tuple[dict, tuple[str, str]]]) -> None:
        nonlocal last_close, line_start
        for m, delim in pairs:
            out.append(_close_mark(m, delim))
            last_close = delim[1]
            line_start = False

    def transition(closes, held: str) -> None:
        # Held whitespace lands OUTSIDE a fixed delimiter closing here, and
        # stays INSIDE a link (its text is bracketed, not delimited).
        if any(m["type"] in _MARK_DELIM for m, _ in closes):
            close(closes)
            emit(held)
        else:
            emit(held)
            close(closes)

    for i, node in enumerate(nodes):
        node_marks = node.get("marks", [])
        node_keys = {_mark_key(m) for m in node_marks}
        cut = next((k for k, m in enumerate(active) if _mark_key(m) not in node_keys), None)
        closes = (
            list(zip(reversed(active[cut:]), reversed(opened_with[cut:]), strict=True))
            if cut is not None
            else []
        )
        transition(closes, pend)
        pend = ""
        if cut is not None:
            del active[cut:]
            del opened_with[cut:]

        lead = ""
        body = None
        if _is_plain_text(node):
            text = node["text"].replace("\n", " ") if single_line else node["text"]
            body = text.strip()
            if not body:
                pend = text  # whitespace-only: nothing to wrap, hold it back
                continue
            lead = text[: len(text) - len(text.lstrip())]
            pend = text[len(text.rstrip()) :]

        active_keys = {_mark_key(m) for m in active}
        reopened = {_mark_key(m) for m, _ in closes}
        to_open = [m for m in node_marks if _mark_key(m) not in active_keys]
        to_open.sort(
            key=lambda m: (-_persists_for(_mark_key(m), nodes, i), _MARK_RANK.get(m["type"], 99))
        )
        if any(m["type"] in _MARK_DELIM for m in to_open):
            emit(lead)  # outside the delimiter about to open, inside what is open
            lead = ""
        for m in to_open:
            delim = _MARK_DELIM.get(m["type"], ("", ""))
            if _mark_key(m) in reopened and _needs_alternate(delim, last_close):
                # A crossing re-open right after a same-character close: the two
                # runs would merge into one and CommonMark's rule of three would
                # refuse the match. ``_`` keeps them apart — unless the re-opened
                # span ends inside a word, where ``_`` cannot close either. That
                # shape has no markdown form at all: the span is emitted
                # unmarked (formatting lost on a few characters) rather than as
                # literal delimiters that would be escaped forever.
                alt = _ALT_DELIM.get(m["type"])
                if alt is None or _ends_inside_a_word(_mark_key(m), nodes, i):
                    continue
                delim = alt
            emit(_open_mark(m, delim))
            active.append(m)
            opened_with.append(delim)
        emit(lead)
        if body is not None:
            # Escaped at emission time: a line-start construct is only escaped
            # when the text really starts a line — ``**1. Aim**`` is not a list.
            emit(_escape_text(body, at_line_start=line_start))
        else:
            emit(_serialize_inline_atom(node, single_line=single_line))

    # Block-final whitespace has no markdown form either: the reader drops it,
    # so writing it would make the projection non-canonical.
    transition(list(zip(reversed(active), reversed(opened_with), strict=True)), "")
    return "".join(out)


# Tie-break for marks that persist equally far: schema order, outermost first —
# the same order the Yjs reader already produced, so unchanged documents stay
# byte-identical.
_MARK_RANK: dict[str, int] = {name: i for i, name in enumerate(SCHEMA.marks)}


def _needs_alternate(delim: tuple[str, str], last_close: str) -> bool:
    """True when opening with ``delim`` would extend the close delimiter written
    immediately before it into one longer run."""
    return bool(delim[0] and last_close and last_close[-1] == delim[0][0])


def _ends_inside_a_word(mark_key: tuple, nodes: list[dict], i: int) -> bool:
    """True when the run of ``mark_key`` starting at ``i`` is followed directly
    by a letter or digit — where ``_`` cannot act as a closing delimiter."""
    j = i + 1 + _persists_for(mark_key, nodes, i)
    if j >= len(nodes) or not _is_plain_text(nodes[j]):
        return False
    text = nodes[j]["text"]
    return bool(text) and text[0].isalnum()


def _open_mark(m: dict, delim: tuple[str, str]) -> str:
    if delim[0]:
        return delim[0]
    if m["type"] == "link":
        return "["
    return ""


# A bare (un-bracketed) CommonMark link destination may not start with "<" and
# may contain neither ASCII whitespace nor an ASCII control character; parens are
# allowed only when balanced. Anything else needs the ``<…>`` form.
_BARE_DEST_UNSAFE_RE = re.compile(r"[\x00-\x20\x7f<>]")


def _parens_balanced(dest: str) -> bool:
    depth = 0
    for ch in dest:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _link_destination(dest: str) -> str:
    """A CommonMark link/image destination that re-parses to *dest* EXACTLY.

    A bare destination silently re-targets the link whenever it holds a space or
    an unbalanced paren: the editor's toolbar accepts
    ``https://h.se/Research Group/x.docx`` (its SAFE_SCHEME check looks at the
    scheme, not inner spaces) and ``[plan](https://h.se/Research Group/x.docx)``
    re-parses as plain TEXT, while ``[wiki](https://h.se/a)b)`` re-parses as a
    link to ``https://h.se/a`` with ``b)`` left behind as text — in a section the
    user never edited (review 2026-10-26 H5).

    So: keep the bare form whenever it is faithful (which keeps every
    already-correct document byte-identical, balanced parens included), and
    otherwise use the ``<…>`` form. Inside ``<…>`` a backslash escape is honoured,
    so ``\\``, ``<`` and ``>`` are escaped; a line ending has no representation at
    all there and is percent-encoded. ``_verbatim_link`` is what makes the angle
    form a byte-faithful round trip — markdown-it would otherwise percent-encode
    the space back out of our reach.

    An entity-like run is escaped in BOTH forms: CommonMark runs ``unescapeAll``
    over a destination (reached from ``parse_link_destination``), so an href the
    editor stored as ``https://h.se/?a=1&amp;b=2`` — an HTML-escaped URL pasted
    out of a browser — came back as ``?a=1&b=2``. That failed CLOSED (the kernel
    guard refused the section), but it permanently blocked citing in a section
    holding a plausible real href. ``&`` is in markdown-it's ``ESCAPABLE`` set and
    a backslash escape is honoured in both destination forms (review 2026-10-26
    fix pass, Minor 5).
    """
    if dest and not _BARE_DEST_UNSAFE_RE.search(dest) and _parens_balanced(dest):
        return _escape_entity_runs(dest)
    body = (
        dest.replace("\\", "\\\\")
        .replace("<", "\\<")
        .replace(">", "\\>")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )
    return f"<{_escape_entity_runs(body)}>"


def _link_title(title: str) -> str:
    """A double-quoted CommonMark title that re-parses to *title* EXACTLY.

    An unescaped ``"`` ends the title early and the rest of the destination
    becomes literal text — the whole link is destroyed (review 2026-10-26 H5;
    reference corpus #57). A trailing backslash did the same by escaping the
    closing quote.

    A newline is flattened to a space, as an image title already was. A
    CommonMark title MAY span lines, so a title holding ``"a\n## Fake heading"``
    put a ``## `` at the start of a line and ``split_sections`` then saw TWO
    sections inside one paragraph — a phantom boundary that the sectionizer, the
    kernel's section map and the marker insert all had to be defended against
    separately. Flattening removes the class at the source; a title that really
    held a newline no longer round-trips, which the kernel guard refuses.

    An entity-like run is escaped for the same reason as in ``_link_destination``:
    ``parseLinkTitle`` runs ``unescapeAll`` over the title, so a literal
    ``&amp;`` in a title came back as ``&`` (review 2026-10-26 fix pass,
    Minor 5)."""
    escaped = title.replace("\n", " ").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{_escape_entity_runs(escaped)}"'


def _close_mark(m: dict, delim: tuple[str, str]) -> str:
    if delim[1]:
        return delim[1]
    if m["type"] == "link":
        attrs = m.get("attrs", {})
        dest = _link_destination(attrs.get("href", ""))
        title = attrs.get("title")
        if title:
            return f"]({dest} {_link_title(title)})"
        return f"]({dest})"
    return ""


def _longest_backtick_run(text: str) -> int:
    longest = 0
    run = 0
    for ch in text:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return longest


def _wrap_inline_code(text: str) -> str:
    """Wrap *text* as a CommonMark inline code span with a backtick-safe delimiter.

    The delimiter is a run of backticks ONE LONGER than the longest run of
    backticks appearing in *text*, so a backtick inside the code never collides
    with the delimiter (the code body is otherwise emitted verbatim — code spans
    have no backslash escaping). If the body begins or ends with a backtick (or
    is entirely backticks/whitespace), a single space is padded on each side so
    the leading/trailing backtick is not eaten as part of the delimiter; one of
    those padding spaces is stripped again on parse, leaving the body intact and
    keeping the round-trip idempotent. A body with a space at BOTH ends gets the
    same one-space padding, because CommonMark strips one space from each end
    of such a span.
    """
    fence = "`" * (_longest_backtick_run(text) + 1)
    # CommonMark converts a line ending inside a code span to a space — and the
    # block parser runs first, so a continuation line starting with "## " would
    # END the span and start a heading. Write the space the reader would produce.
    body = text.replace("\n", " ")
    if body.startswith("`") or body.endswith("`") or body.strip("`") == "":
        body = f" {body} "
    elif body.startswith(" ") and body.endswith(" ") and body.strip():
        # CommonMark strips ONE space from each end of a code span whose body
        # starts and ends with a space (and is not all spaces) — so `` ` a ` ``
        # re-read as ``a``. Pad once more; the reader strips it back off.
        body = f" {body} "
    return f"{fence}{body}{fence}"


def _serialize_inline_atom(node: dict, *, single_line: bool = False) -> str:
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
        # In a table cell a "\"-newline would split the row, and in a heading
        # it would END the heading and rename the section: degrade to a single
        # space (spec §C; review 2026-09-02 §2.3). Elsewhere, unchanged.
        return " " if single_line else "\\\n"
    if ntype == "image":
        attrs = node.get("attrs", {})
        alt = (attrs.get("alt") or "").replace("\n", " ")
        src = _link_destination(attrs.get("src", ""))
        title = attrs.get("title")
        if title:
            return f"![{alt}]({src} {_link_title(title)})"
        return f"![{alt}]({src})"
    return ""


def _serialize_block(node: dict, *, alt: bool = False) -> str:
    """Serialize one block node to markdown text (no trailing block separator).

    ``alt`` is only meaningful for a list: see :func:`_alternate_adjacent_lists`.
    """
    ntype = node["type"]
    if ntype == "paragraph":
        return _serialize_inline(_without_block_final_breaks(node.get("content", [])))
    if ntype == "heading":
        level = node.get("attrs", {}).get("level", 1)
        body = _serialize_inline(
            _without_block_final_breaks(node.get("content", [])),
            single_line=True,
            line_start=False,
        )
        return f"{'#' * level} {body}"
    if ntype == "code_block":
        body = _code_block_text(node)
        # The fence must be longer than any backtick run in the body, or a
        # "```" line inside the code closes the block early and every later
        # section is swallowed into a new one (review 2026-09-02 §2.3).
        fence = "`" * max(3, _longest_backtick_run(body) + 1)
        return f"{fence}\n{body}\n{fence}" if body else f"{fence}\n{fence}"
    if ntype == "horizontal_rule":
        return "---"
    if ntype == "blockquote":
        inner = _serialize_blocks(node.get("content", []))
        return "\n".join("> " + line if line else ">" for line in inner.split("\n"))
    if ntype in ("bullet_list", "ordered_list"):
        return _serialize_list(node, alt=alt)
    if ntype == "table":
        return _serialize_table(node)
    return ""


def _code_block_text(node: dict) -> str:
    content = node.get("content", [])
    return "".join(c.get("text", "") for c in content)


def _without_block_final_breaks(nodes: list[dict]) -> list[dict]:
    """A ``hard_break`` that ends a block has no markdown form: ``\\`` before a
    block boundary is a LITERAL backslash on re-parse (review 2026-09-02 §2.3).
    It carries no text, so dropping it loses nothing the reader can see. A
    whitespace-only text node after it is block-final too — the reader drops
    block-final whitespace — so it must not hide the break (second-opinion
    finding: ``line\\\n   `` followed by a blank line kept the backslash)."""
    end = len(nodes)
    while end:
        node = nodes[end - 1]
        if node.get("type") == "hard_break" or (
            _is_plain_text(node) and not node.get("text", "").strip()
        ):
            end -= 1
        else:
            break
    return nodes[:end]


# CommonMark starts a NEW list when the marker character changes, so two
# adjacent same-type lists are kept apart by alternating the delimiter. The pair
# is positional, so ``markdown_to_pm`` (which has no delimiter attr to lose) and
# the next serialization agree and the round trip stays idempotent.
_ORDERED_DELIMS = (".", ")")
_BULLET_DELIMS = ("-", "*")


def _alternate_adjacent_lists(blocks: list[dict]) -> list[bool]:
    """Per block, whether it must use the ALTERNATE list delimiter.

    Two adjacent lists of the same PM type serialize to the same marker
    character, and CommonMark then reads them as ONE list — ``1. a`` + ``1. b``
    came back as a single two-item list, renumbered, and ``- a`` / ``* b`` /
    ``+ c`` collapsed three lists into one (review 2026-10-26 H5; reference
    corpus #17, which was also NOT idempotent). Alternating within each run of
    consecutive same-type lists keeps every boundary visible. Blocks of any other
    type reset the run, because a paragraph (or heading, or table) between two
    lists already separates them.
    """
    flags = [False] * len(blocks)
    prev_list_type: str | None = None
    for i, block in enumerate(blocks):
        btype = block["type"] if block["type"] in ("bullet_list", "ordered_list") else None
        if btype is not None and btype == prev_list_type:
            flags[i] = not flags[i - 1]
        prev_list_type = btype
    return flags


def _list_interrupts_a_paragraph(block: dict) -> bool:
    """True when this list can follow a paragraph with NO blank line between them.

    CommonMark lets a list interrupt a paragraph only if an ordered list starts
    at 1. ``- Aims:`` + ``  3. third aim`` therefore folded the nested list into
    the paragraph as a soft line break, so ``Aims:\n3. third aim`` became one
    paragraph and the list was gone (review 2026-10-26 H5)."""
    if block["type"] != "ordered_list":
        return True
    return block.get("attrs", {}).get("order", 1) == 1


def _serialize_list(node: dict, *, alt: bool = False) -> str:
    """Serialize a bullet/ordered list. Items are separated by single newlines
    (tight list); nested blocks are indented by two spaces."""
    ordered = node["type"] == "ordered_list"
    start = node.get("attrs", {}).get("order", 1) if ordered else None
    delim = (_ORDERED_DELIMS if ordered else _BULLET_DELIMS)[1 if alt else 0]
    lines: list[str] = []
    for idx, item in enumerate(node.get("content", [])):
        marker = f"{start + idx}{delim} " if ordered else f"{delim} "
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


def _cell_text(cell: dict) -> str:
    """Flatten one cell to single-line, pipe-safe inline markdown.

    Cells hold one paragraph by schema; anything else (hostile injection —
    the Yjs read path is unvalidated) degrades: each block's INLINE content
    serializes in cell mode and the pieces join with single spaces — degrade,
    never drop, never raise (spec §C). The trailing replaces are uniform and
    deliberate: GFM splits rows on unescaped pipes BEFORE inline parsing, so
    EVERY literal "|" — plain text, code-span bodies, link targets — must
    leave here as "\\|" (GFM unescapes it at row level on re-parse), and any
    stray newline (softbreak text) must flatten to a space.
    """
    parts: list[str] = []
    for block in cell.get("content", []):
        text = _serialize_inline(
            _without_block_final_breaks(block.get("content", [])),
            single_line=True,
            line_start=False,
        )
        if text:
            parts.append(text)
    return " ".join(parts).replace("\n", " ").replace("|", "\\|")


def _row_line(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


# GFM tables are narrow; colspan reaches us via the unvalidated Yjs read path,
# so we clamp it to a sane maximum to bound the emitted-cell count (spec §C).
_MAX_COLSPAN = 64


def _serialize_table(node: dict) -> str:
    """Strict-GFM table serialization — the table loose-form law (spec §C).

    Always bordered (every line starts "| " — the sectionizer-safety
    invariant); row 1 is the header REGARDLESS of cell types; column count is
    the header width after colspan expansion; body rows pad/truncate to it;
    colspan > 1 degrades to content + empty filler cells; rowspan is ignored
    (spanned-over cells are absent in the PM shape — padding fills the gap).
    """
    rows = node.get("content", [])
    if not rows:
        return ""

    def expand(row: dict) -> list[str]:
        cells: list[str] = []
        for cell in row.get("content", []):
            cells.append(_cell_text(cell))
            # colspan arrives via the UNVALIDATED Yjs read path (spec §5): a
            # hostile client can inject a non-int or a huge int. Coerce
            # defensively and clamp — degrade, never raise, never amplify
            # (spec §C). GFM tables are narrow; a small cap is ample.
            try:
                span = int(cell.get("attrs", {}).get("colspan", 1))
            except (TypeError, ValueError):
                span = 1
            span = max(1, min(span, _MAX_COLSPAN))
            cells.extend([""] * (span - 1))
        return cells

    header = expand(rows[0])
    width = max(1, len(header))

    def fit(cells: list[str]) -> list[str]:
        cells = cells[:width]
        return cells + [""] * (width - len(cells))

    lines = [_row_line(fit(header)), _row_line(["---"] * width)]
    for row in rows[1:]:
        lines.append(_row_line(fit(expand(row))))
    return "\n".join(lines)


def _serialize_item_blocks(blocks: list[dict]) -> str:
    """Serialize the blocks inside a single list item.

    Consecutive blocks in a list item need a blank line between them to remain
    distinct on re-parse — without it CommonMark folds a following paragraph
    into the previous one as a soft line-break (silent content loss, since
    ``list_item`` content is ``paragraph block*``). The ONE exception is a
    following list: a list marker interrupts a paragraph on its own, so it stays
    tight — which also keeps single-paragraph + nested-list items byte-identical.
    An empty leading paragraph followed by more blocks is dropped (the
    companion to the parser's leading-paragraph synthesis, spec §B) so the
    real first block occupies the marker line instead.
    """
    # Companion to the parser's leading-paragraph synthesis (spec §B): an EMPTY
    # leading paragraph followed by more blocks is dropped, so the real first
    # block occupies the marker line ("- > q", "- | a | b |"). Emitting it
    # would place a blank line between the list marker and the first block —
    # CommonMark allows at most ONE blank line at an item start, so the item
    # would terminate empty and the following block would ESCAPE the list on
    # re-parse. Dropping it here + re-synthesizing on parse = idempotent.
    # (An item that is ONLY an empty paragraph keeps it: "- " re-parses as an
    # empty item.)
    if len(blocks) > 1 and blocks[0]["type"] == "paragraph" and not blocks[0].get("content"):
        blocks = blocks[1:]
    parts: list[str] = []
    alts = _alternate_adjacent_lists(blocks)
    for i, b in enumerate(blocks):
        is_list = b["type"] in ("bullet_list", "ordered_list")
        tight = is_list and _list_interrupts_a_paragraph(b)
        if i > 0 and not tight:
            parts.append("")  # blank-line separator -> distinct block on re-parse
        parts.append(_serialize_block(b, alt=alts[i]))
    return "\n".join(parts)


def _serialize_blocks(blocks: list[dict]) -> str:
    """Serialize a sequence of top-level (or blockquote) blocks, separating
    them with a single blank line. No trailing newline."""
    alts = _alternate_adjacent_lists(blocks)
    return "\n\n".join(_serialize_block(b, alt=a) for b, a in zip(blocks, alts, strict=True))


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
