"""Cross-runtime write-path validation (Python producer half).

This closes the Slice-0 spike deferral in the Python→Node direction: a ydoc
built by the pure-Python ``pm_json_to_ydoc`` must be read *identically* by a real
browser ``y-prosemirror``. The committed artifact
``goldens/python_writepath.json`` holds, per Slice-0 fixture,
``{name, pm_json, update_b64}`` where ``update_b64`` is
``base64(pm_json_to_ydoc(golden_pm, client_id=1).get_update())``. The Node
verifier (``frontend/scripts/verify_python_writepath.mjs`` + its vitest wrapper)
applies those bytes into a ``Y.Doc`` and asserts the reconstructed PM JSON equals
``pm_json`` for all fixtures — the GO/NO-GO gate.

WHY BYTE-EQUALITY IS NOT ASSERTED (Finding E, empirically confirmed): pycrdt's
``Doc.get_update()`` is NOT byte-stable across two builds even with a fixed
``client_id`` (two ``pm_json_to_ydoc(pm, client_id=1)`` builds produce different
bytes). So this test does NOT regenerate-and-assert-byte-equality against the
committed file (that would flake and leave git perpetually dirty). Instead it
VALIDATES the committed artifact structurally: corpus parity, pm_json parity with
the goldens, the Python producer round-trip, and that the *committed* bytes
reconstruct the pm_json. The artifact is regenerated only under
``REGEN_WRITEPATH=1`` (a maintenance escape hatch), never during a normal run.
"""

import base64
import json
import os
import pathlib

from grant_lab.proposal_assist.collab.hydrate import build_doc
from grant_lab.proposal_assist.collab.yjs_bridge import (
    pm_json_to_ydoc,
    ydoc_to_pm_json,
)

from .fixtures import CORPUS

GOLDENS = pathlib.Path(__file__).parent / "goldens"
WRITEPATH = GOLDENS / "python_writepath.json"

# Fixed client id so the produced doc is reproducible at the CRDT level. (The
# *bytes* still are not byte-stable — see the module docstring / Finding E.)
_CLIENT_ID = 1


def _golden(name):
    return json.loads((GOLDENS / f"{name}.json").read_text())


def _build_entries():
    """Produce the write-path entries from the canonical golden pm_json."""
    entries = []
    for name, _ in CORPUS:
        pm = _golden(name)["pm_json"]
        update = pm_json_to_ydoc(pm, client_id=_CLIENT_ID).get_update()
        entries.append(
            {"name": name, "pm_json": pm, "update_b64": base64.b64encode(update).decode("ascii")}
        )
    return entries


def _maybe_regenerate():
    """Regenerate + commit the artifact ONLY under REGEN_WRITEPATH (maintenance)."""
    if not os.environ.get("REGEN_WRITEPATH"):
        return
    WRITEPATH.write_text(json.dumps(_build_entries(), indent=2) + "\n")


def _load_committed():
    _maybe_regenerate()
    assert WRITEPATH.exists(), (
        "python_writepath.json is missing; regenerate with REGEN_WRITEPATH=1"
    )
    return json.loads(WRITEPATH.read_text())


def test_writepath_corpus_parity():
    """The committed artifact covers exactly the Slice-0 corpus (no drift)."""
    committed = _load_committed()
    names = {e["name"] for e in committed}
    golden_names = {name for name, _ in CORPUS}
    assert names == golden_names


def test_writepath_pm_json_matches_goldens():
    """Each entry's pm_json IS the canonical golden pm_json (the producer input)."""
    committed = _load_committed()
    for entry in committed:
        assert entry["pm_json"] == _golden(entry["name"])["pm_json"], entry["name"]


def test_writepath_producer_roundtrip():
    """Python producer round-trip is exact at the JSON level for every fixture."""
    committed = _load_committed()
    for entry in committed:
        pm = entry["pm_json"]
        rebuilt = ydoc_to_pm_json(pm_json_to_ydoc(pm, client_id=_CLIENT_ID))
        assert rebuilt == pm, entry["name"]


def test_writepath_committed_bytes_reconstruct_pm():
    """The COMMITTED Python-produced bytes reconstruct pm_json.

    This is the regression guard that does NOT depend on byte-determinism: it
    decodes the committed base64 update, applies it into a fresh doc, and reads
    the PM JSON back. It fails only if the committed bytes stop encoding the
    fixture — exactly the corruption we want to catch — without ever comparing
    freshly-produced bytes to the committed ones.
    """
    committed = _load_committed()
    for entry in committed:
        doc = build_doc([base64.b64decode(entry["update_b64"])])
        assert ydoc_to_pm_json(doc) == entry["pm_json"], entry["name"]


# ---------------------------------------------------------------------------
# Incremental fixtures: base full-state + apply_pm_section_to_fragment update
# ---------------------------------------------------------------------------

INCREMENTAL = GOLDENS / "python_writepath_incremental.json"

_INCR_CASES = [
    ("replace_middle", "Pre.\n\n## A\n\nBody a.\n\n## B\n\nBody b.\n", 1, "## A\n\nA EDITED.\n"),
    ("grow_blocks", "Pre.\n\n## A\n\nBody a.\n\n## B\n\nBody b.\n", 1, "## A\n\nP1.\n\nP2.\n"),
    ("shrink_blocks", "Pre.\n\n## A\n\nP1.\n\nP2.\n\n## B\n\nBody b.\n", 1, "## A\n\nOnly.\n"),
    ("clear_blocks_empty", "Pre.\n\n## A\n\nBody a.\n\n## B\n\nBody b.\n", 1, ""),
    ("clear_preamble", "Pre.\n\n## A\n\nBody a.\n", 0, ""),
]


def _build_incremental_entries():
    from grant_lab.proposal_assist.collab.markdown_bridge import markdown_to_pm
    from grant_lab.proposal_assist.collab.yjs_bridge import apply_pm_section_to_fragment

    entries = []
    for name, base_md, idx, new_md in _INCR_CASES:
        doc = pm_json_to_ydoc(markdown_to_pm(base_md), client_id=_CLIENT_ID)
        base = doc.get_update()
        blocks = (markdown_to_pm(new_md).get("content") or []) if new_md.strip() else []
        incr = apply_pm_section_to_fragment(doc, section_index=idx, blocks=blocks)
        entries.append({
            "name": name,
            "pm_json": ydoc_to_pm_json(doc),
            "base_b64": base64.b64encode(base).decode("ascii"),
            "incr_b64": base64.b64encode(incr).decode("ascii"),
        })
    return entries


def _maybe_regenerate_incremental():
    if not os.environ.get("REGEN_WRITEPATH"):
        return
    INCREMENTAL.write_text(json.dumps(_build_incremental_entries(), indent=2) + "\n")


def test_incremental_committed_bytes_reconstruct_pm():
    """Committed base+incremental bytes reconstruct the expected post-edit PM."""
    _maybe_regenerate_incremental()
    assert INCREMENTAL.exists(), "python_writepath_incremental.json missing; REGEN_WRITEPATH=1"
    for entry in json.loads(INCREMENTAL.read_text()):
        doc = build_doc([base64.b64decode(entry["base_b64"]), base64.b64decode(entry["incr_b64"])])
        assert ydoc_to_pm_json(doc) == entry["pm_json"], entry["name"]
