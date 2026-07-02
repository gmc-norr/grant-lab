"""Pure-Python bridge between ProseMirror JSON and a Yjs ``XmlFragment``.

This is the Python analogue of ``y-prosemirror``'s ``prosemirrorJSONToYDoc`` /
``yXmlFragmentToProsemirrorJSON``. It lets the server read and write the same
``pycrdt.Doc`` that a browser collaborates on via ``y-prosemirror``.

OBSERVED REFERENCE MAPPING (inspect-first; derived from the Task-4 goldens that
were produced + self-verified by REAL ``y-prosemirror``, read back through
``ydoc.get("prosemirror", type=pycrdt.XmlFragment)``):

* The shared root is an ``XmlFragment`` registered under the key
  ``"prosemirror"``. Its children are the document's top-level block nodes;
  i.e. it stands in for the PM ``doc`` node (the ``doc`` wrapper itself is NOT
  encoded as an element).

* Every non-text ProseMirror node -> an ``XmlElement`` whose ``tag`` is the PM
  node ``type``. The PM node ``attrs`` become the element's Yjs attributes.
    - Block containers (paragraph, heading, blockquote, code_block, the list
      nodes, list_item) carry their children as element children.
    - Leaf / atom nodes (horizontal_rule, hard_break, image, citation) are
      childless elements; their content is fully described by their attributes.
    - ``citation`` is an INLINE ATOM: it appears as an ``XmlElement`` child
      *inside* the paragraph, interleaved with ``XmlText`` siblings, with
      attributes ``citekey`` and ``ev_id``. ``image``/``hard_break`` likewise
      appear inline inside the paragraph.

* Text + marks -> a single ``XmlText`` per text-containing block, whose
  ``.diff()`` returns ``[(chunk_text, attrs_or_None), ...]``. Each *mark* is a
  formatting-attribute KEY on the chunk; its value is the mark's ``attrs``
  dict (``{}`` for attr-less marks like strong/em/code/strikethrough;
  ``{"href": ..., "title": null}`` for ``link``). ``None`` formatting == an
  unmarked chunk. Adjacent chunks with identical formatting are coalesced by
  Yjs, exactly as PM merges adjacent identically-marked text nodes.

YJS SERIALIZATION CONVENTIONS observed (handled here + by ``_canonical`` in the
tests):
* Numeric node attrs (``heading.level``, ``ordered_list.order``) come back from
  pycrdt as floats (``2.0``, ``1.0``); PM JSON uses ints. We coerce integral
  floats back to ``int``.
* Null-valued attrs are OMITTED from the Yjs encoding (e.g. ``image`` stores
  only ``src``/``alt``, never ``title: null``). We therefore never emit a
  null attr we did not read, and the test ``_canonical`` drops null attrs on
  both sides so a faithfully-omitted null is not a spurious diff.
* Yjs map attribute *iteration order* is not significant (it can differ from PM
  JSON order); we always rebuild attrs from the schema-declared key order so
  output is deterministic.
"""

from __future__ import annotations

from typing import Any

import pycrdt

from call_manager.proposal_assist.editing.sectionizer import is_h2_heading_line

from .markdown_bridge import pm_to_markdown
from .schema import SCHEMA

_FRAGMENT_KEY = "prosemirror"

# Mark types in a fixed, deterministic order (schema declaration order). Used to
# order the marks emitted for a text chunk that carries more than one mark.
_MARK_ORDER: dict[str, int] = {name: i for i, name in enumerate(SCHEMA.marks)}


# ---------------------------------------------------------------------------
# attr helpers
# ---------------------------------------------------------------------------


