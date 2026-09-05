"""Review 2026-09-02 §2.5(a) / Appendix A4 F1: a 50 MB text/plain upload (allowed
MIME, under every byte cap) produced 58,255 chunks — 58,255 sequential embed calls
on the request thread inside one of three ingest slots, ~227 MB of vectors held in
memory before a single INSERT. Markdown of the same size: 3.7 million.

Nothing between `chunk_sections` and `embedder.embed` bounded the chunk count. Now
`_cap_chunks` keeps the first `_MAX_REFERENCE_CHUNKS` and appends one visible
`[truncated]` chunk, exactly like the PDF page cap in extract.py.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from grant_lab.db.models import ReferenceChunk
from grant_lab.proposal_assist.references import ingest as ingest_mod
from grant_lab.proposal_assist.references.embed import FakeEmbedder
from grant_lab.proposal_assist.references.ingest import _cap_chunks, ingest_bytes
from grant_lab.proposal_assist.references.storage import ReferenceStorage
from tests.proposal_assist.conftest import make_user, make_workspace


def test_cap_chunks_keeps_the_first_n_and_appends_a_visible_marker():
    chunks = [(f"s{i}", f"text {i}") for i in range(10)]
    capped = _cap_chunks(chunks, limit=3)
    assert capped[:3] == chunks[:3]
    assert len(capped) == 4
    path, text = capped[3]
    assert path == "[truncated]"
    # The marker says what was kept and what was dropped, so a reader (or the
    # drafting agent) is not misled into thinking the reference ends there.
    assert "3" in text and "10" in text
    assert "truncated" in text.lower()


def test_cap_chunks_is_a_no_op_at_or_under_the_limit():
    chunks = [(f"s{i}", f"text {i}") for i in range(3)]
    assert _cap_chunks(chunks, limit=3) == chunks
    assert _cap_chunks(chunks[:1], limit=3) == chunks[:1]
    assert _cap_chunks([], limit=3) == []


def test_ingest_embeds_at_most_the_cap_plus_the_marker(
    pg_session_factory, tmp_path, monkeypatch, caplog
):
    """End to end through ingest_bytes: eight one-chunk sections under a cap of
    three become exactly four ReferenceChunk rows — three real, one marker — and
    the reference still completes as 'done' (a capped document is usable, not
    failed)."""
    monkeypatch.setattr(ingest_mod, "_MAX_REFERENCE_CHUNKS", 3)
    md = "".join(f"## Section {i}\n\nBody of section {i}.\n\n" for i in range(8)).encode()
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid, uid = ws.id, user.id

    with caplog.at_level(logging.WARNING), pg_session_factory() as s:
        result = ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=embedder,
            workspace_id=wsid,
            kind="paper",
            source_type="uploaded",
            source_uri=None,
            filename="long.md",
            mime_type="text/markdown",
            data=md,
            uploaded_by=uid,
        )
        s.commit()

    assert result.text_status == "done"
    with pg_session_factory() as s:
        rows = list(
            s.scalars(
                select(ReferenceChunk)
                .where(ReferenceChunk.reference_id == result.reference_id)
                .order_by(ReferenceChunk.chunk_index)
            )
        )
    assert len(rows) == 4
    assert [r.section_path for r in rows[:3]] != ["[truncated]"] * 3
    assert rows[3].section_path == "[truncated]"
    assert "truncated" in rows[3].text.lower()
    assert any("chunk cap" in r.getMessage() for r in caplog.records), (
        "the cap must be visible in the log, not only in the row"
    )
