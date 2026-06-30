"""Task 6: Full end-to-end pipeline tests.

Tests:
  1. JS and Python corpora cannot drift — both sides must define the same
     fixture names with the same markdown strings.
  2. The full md→PM→Yjs→PM→md pipeline round-trips cleanly (normalised).
  3. Byte-identical raw fidelity against every y-prosemirror-produced golden —
     no canonicalization, the strongest possible claim.
"""

import base64
import json
import pathlib

import pycrdt

from call_manager.proposal_assist.collab.markdown_bridge import (
    markdown_to_pm,
    pm_to_markdown,
)
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc, ydoc_to_pm_json

from .fixtures import CORPUS

_GOLDENS = pathlib.Path(__file__).parent / "goldens"


def test_js_and_python_corpora_match():
    py = {name: md for name, md in CORPUS}
    js = {p.stem: json.loads(p.read_text())["markdown"] for p in _GOLDENS.glob("*.json")}
    assert py == js  # same names, same markdown on both sides


def test_md_to_yjs_to_md_is_canonical():
    for name, md in CORPUS:
        norm = pm_to_markdown(markdown_to_pm(md))
        ydoc = pm_json_to_ydoc(markdown_to_pm(md))
        back = pm_to_markdown(ydoc_to_pm_json(ydoc))
        assert back == norm, name


def test_fidelity_byte_identical_raw():
    """Decodes ydoc_update_b64 into a pycrdt.Doc and asserts ydoc_to_pm_json()
    equals the JS pm_json with NO canonicalization.  This locks in the
    strongest fidelity claim: the Python reader produces bit-for-bit the same
    ProseMirror JSON that y-prosemirror emitted.  If a golden does NOT pass
    raw, that is a real fidelity gap — do not delete this assertion, surface
    it.
    """
    for name, _ in CORPUS:
        g = json.loads((_GOLDENS / f"{name}.json").read_text())
        ydoc = pycrdt.Doc()
        ydoc.apply_update(base64.b64decode(g["ydoc_update_b64"]))
        assert ydoc_to_pm_json(ydoc) == g["pm_json"], name
