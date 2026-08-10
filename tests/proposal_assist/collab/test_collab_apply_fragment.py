import pycrdt

from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import (
    apply_pm_json_to_fragment,
    pm_json_to_ydoc,
    ydoc_to_pm_json,
)
from call_manager.proposal_assist.editing.sectionizer import split_sections


def test_apply_fragment_mutates_and_client_converges_via_incremental_update():
    md0 = "preamble\n\n## Background\noriginal body.\n\n## Methods\nmethods body.\n"
    room = pm_json_to_ydoc(markdown_to_pm(md0))

    # a client synced to the room's current (pre-edit) state
    client = pycrdt.Doc()
    client.apply_update(room.get_update())
    assert ydoc_to_pm_json(client) == ydoc_to_pm_json(room)

    # replace section 1 (Background) — whole-doc PM-JSON
    secs = split_sections(pm_to_markdown(ydoc_to_pm_json(room)))
    new_text = "## Background\nREWRITTEN.\n\n"
    new_md = "".join(new_text if s.index == 1 else s.text for s in secs)
    new_pm = markdown_to_pm(new_md)

    update = apply_pm_json_to_fragment(room, new_pm)
    assert update is not None and len(update) > 0

    room_md = pm_to_markdown(ydoc_to_pm_json(room))
    assert "REWRITTEN." in room_md and "methods body." in room_md
    # split_sections invariant holds on the mutated serialization
    assert "".join(s.text for s in split_sections(room_md)) == room_md

    # the client converges to the SAME doc using ONLY the incremental update
    client.apply_update(update)
    assert ydoc_to_pm_json(client) == ydoc_to_pm_json(room)


def test_apply_fragment_to_empty_doc_content():
    # replacing with the canonical empty doc must not raise
    room = pm_json_to_ydoc(markdown_to_pm("## X\nbody.\n"))
    update = apply_pm_json_to_fragment(room, {"type": "doc", "content": [{"type": "paragraph"}]})
    assert update is not None
    assert ydoc_to_pm_json(room) == {"type": "doc", "content": [{"type": "paragraph"}]}


def test_table_doc_survives_fragment_apply_roundtrip():
    md = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    pm = markdown_to_pm(md)
    ydoc = pm_json_to_ydoc(pm)
    assert ydoc_to_pm_json(ydoc) == pm          # attrs survive the Yjs coercion
    assert pm_to_markdown(ydoc_to_pm_json(ydoc)) == pm_to_markdown(pm)