def _coerce_scalar(value: Any) -> Any:
    """Undo Yjs' float widening of integral numbers (``2.0`` -> ``2``)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _node_attrs_for_yjs(node_type: str, attrs: dict[str, Any]) -> dict[str, str | int | float]:
    """PM node attrs -> Yjs element attributes (drop null, keep declared keys)."""
    spec = SCHEMA.nodes.get(node_type)
    declared = list(spec.attrs.keys()) if spec is not None else list(attrs.keys())
    out: dict[str, Any] = {}
    for key in declared:
        if key in attrs and attrs[key] is not None:
            out[key] = attrs[key]
    # Preserve any extra non-null attrs not declared in the schema (defensive).
    for key, value in attrs.items():
        if key not in out and value is not None:
            out[key] = value
    return out


def _read_node_attrs(node_type: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Yjs element attributes -> PM node attrs (coerce ints, schema key order)."""
    spec = SCHEMA.nodes.get(node_type)
    coerced = {k: _coerce_scalar(v) for k, v in raw.items()}
    if spec is None:
        return coerced
    out: dict[str, Any] = {}
    for key in spec.attrs:
        if key in coerced:
            out[key] = coerced[key]
    for key, value in coerced.items():
        if key not in out:
            out[key] = value
    return out


def _mark_attrs_to_yjs_value(mark: dict[str, Any]) -> dict[str, Any]:
    """A PM mark -> the Yjs formatting-attribute value (its attrs dict)."""
    attrs = mark.get("attrs") or {}
    # y-prosemirror stores the mark's attrs verbatim (including null-valued keys
    # such as link.title); the goldens show ``{"href":..,"title":null}``.
    return dict(attrs)


# ---------------------------------------------------------------------------
# PM JSON -> Yjs
# ---------------------------------------------------------------------------


def _is_text_node(node: dict[str, Any]) -> bool:
    return node.get("type") == "text"


