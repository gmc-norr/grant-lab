"""Tests for Defect 1 — embed() truncation-invariant for stored chunk text.

The invariant: when the shrink-and-retry path fires, the stored chunk text
must match the text that was actually embedded (not the original full text).
embed() now returns (vector, embedded_chars) so callers can enforce this.
"""

from __future__ import annotations

import json

import httpx
from sqlalchemy import select

from grant_lab.db.models import ReferenceChunk
from grant_lab.embeddings.client import EmbedResult, OllamaEmbedClient
from grant_lab.proposal_assist.references.embed import FakeEmbedder, OllamaEmbedderAdapter
from grant_lab.proposal_assist.references.ingest import ingest_bytes
from grant_lab.proposal_assist.references.storage import ReferenceStorage
from tests.proposal_assist.conftest import make_user, make_workspace

# ---------------------------------------------------------------------------
# OllamaEmbedClient.embed() — EmbedResult shape
# ---------------------------------------------------------------------------


def _client_returning(payload: dict, *, status: int = 200) -> OllamaEmbedClient:
    def handler(_req):
        return httpx.Response(status, json=payload)

    return OllamaEmbedClient(
        base_url="http://x",
        model="bge-m3",
        transport=httpx.MockTransport(handler),
    )


def test_embed_returns_embed_result_with_full_chars_on_success():
    """When no truncation occurs, embedded_chars == len(original text)."""
    vec = [0.1] * 1024
    client = _client_returning({"embeddings": [vec]})
    text = "short input"
    result = client.embed(text)
    assert isinstance(result, EmbedResult)
    assert result.vector == vec
    assert result.embedded_chars == len(text)


def test_embed_returns_reduced_embedded_chars_after_shrink():
    """After a shrink-and-retry, embedded_chars reflects the truncated length."""
    good = [0.1] * 1024
    bad_body = json.dumps({"embeddings": [[0.1] * 1023 + [float("nan")]]}).encode()

    # Inputs >1000 chars get NaN, shorter get good vector.
    def handler(req):
        body = json.loads(req.content.decode())
        n = len(body["input"])
        if n > 1000:
            return httpx.Response(
                200,
                content=bad_body,
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, json={"embeddings": [good]})

    client = OllamaEmbedClient(
        base_url="http://x",
        model="bge-m3",
        transport=httpx.MockTransport(handler),
    )
    text = "z" * 2500
    result = client.embed(text)
    assert isinstance(result, EmbedResult)
    assert result.vector == good
    # embedded_chars must be less than the original length (truncation occurred)
    assert result.embedded_chars < len(text)
    # and must equal the final accepted length (<=1000 since that's where good starts)
    assert result.embedded_chars <= 1000


def test_embed_no_shrink_embedded_chars_equals_input_length():
    """Happy path — no shrink — embedded_chars == len(text)."""
    vec = [0.5] * 1024
    text = "Hello, embedding world!"
    client = _client_returning({"embeddings": [vec]})
    result = client.embed(text)
    assert result.embedded_chars == len(text)


# ---------------------------------------------------------------------------
# OllamaEmbedderAdapter wraps EmbedResult transparently
# ---------------------------------------------------------------------------


def test_ollama_embedder_adapter_embed_returns_embed_result():
    """OllamaEmbedderAdapter.embed() passes through EmbedResult from the client."""
    vec = [0.2] * 1024
    client = _client_returning({"embeddings": [vec]})
    adapter = OllamaEmbedderAdapter(client)
    result = adapter.embed("some text")
    assert isinstance(result, EmbedResult)
    assert result.vector == vec


# ---------------------------------------------------------------------------
# FakeEmbedder returns EmbedResult
# ---------------------------------------------------------------------------


def test_fake_embedder_returns_embed_result():
    embedder = FakeEmbedder()
    result = embedder.embed("some text")
    assert isinstance(result, EmbedResult)
    assert len(result.vector) == 1024
    assert result.embedded_chars == len("some text")


# ---------------------------------------------------------------------------
# Ingest: stored chunk text == embedded text when truncation fires
# ---------------------------------------------------------------------------


class _TruncatingEmbedder:
    """Test embedder that simulates the shrink-and-retry: for texts over
    _TRUNCATE_AT chars, it only embeds the first _TRUNCATE_AT chars.
    Returns EmbedResult with embedded_chars reflecting the truncated length."""

    _TRUNCATE_AT = 400  # chars at which "NaN" fires and we fall back

    model = "truncating-embedder"

    def embed(self, text: str) -> EmbedResult:
        embedded = text[: self._TRUNCATE_AT] if len(text) > self._TRUNCATE_AT else text
        v = [0.0] * 1024
        v[0] = (len(embedded) % 1024) / 1024.0
        return EmbedResult(vector=v, embedded_chars=len(embedded))


def test_ingest_stores_truncated_prefix_when_embed_shrinks(pg_session_factory, tmp_path):
    """When embed() returns embedded_chars < len(chunk text), the stored
    chunk.text must be trimmed to embedded_chars (the invariant: stored text
    == embedded text). text_status still ends up 'done'."""
    storage = ReferenceStorage(root=tmp_path)
    embedder = _TruncatingEmbedder()

    # Create a markdown doc with a long section that exceeds _TRUNCATE_AT (400 chars)
    long_section = "A" * 600  # will trigger truncation
    md_content = f"# Title\n\n{long_section}\n"
    md = md_content.encode()

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid, uid = ws.id, user.id

    with pg_session_factory() as s:
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
        chunks = list(
            s.scalars(
                select(ReferenceChunk).where(ReferenceChunk.reference_id == result.reference_id)
            )
        )

    # Invariant: for every chunk, stored text length == embedded_chars.
    # When truncation fires, chunk.text must be trimmed to the embedded prefix.
    for chunk in chunks:
        # All stored chunk texts must be <= _TRUNCATE_AT chars (the truncation
        # threshold), because anything longer was truncated to that length.
        assert len(chunk.text) <= _TruncatingEmbedder._TRUNCATE_AT, (
            f"chunk {chunk.chunk_index}: stored text length {len(chunk.text)} "
            f"exceeds max embedded length {_TruncatingEmbedder._TRUNCATE_AT}. "
            "Stored text is not the text that was actually embedded."
        )


def test_ingest_no_truncation_preserves_full_chunk_text(pg_session_factory, tmp_path):
    """When embed() does NOT truncate (normal path), chunk.text must remain
    the full original chunk text (no data lost)."""
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()  # always returns embedded_chars == len(text)

    md = b"# Title\n\nA short section.\n\n## Methods\n\nAnother short section.\n"

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid, uid = ws.id, user.id

    with pg_session_factory() as s:
        result = ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=embedder,
            workspace_id=wsid,
            kind="paper",
            source_type="uploaded",
            source_uri=None,
            filename="short.md",
            mime_type="text/markdown",
            data=md,
            uploaded_by=uid,
        )
        s.commit()

    assert result.text_status == "done"

    with pg_session_factory() as s:
        chunks = list(
            s.scalars(
                select(ReferenceChunk).where(ReferenceChunk.reference_id == result.reference_id)
            )
        )

    # No truncation occurred: all chunks should have non-trivially short text
    for chunk in chunks:
        # The chunk text should contain the original section content
        assert len(chunk.text) > 0
        # FakeEmbedder never truncates, so embedded_chars == len(text) always
        # which means chunk.text is preserved in full
        assert "short section" in chunk.text or "Another" in chunk.text or "Title" in chunk.text
