# tests/proposal_assist/collab/test_bridge_review_2026_10_26.py
"""Review 2026-10-26 — H5 (interim), M-PA-7 and the A6 bridge Lows.

H5: a kernel write (accept / cite / annotation insert) re-parses the WHOLE target
section from markdown, so any construct the serializer cannot round-trip is
corrupted in text the user never touched. Two halves are pinned here:

1. the serializer patches, as a parametrized round trip over A6's table of
   constructs, and
2. the fail-closed guard: whatever is STILL lossy makes the kernel refuse the
   write (``KernelUnsafeSection`` → 409) instead of silently rewriting it.

THE ROUND TRIP ASSERTED HERE IS THE ONE THE KERNEL PERFORMS:
``PM block -> pm_to_markdown -> markdown_to_pm -> Yjs -> PM``. The Yjs leg is the
EXISTING canonicalisation — ``test_full_pipeline.test_python_parse_matches_golden_pm_json``
compares the parser against the goldens through exactly that normal form — so the
Yjs conventions (null attrs omitted, integral floats coerced) are not spurious
diffs. Comparing raw ``markdown_to_pm`` output against a room-read block instead
flags 27/141 corpus blocks on ``"title": null`` alone, which would make the guard
refuse almost every section.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import uuid as uuidlib

import pytest

from grant_lab.db.models import ProposedChange, User, UserClaimAnnotation
from grant_lab.proposal_assist.citations.markers import insert_marker
from grant_lab.proposal_assist.collab import state, store
from grant_lab.proposal_assist.collab.canonical import (
    _EMPTY_DOC,
    block_survives_round_trip,
    markdown_to_pm_or_empty,
)
from grant_lab.proposal_assist.collab.hydrate import build_doc, load_or_hydrate
from grant_lab.proposal_assist.collab.kernel import (
    UNSAFE_SECTION_MESSAGE,
    KernelUnsafeSection,
    apply_annotation_insert_to_room,
    apply_citation_to_room,
    kernel_accept_prose,
)
from grant_lab.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from grant_lab.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc, ydoc_to_pm_json
from grant_lab.proposal_assist.editing.sectionizer import split_sections
from tests.proposal_assist.collab.test_collab_kernel_citation import (
    _init_repo,
    _seed_reference,
)
from tests.proposal_assist.conftest import make_user, make_workspace, stamp_for

_GOLDENS = pathlib.Path(__file__).parent / "goldens"

# ---------------------------------------------------------------------------
# PM shapes, built the way the editor writes them (A6's probes/pm_classes.py)
# ---------------------------------------------------------------------------


def T(text: str, *marks: dict) -> dict:
    node = {"type": "text", "text": text}
    if marks:
        node["marks"] = [dict(m) for m in marks]
    return node


def P(*content: dict) -> dict:
    return {"type": "paragraph", "content": list(content)}


def L(href: str, title: str | None = None) -> dict:
    return {"type": "link", "attrs": {"href": href, "title": title}}


def li(*blocks: dict) -> dict:
    return {"type": "list_item", "content": list(blocks)}


def ol(*items: dict, order: int = 1) -> dict:
    return {"type": "ordered_list", "attrs": {"order": order}, "content": list(items)}


STRONG = {"type": "strong"}
EM = {"type": "em"}
CODE = {"type": "code"}


def _y(blocks: list[dict]) -> list[dict]:
    """The blocks as the LIVE ROOM holds them (the existing canonicalisation)."""
    doc = ydoc_to_pm_json(pm_json_to_ydoc({"type": "doc", "content": blocks}))
    return doc.get("content") or []


def _kernel_round_trip(blocks: list[dict]) -> tuple[str, list[dict]]:
    """Exactly what a kernel write does to a section: serialize, re-parse, write
    the result into the room. Returns (markdown, blocks-as-the-room-would-hold-them)."""
    md = pm_to_markdown({"type": "doc", "content": blocks})
    return md, _y(markdown_to_pm_or_empty(md).get("content") or [])


# ---------------------------------------------------------------------------
# A6's table of constructs
# ---------------------------------------------------------------------------

_EMPHASIS_ADJACENCY = (
    "CommonMark cannot express it: the closing delimiter run is preceded by "
    "punctuation and followed by a letter, so it is not right-flanking and "
    "cannot close. The parser is built html=False, so the inline-HTML fallback "
    "the task allows is unavailable. Refused by the kernel's fail-closed guard "
    "instead (test_*_refuses_a_section_it_cannot_rewrite)."
)

_FENCE_LANGUAGE = (
    "code_block has no `language` attr in the constrained SCHEMA (the "
    "prosemirror-schema-basic node, mirrored in frontend/scripts/collab-schema.mjs "
    "and frontend/src/collab/schema.ts), so the info string has nowhere to live in "
    "the PM doc. A HYDRATION-direction loss, not an H5 kernel-write loss."
)

ROUND_TRIP_CASES = [
    pytest.param(
        [P(T("see "), T("plan", L("https://h.se/Research Group/x.docx")))],
        id="link_href_with_a_space",
    ),
    pytest.param(
        [P(T("see "), T("wiki", L("https://h.se/a)b")))],
        id="link_href_with_an_unbalanced_paren",
    ),
    pytest.param(
        [P(T("x", L("https://h.se", 'say "hi"')))],
        id="link_title_with_a_double_quote",
    ),
    pytest.param(
        [P(T("write &amp; or &copy; literally"))],
        id="entity_like_literal_text",
    ),
    pytest.param(
        [
            {
                "type": "bullet_list",
                "content": [li(P(T("Aims:")), ol(li(P(T("third aim"))), order=3))],
            }
        ],
        id="nested_ordered_list_starting_at_3",
    ),
    pytest.param(
        [ol(li(P(T("a")))), ol(li(P(T("b"))))],
        id="two_adjacent_ordered_lists",
    ),
    pytest.param(
        [{"type": "code_block", "content": [T("print('x')")]}],
        id="fenced_code_block_body",
    ),
    pytest.param(
        [P(T("TP53", EM, CODE), T("s are"))],
        id="italic_inline_code_then_a_letter",
        marks=pytest.mark.xfail(strict=True, reason=_EMPHASIS_ADJACENCY),
    ),
    pytest.param(
        [P(T("重要。", STRONG), T("下一句"))],
        id="cjk_bold_then_a_letter",
        marks=pytest.mark.xfail(strict=True, reason=_EMPHASIS_ADJACENCY),
    ),
    pytest.param(
        [P(T("Note:", STRONG), T("This is bold-labelled."))],
        id="bold_then_a_letter",
        marks=pytest.mark.xfail(strict=True, reason=_EMPHASIS_ADJACENCY),
    ),
]


@pytest.mark.parametrize("blocks", ROUND_TRIP_CASES)
def test_the_kernel_round_trip_preserves_the_construct(blocks):
    before = _y(blocks)
    md, after = _kernel_round_trip(before)
    assert after == before, f"md was {md!r}"


@pytest.mark.xfail(strict=True, reason=_FENCE_LANGUAGE)
def test_a_fenced_code_block_keeps_its_info_string():
    """A6 LOW (``LANG-ONLY``): ```` ```python ```` hydrates to a fence with no
    info string, so the language is gone from the projection.

    xfail: the constrained SCHEMA's ``code_block`` has NO language attr (it is the
    prosemirror-schema-basic node, mirrored in ``frontend/scripts/collab-schema.mjs``
    and ``frontend/src/collab/schema.ts``), so there is nowhere in the PM doc to put
    the info string. See STILL-LOSSY in the task report: this one is a HYDRATION
    (md->PM) loss, not an H5 kernel-write loss, and fixing it is a schema-pair
    change plus a goldens regeneration, not a serializer patch."""
    md = "```python\nprint('x')\n```\n"
    assert pm_to_markdown(markdown_to_pm(md)) == md


def test_the_goldens_all_survive_the_kernel_round_trip():
    """The guard must not refuse ordinary content. Every top-level block of every
    y-prosemirror-produced golden passes (and the population is non-empty, so the
    assertion is not vacuous)."""
    blocks: list[dict] = []
    for path in sorted(_GOLDENS.glob("*.json")):
        golden = json.loads(path.read_text())
        if isinstance(golden, dict) and "pm_json" in golden:
            blocks.extend(golden["pm_json"].get("content") or [])
    assert len(blocks) >= 60, f"golden population collapsed to {len(blocks)} blocks"
    unsafe = [b["type"] for b in blocks if not block_survives_round_trip(b)]
    assert unsafe == []


# ---------------------------------------------------------------------------
# A6 LOW: refdef-only markdown must not raise on hydration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "md",
    [
        "[^1]: https://doi.org/10.1/x\n",
        "[ref]: https://doi.org/10.1/x\n",
        "[a]: /x\n[b]: /y\n",
    ],
)
def test_link_reference_definitions_only_is_the_canonical_empty_doc(md):
    """A6 LOW: such a file made ``markdown_to_pm`` raise ``ValueError`` and every
    room open fail (1011 reconnect loop), because the refdefs produce ZERO blocks
    and ``doc`` requires ``block+``. ``markdown_to_pm_or_empty`` only guarded
    whitespace."""
    assert markdown_to_pm_or_empty(md) == _EMPTY_DOC


def test_raw_markdown_to_pm_still_raises_on_a_content_free_document():
    """The or_empty canonicalizer absorbs it; the raw parser keeps its contract."""
    with pytest.raises(ValueError):
        markdown_to_pm("[^1]: https://doi.org/10.1/x\n")


# ---------------------------------------------------------------------------
# M-PA-7: citing text inside a link destroyed the link
# ---------------------------------------------------------------------------

_EV = "ev-0123abcd"


def _link_marked_nodes(pm: dict) -> list[dict]:
    out = []
    for block in pm.get("content") or []:
        for node in block.get("content") or []:
            if any(m["type"] == "link" for m in node.get("marks") or []):
                out.append(node)
    return out


def test_insert_marker_moves_past_a_link_it_would_otherwise_break():
    text = "See the [CRISPR screen](https://doi.org/10.1/x) for details.\n"
    out = insert_marker(text, "CRISPR screen", "ref-a1b2c3", _EV)
    assert out == (
        "See the [CRISPR screen](https://doi.org/10.1/x)"
        f"[@ref-a1b2c3]<!-- ev:{_EV} --> for details.\n"
    )


def test_insert_marker_keeps_the_cited_link_parseable():
    text = "See the [CRISPR screen](https://doi.org/10.1/x) for details.\n"
    out = insert_marker(text, "CRISPR screen", "ref-a1b2c3", _EV)
    pm = markdown_to_pm(out)
    links = _link_marked_nodes(pm)
    assert [n["text"] for n in links] == ["CRISPR screen"]
    assert links[0]["marks"][0]["attrs"]["href"] == "https://doi.org/10.1/x"


def test_insert_marker_with_a_title_and_an_angle_destination():
    text = 'A [paper](<https://h.se/a b> "t(x)") here.\n'
    out = insert_marker(text, "paper", "ref-a1b2c3", _EV)
    assert out.startswith('A [paper](<https://h.se/a b> "t(x)")[@ref-a1b2c3]')
    assert _link_marked_nodes(markdown_to_pm(out))


def test_insert_marker_does_not_cross_a_line_break_inside_a_link_tail():
    """The move must never jump a section boundary. A CommonMark title may span
    lines, so a multi-line link tail could carry the marker past a ``## `` line
    into text the caller never validated (``human_cite`` proves the anchor belongs
    to the validated section and then relies on this placement). The scan aborts
    on a newline, leaving the marker at the anchor."""
    text = '## Real\n\ntail [w](https://h.se "a\n## Fake heading") rest\n'
    out = insert_marker(text, "w", "ref-a1b2c3", _EV)
    marker = f"[@ref-a1b2c3]<!-- ev:{_EV} -->"
    # The observable is the SECTION INDEX the marker lands in, not the section
    # count (the forged boundary is in the input either way, so a count is a
    # constant). Letting the scan cross the newline moves the marker from the
    # anchor's section into the next one.
    sections = split_sections(out)
    with_anchor = [i for i, sec in enumerate(sections) if "tail [" in sec.text]
    with_marker = [i for i, sec in enumerate(sections) if marker in sec.text]
    assert with_marker == with_anchor, out


def test_a_link_title_newline_cannot_forge_a_section_boundary():
    """A title holding ``a\n## Fake heading`` put a ``## `` at the start of a line,
    so split_sections saw TWO sections inside ONE paragraph. The serializer
    flattens the newline, and the (now unrepresentable) title is refused by the
    kernel guard rather than written."""
    block = _y(
        [P(T("tail "), T("w", L("https://h.se", "a\n## Fake heading")), T(" rest"))]
    )[0]
    md = pm_to_markdown({"type": "doc", "content": [block]})
    assert "\n## " not in md, md
    assert len(split_sections(md)) == 1, md
    assert not block_survives_round_trip(block)  # fails closed instead


def test_insert_marker_outside_a_link_is_unchanged():
    text = "Plain [a](b) text here.\n"
    out = insert_marker(text, "text", "ref-a1b2c3", _EV)
    assert out == f"Plain [a](b) text[@ref-a1b2c3]<!-- ev:{_EV} --> here.\n"


# ---------------------------------------------------------------------------
# Fail closed: the three kernel entry points
# ---------------------------------------------------------------------------

LOSSY_PARAGRAPH = P(T("Note:", STRONG), T("This is bold-labelled."))

SAFE_CLAIM = "The claim here."
CHUNK_TEXT = "Adjuvant therapy reduces recurrence by 30% in the treated cohort."
QUOTE = "reduces recurrence by 30%"


def _lossy_room_pm(*, lossy: bool = True) -> dict:
    blocks = [
        P(T("preamble")),
        {"type": "heading", "attrs": {"level": 2}, "content": [T("Methods")]},
        P(T(SAFE_CLAIM)),
    ]
    blocks.append(LOSSY_PARAGRAPH if lossy else P(T("Note: this is plain.")))
    return {"type": "doc", "content": blocks}


def _git_head(repo) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


def _setup_room(pg_session_factory, tmp_path, *, email, lossy=True):
    """A workspace whose LIVE ROOM holds the lossy construct (the log is truth, so
    an authored full-state update IS the room)."""
    with pg_session_factory() as s:
        owner = make_user(s, email=email)
        ws = make_workspace(s, owner=owner, current_phase="active")
        s.commit()
        wid, oid = ws.id, owner.id
    repo = pathlib.Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, "preamble\n\n## Methods\n\nThe claim here.\n")
    pm = _lossy_room_pm(lossy=lossy)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=oid,
            update=pm_json_to_ydoc(pm).get_update(),
        )
        ref_id = _seed_reference(s, wid, chunk_text=CHUNK_TEXT).id
        s.commit()
    return wid, oid, repo, ref_id


def _room(pg_session_factory, wid, tmp_path):
    with pg_session_factory() as s:
        updates = load_or_hydrate(
            s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft",
            workspaces_dir=str(tmp_path),
        )
        s.commit()
    return build_doc(updates)


def _room_md(room_doc) -> str:
    return pm_to_markdown(ydoc_to_pm_json(room_doc))


def test_apply_citation_to_room_refuses_a_section_it_cannot_rewrite(
    pg_session_factory, tmp_path
):
    wid, oid, repo, ref_id = _setup_room(
        pg_session_factory, tmp_path, email="h5-cite@umu.se"
    )
    room_doc = _room(pg_session_factory, wid, tmp_path)
    before_md, before_head = _room_md(room_doc), _git_head(repo)
    base_sha = split_sections(before_md)[1].sha
    with pg_session_factory() as s:
        with pytest.raises(KernelUnsafeSection) as exc:
            apply_citation_to_room(
                s, stamp=stamp_for(s, wid), room_doc=room_doc, workspace_id=wid,
                section_index=1, base_section_sha=base_sha, anchor=SAFE_CLAIM,
                reference_id=ref_id, chunk_index=0, evidence_quote=QUOTE,
                actor_id=oid, workspaces_dir=str(tmp_path),
            )
        s.rollback()
    assert str(exc.value) == UNSAFE_SECTION_MESSAGE
    assert _room_md(room_doc) == before_md  # the room is unchanged
    assert _git_head(repo) == before_head   # and git has no new commit


def test_apply_citation_to_room_still_cites_a_section_it_can_rewrite(
    pg_session_factory, tmp_path
):
    """The guard's observable CHANGES with the construct: the same call on a
    section whose only difference is a plain 'Note:' paragraph succeeds."""
    wid, oid, repo, ref_id = _setup_room(
        pg_session_factory, tmp_path, email="h5-cite-ok@umu.se", lossy=False
    )
    room_doc = _room(pg_session_factory, wid, tmp_path)
    base_sha = split_sections(_room_md(room_doc))[1].sha
    with pg_session_factory() as s:
        res = apply_citation_to_room(
            s, stamp=stamp_for(s, wid), room_doc=room_doc, workspace_id=wid,
            section_index=1, base_section_sha=base_sha, anchor=SAFE_CLAIM,
            reference_id=ref_id, chunk_index=0, evidence_quote=QUOTE,
            actor_id=oid, workspaces_dir=str(tmp_path),
        )
        s.commit()
    assert res.ev_id and f"ev:{res.ev_id}" in _room_md(room_doc)


def test_kernel_accept_prose_refuses_a_section_it_cannot_rewrite(
    pg_session_factory, tmp_path
):
    wid, oid, repo, _ref = _setup_room(
        pg_session_factory, tmp_path, email="h5-accept@umu.se"
    )
    room_doc = _room(pg_session_factory, wid, tmp_path)
    before_md, before_head = _room_md(room_doc), _git_head(repo)
    base_sha = split_sections(before_md)[1].sha
    with pg_session_factory() as s:
        pc = ProposedChange(
            id=uuidlib.uuid4(), workspace_id=wid, status="pending",
            target_artifact="draft", section_index=1, base_commit_sha=before_head,
            base_section_sha=base_sha, kind="prose_edit",
            payload={"new_text": "## Methods\n\nACCEPTED body.\n"}, origin_kind="human",
        )
        s.add(pc)
        s.commit()
        pc_id = pc.id
    with pg_session_factory() as s:
        approver = s.get(User, oid)
        with pytest.raises(KernelUnsafeSection) as exc:
            kernel_accept_prose(
                s, stamp=stamp_for(s, wid), room_doc=room_doc, proposed_change_id=pc_id,
                workspace_id=wid, approver=approver, workspaces_dir=str(tmp_path),
            )
        s.rollback()
    assert str(exc.value) == UNSAFE_SECTION_MESSAGE
    assert _room_md(room_doc) == before_md
    assert _git_head(repo) == before_head
    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).status == "pending"  # fails clean


def test_apply_annotation_insert_to_room_refuses_a_section_it_cannot_rewrite(
    pg_session_factory, tmp_path
):
    wid, oid, repo, ref_id = _setup_room(
        pg_session_factory, tmp_path, email="h5-annot@umu.se"
    )
    room_doc = _room(pg_session_factory, wid, tmp_path)
    before_md, before_head = _room_md(room_doc), _git_head(repo)
    section = split_sections(before_md)[1]
    with pg_session_factory() as s:
        ann = UserClaimAnnotation(
            workspace_id=wid, section_sha=section.sha, section_occurrence=0,
            section_heading=section.heading, sentence_ordinal=0,
            char_offset=section.text.index(SAFE_CLAIM), claim_sha="0" * 64,
            claim_text=SAFE_CLAIM, kind="uncited_supported", evidence_id="",
            reference_id=ref_id, chunk_index=0, evidence_quote=QUOTE, status="open",
        )
        s.add(ann)
        s.commit()
        ann_id = ann.id
    with pg_session_factory() as s:
        with pytest.raises(KernelUnsafeSection) as exc:
            apply_annotation_insert_to_room(
                s, stamp=stamp_for(s, wid), room_doc=room_doc, workspace_id=wid,
                workspaces_dir=str(tmp_path), annotation_id=ann_id, actor_id=oid,
            )
        s.rollback()
    assert str(exc.value) == UNSAFE_SECTION_MESSAGE
    assert _room_md(room_doc) == before_md
    assert _git_head(repo) == before_head
    with pg_session_factory() as s:
        assert s.get(UserClaimAnnotation, ann_id).status == "open"


def test_the_guard_logs_the_block_type_and_never_the_content(
    pg_session_factory, tmp_path, caplog
):
    """Patient-adjacent text: the ERROR line names the block TYPE only."""
    wid, oid, repo, ref_id = _setup_room(
        pg_session_factory, tmp_path, email="h5-log@umu.se"
    )
    room_doc = _room(pg_session_factory, wid, tmp_path)
    base_sha = split_sections(_room_md(room_doc))[1].sha
    with caplog.at_level("ERROR"), pg_session_factory() as s:
        with pytest.raises(KernelUnsafeSection):
            apply_citation_to_room(
                s, stamp=stamp_for(s, wid), room_doc=room_doc, workspace_id=wid,
                section_index=1, base_section_sha=base_sha, anchor=SAFE_CLAIM,
                reference_id=ref_id, chunk_index=0, evidence_quote=QUOTE,
                actor_id=oid, workspaces_dir=str(tmp_path),
            )
        s.rollback()
    logged = "\n".join(r.getMessage() for r in caplog.records if r.levelname == "ERROR")
    assert "paragraph" in logged
    assert "bold-labelled" not in logged
    assert "Note:" not in logged


# ---------------------------------------------------------------------------
# The API contract: 409 with the exact message
# ---------------------------------------------------------------------------


def test_post_citation_on_an_unrewritable_section_is_409(
    make_app, monkeypatch, pg_url, pg_session_factory, tmp_path
):
    from tests.test_api_workspaces import _app, _client

    app = _app(make_app, monkeypatch, pg_url, tmp_path)
    wid, oid, repo, ref_id = _setup_room(
        pg_session_factory, tmp_path, email="h5-api@umu.se"
    )
    with pg_session_factory() as s:
        room_doc = build_doc(
            load_or_hydrate(
                s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft",
                workspaces_dir=str(tmp_path),
            )
        )
        s.commit()
    base_sha = split_sections(_room_md(room_doc))[1].sha
    head = _git_head(repo)
    r = _client(app, oid).post(
        f"/api/workspaces/{wid}/citations",
        json={
            "section_index": 1,
            "base_commit_sha": head,
            "base_section_sha": base_sha,
            "anchor": SAFE_CLAIM,
            "reference_id": str(ref_id),
            "chunk_index": 0,
            "evidence_quote": QUOTE,
            "quote_start": CHUNK_TEXT.index(QUOTE),
            "quote_end": CHUNK_TEXT.index(QUOTE) + len(QUOTE),
            "stance": "supports",
        },
    )
    assert r.status_code == 409, r.text
    # The /api error envelope (web/api/errors.envelope_dict), not a bare detail.
    assert r.json()["error"] == {"code": "conflict", "message": UNSAFE_SECTION_MESSAGE}
    assert _git_head(repo) == head


# ---------------------------------------------------------------------------
# M-PA-7 end to end: citing linked text keeps the link
# ---------------------------------------------------------------------------


def test_citing_linked_text_through_the_kernel_keeps_the_link(
    pg_session_factory, tmp_path
):
    with pg_session_factory() as s:
        owner = make_user(s, email="mpa7@umu.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        s.commit()
        wid, oid = ws.id, owner.id
    repo = pathlib.Path(tmp_path) / str(wid) / "repo"
    md0 = (
        "preamble\n\n## Methods\n\n"
        "See the [CRISPR screen](https://doi.org/10.1/x) for details.\n"
    )
    _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        ref_id = _seed_reference(s, wid, chunk_text=CHUNK_TEXT).id
        s.commit()
    room_doc = _room(pg_session_factory, wid, tmp_path)
    base_sha = split_sections(_room_md(room_doc))[1].sha
    with pg_session_factory() as s:
        res = apply_citation_to_room(
            s, stamp=stamp_for(s, wid), room_doc=room_doc, workspace_id=wid,
            section_index=1, base_section_sha=base_sha, anchor="CRISPR screen",
            reference_id=ref_id, chunk_index=0, evidence_quote=QUOTE,
            actor_id=oid, workspaces_dir=str(tmp_path),
        )
        s.commit()
    pm = ydoc_to_pm_json(room_doc)
    links = _link_marked_nodes(pm)
    assert [n["text"] for n in links] == ["CRISPR screen"], _room_md(room_doc)
    assert f"ev:{res.ev_id}" in _room_md(room_doc)