def _marks_to_format(marks: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not marks:
        return None
    fmt: dict[str, Any] = {}
    for mark in marks:
        fmt[mark["type"]] = _mark_attrs_to_yjs_value(mark)
    return fmt


def _append_inline_content(
    container: pycrdt.XmlElement, content: list[dict[str, Any]]
) -> None:
    """Populate an *integrated* text-containing block element from PM inline content.

    Text nodes are written into a shared ``XmlText`` (so Yjs coalesces adjacent
    identically-marked runs); inline atoms (citation/image/hard_break) become
    childless ``XmlElement`` siblings. ``container`` must already be integrated
    into the doc (so ``children.append`` works).
    """
    pending_text: pycrdt.XmlText | None = None

    def flush_text() -> pycrdt.XmlText:
        nonlocal pending_text
        if pending_text is None:
            pending_text = container.children.append(pycrdt.XmlText())
        return pending_text

    for child in content:
        if _is_text_node(child):
            xtext = flush_text()
            fmt = _marks_to_format(child.get("marks"))
            xtext.insert(len(xtext), child.get("text", ""), fmt)
        else:
            # An inline atom breaks the text run: subsequent text starts a new
            # XmlText sibling (matching the y-prosemirror element layout).
            pending_text = None
            _append_node(container, child)


def _append_node(
    parent: pycrdt.XmlFragment | pycrdt.XmlElement, node: dict[str, Any]
) -> pycrdt.XmlElement:
    """Append PM ``node`` as a child of the already-integrated ``parent``.

    Appends the (childless) element first so it is integrated into the doc, then
    populates its children — pycrdt requires a node to be integrated before its
    own ``children.append`` can be used.
    """
    node_type = node["type"]
    attrs = _node_attrs_for_yjs(node_type, node.get("attrs") or {})
    elem = parent.children.append(pycrdt.XmlElement(node_type, dict(attrs)))
    content = node.get("content") or []
    if content and _block_holds_inline(node_type, content):
        _append_inline_content(elem, content)
    else:
        for child in content:
            _append_node(elem, child)
    return elem


def _insert_node(
    parent: pycrdt.XmlFragment | pycrdt.XmlElement, index: int, node: dict[str, Any]
) -> pycrdt.XmlElement:
    """Insert PM ``node`` as a child of ``parent`` at ``index`` (positional sibling
    of ``_append_node``). ``children.insert`` integrates the element, so its own
    children can then be populated identically to the append path."""
    node_type = node["type"]
    attrs = _node_attrs_for_yjs(node_type, node.get("attrs") or {})
    elem = parent.children.insert(index, pycrdt.XmlElement(node_type, dict(attrs)))
    content = node.get("content") or []
    if content and _block_holds_inline(node_type, content):
        _append_inline_content(elem, content)
    else:
        for child in content:
            _append_node(elem, child)
    return elem


def _block_holds_inline(node_type: str, content: list[dict[str, Any]]) -> bool:
    """True when this block's content is inline (text / inline atoms)."""
    spec = SCHEMA.nodes.get(node_type)
    if spec is not None and spec.is_textblock:
        return True
    # Fallback: any text node present means inline content.
    return any(
        c.get("type") == "text" or _is_inline_atom(c.get("type")) for c in content
    )


def _is_inline_atom(node_type: str | None) -> bool:
    if node_type is None:
        return False
    spec = SCHEMA.nodes.get(node_type)
    return bool(spec is not None and spec.is_inline and not spec.is_text)


def pm_json_to_ydoc(doc: dict, *, client_id: int | None = None) -> pycrdt.Doc:
    """Build a ``pycrdt.Doc`` whose ``"prosemirror"`` XmlFragment mirrors ``doc``.

    ``doc`` is a ProseMirror ``doc`` node (``{"type": "doc", "content": [...]}``).

    ``client_id`` (when given) is forwarded to ``pycrdt.Doc`` so callers can pin
    the doc's client id. This makes the *logical* CRDT content reproducible; note
    that it does NOT make ``get_update()`` byte-stable across builds (pycrdt's
    update serialization is not byte-deterministic even with a fixed client id —
    see the write-path test), so callers must not rely on byte equality.
    """
    if doc.get("type") != "doc":
        raise ValueError(f"expected a ProseMirror doc node, got type={doc.get('type')!r}")
    ydoc = pycrdt.Doc() if client_id is None else pycrdt.Doc(client_id=client_id)
    fragment = ydoc.get(_FRAGMENT_KEY, type=pycrdt.XmlFragment)
    with ydoc.transaction():
        for block in doc.get("content") or []:
            _append_node(fragment, block)
    return ydoc


def apply_pm_json_to_fragment(doc: pycrdt.Doc, pm_json: dict) -> bytes:
    """Replace the doc's ``"prosemirror"`` XmlFragment content with ``pm_json``
    (a ProseMirror ``doc`` node) in one transaction, returning the INCREMENTAL
    Yjs update that effects the change.

    Used by the room-aware write kernel to apply a whole-doc replacement (the
    section-scoped edit reassembled into the full doc) to a LIVE room: a client
    synced to the pre-edit state converges by applying this update. The update
    is captured via an observer (NOT ``get_update()``, which encodes full state).
    """
    if pm_json.get("type") != "doc":
        raise ValueError(f"expected a ProseMirror doc node, got type={pm_json.get('type')!r}")
    captured: list[bytes] = []
    sub = doc.observe(lambda event: captured.append(event.update))
    try:
        fragment = doc.get(_FRAGMENT_KEY, type=pycrdt.XmlFragment)
        with doc.transaction():
            while len(fragment.children) > 0:
                del fragment.children[0]
            for block in pm_json.get("content") or []:
                _append_node(fragment, block)
    finally:
        doc.unobserve(sub)
    # One transaction => one observer callback with the coalesced incremental update.
    return captured[-1] if captured else b""


def _block_starts_section(block_json: dict[str, Any]) -> bool:
    """True if this top-level block begins a section, using the SAME rule as
    split_sections: is_h2_heading_line on the block's serialized first line.
    A node-type (heading level-2) check would diverge on `##NoSpace` paragraphs."""
    first_line = pm_to_markdown({"type": "doc", "content": [block_json]}).split("\n", 1)[0]
    return is_h2_heading_line(first_line)


def apply_pm_section_to_fragment(
    doc: pycrdt.Doc, *, section_index: int, blocks: list[dict[str, Any]]
) -> bytes:
    """Replace ONLY ``section_index``'s top-level block range in the ``prosemirror``
    XmlFragment with ``blocks`` (the new section's PM blocks), preserving every
    other section's Yjs identity + any concurrent client edits. Returns the
    incremental Yjs update (captured via observer), or ``b""`` if nothing changed.
    Safe because the kernel's staleness gate proved the target section unchanged
    since the proposer read it.
    """
    captured: list[bytes] = []
    sub = doc.observe(lambda event: captured.append(event.update))
    try:
        fragment = doc.get(_FRAGMENT_KEY, type=pycrdt.XmlFragment)
        blocks_json = ydoc_to_pm_json(doc).get("content") or []
        # Section boundaries via the SAME rule as split_sections. The preamble
        # (section 0) is ALWAYS present — split_sections emits Section 0 even when
        # empty — so section k spans [starts[k], starts[k+1]). NO dedupe: a
        # no-preamble doc yields starts=[0, 0, ...] so section 0 is the empty range
        # [0, 0) and every index aligns with the split-numbered pc.section_index the
        # kernel passes (dropping the leading 0 shifts every section — silent
        # wrong-section corruption).
        starts = [0] + [i for i, b in enumerate(blocks_json) if _block_starts_section(b)]
        if not 0 <= section_index < len(starts):
            raise IndexError(
                f"section_index {section_index} out of range ({len(starts)} sections)"
            )
        start = starts[section_index]
        end = starts[section_index + 1] if section_index + 1 < len(starts) else len(blocks_json)
        with doc.transaction():
            for i in range(end - 1, start - 1, -1):   # delete range back-to-front
                del fragment.children[i]
            for offset, block in enumerate(blocks):    # insert new blocks at `start`
                _insert_node(fragment, start + offset, block)
    finally:
        doc.unobserve(sub)
    return captured[-1] if captured else b""


# ---------------------------------------------------------------------------
# Yjs -> PM JSON
# ---------------------------------------------------------------------------


def _format_to_marks(attrs: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    if not attrs:
        return None
    marks: list[dict[str, Any]] = []
    for mark_type in sorted(attrs, key=lambda k: _MARK_ORDER.get(k, len(_MARK_ORDER))):
        value = attrs[mark_type]
        mark_attrs = dict(value) if isinstance(value, dict) else {}
        marks.append({"type": mark_type, "attrs": mark_attrs})
    return marks


def _read_xml_text(xtext: pycrdt.XmlText) -> list[dict[str, Any]]:
    """An ``XmlText`` -> a list of PM ``text`` nodes (one per formatted chunk)."""
    nodes: list[dict[str, Any]] = []
    for chunk, attrs in xtext.diff():
        if not isinstance(chunk, str):
            # Defensive: embeds inside XmlText are not part of our mapping
            # (atoms are sibling XmlElements). Skip non-string chunks.
            continue
        node: dict[str, Any] = {"type": "text", "text": chunk}
        marks = _format_to_marks(attrs)
        if marks is not None:
            node["marks"] = marks
        nodes.append(node)
    return nodes


def _read_element(elem: pycrdt.XmlElement) -> dict[str, Any]:
    node_type = elem.tag
    raw_attrs = {k: v for k, v in elem.attributes}
    node: dict[str, Any] = {"type": node_type}
    attrs = _read_node_attrs(node_type, raw_attrs)
    if attrs:
        node["attrs"] = attrs
    content: list[dict[str, Any]] = []
    for child in elem.children:
        if isinstance(child, pycrdt.XmlText):
            content.extend(_read_xml_text(child))
        elif isinstance(child, pycrdt.XmlElement):
            content.append(_read_element(child))
    if content:
        node["content"] = content
    return node


def ydoc_to_pm_json(ydoc: pycrdt.Doc) -> dict:
    """Read the ``"prosemirror"`` XmlFragment of ``ydoc`` back to PM ``doc`` JSON."""
    fragment = ydoc.get(_FRAGMENT_KEY, type=pycrdt.XmlFragment)
    content: list[dict[str, Any]] = []
    with ydoc.transaction():
        for child in fragment.children:
            if isinstance(child, pycrdt.XmlElement):
                content.append(_read_element(child))
            elif isinstance(child, pycrdt.XmlText):
                # Top-level text is not part of our schema (doc holds blocks),
                # but surface it rather than silently drop, to expose surprises.
                content.extend(_read_xml_text(child))
    return {"type": "doc", "content": content}
