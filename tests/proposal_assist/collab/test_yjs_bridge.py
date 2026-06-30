import base64
import json
import pathlib

import pycrdt

from call_manager.proposal_assist.collab.yjs_bridge import (
    pm_json_to_ydoc,
    ydoc_to_pm_json,
)

from .fixtures import CORPUS

GOLDENS = pathlib.Path(__file__).parent / "goldens"


def _golden(name):
    return json.loads((GOLDENS / f"{name}.json").read_text())


def _canonical(node):
    """Normalise REPRESENTATION DIFFERENCES ONLY between two ProseMirror-JSON trees.

    The golden ``pm_json`` is produced by ``y-prosemirror`` and follows Yjs
    serialization conventions; our reader produces the same logical tree but may
    differ in two purely-representational ways:

      * a mark or node may carry ``attrs: {}`` on one side and omit ``attrs``
        on the other (absent == empty), and
      * an attr whose value is ``null`` may be present on one side and omitted
        on the other (Yjs omits null-valued attrs from the encoding).

    Both differences are normalised away by:
      * dropping ``attrs`` entirely when it is empty after the next step, and
      * dropping individual attr keys whose value is ``None``.

    It NORMALISES NOTHING ELSE. Specifically it preserves, and therefore will
    still surface as a real diff:
      * ``type`` (node and mark),
      * ``text``,
      * every attr key with a NON-null value (and its exact value),
      * the presence/absence/order of marks,
      * the order and identity of children.
    Order is preserved because lists are rebuilt element-wise in place; mark
    lists are likewise preserved in order (not sorted), so a reordered or
    missing mark is NOT hidden.
    """
    if isinstance(node, list):
        return [_canonical(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key == "attrs":
            # Drop null-valued attr keys; treat the whole attrs as absent if empty.
            cleaned = {k: v for k, v in value.items() if v is not None}
            if cleaned:
                out["attrs"] = cleaned
            continue
        if key == "marks":
            # Recurse into each mark (to canonicalise its attrs); preserve order.
            out["marks"] = [_canonical(m) for m in value]
            continue
        if key == "content":
            out["content"] = [_canonical(c) for c in value]
            continue
        out[key] = _canonical(value)
    return out


def test_pm_yjs_roundtrip_stable():
    # PM -> Yjs (pycrdt) -> PM is identity for our own output.
    for name, _ in CORPUS:
        pm = _golden(name)["pm_json"]
        ydoc = pm_json_to_ydoc(pm)
        assert _canonical(ydoc_to_pm_json(ydoc)) == _canonical(pm), name


def test_fidelity_against_y_prosemirror():
    # THE CRITICAL GATE: reading a y-prosemirror-produced document in Python
    # yields exactly the JS ProseMirror JSON.
    for name, _ in CORPUS:
        g = _golden(name)
        ydoc = pycrdt.Doc()
        ydoc.apply_update(base64.b64decode(g["ydoc_update_b64"]))
        assert _canonical(ydoc_to_pm_json(ydoc)) == _canonical(g["pm_json"]), name
