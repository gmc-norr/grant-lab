"""Unit tests for the human select-to-cite core (SP2a-4b)."""

from __future__ import annotations

import subprocess
import uuid

import pytest

from call_manager.db.models import Reference, ReferenceChunk, User, Workspace
from call_manager.proposal_assist.citations.gitio import _git_env, current_head
from call_manager.proposal_assist.citations.human_cite import (
    CiteInvalid,
    CiteRefNotFound,
    CiteStale,
    insert_citation_for_user,
)
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.editing.save import CITATIONS_FILENAME, PROPOSAL_FILENAME
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir


def _init_repo(root, ws_id, doc):
    repo = workspace_repo_dir(root, ws_id)
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=_git_env())
    (repo / PROPOSAL_FILENAME).write_text(doc)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=_git_env())
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, env=_git_env())
    return repo


def _seed_ws_ref(pg_session_factory, *, chunk_text, chunk_index=0):
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(title="W", owner_id=owner.id, current_phase="spec")
        s.add(ws)
        s.flush()
        ref = Reference(
            workspace_id=ws.id, kind="paper", source_type="uploaded",
            title="Smith 2021", filename="smith.pdf", storage_path="r/smith.pdf",
            mime_type="application/pdf", metadata_status="approved",
        )
        s.add(ref)
        s.flush()
        s.add(ReferenceChunk(
            reference_id=ref.id, workspace_id=ws.id, chunk_index=chunk_index,
            section_path=None, text=chunk_text, embedding=[0.0] * 1024,
            embedding_model="fake",
        ))
        s.commit()
        return owner.id, ws.id, ref.id


def _base(repo, section_index):
    head = current_head(repo)
    return head, split_sections((repo / PROPOSAL_FILENAME).read_text())[section_index].sha


def _call(
    session,
    ws,
    *,
    workspaces_dir,
    base_commit_sha,
    base_section_sha,
    reference_id,
    anchor="The sky is blue.",
    section_index=1,
    chunk_index=0,
    evidence_quote="azure",
    quote_start=0,
    quote_end=5,
    stance="supports",
    user_id=1,
):
    return insert_citation_for_user(
        session=session,
        workspace=ws,
        user_id=user_id,
        section_index=section_index,
        base_commit_sha=base_commit_sha,
        base_section_sha=base_section_sha,
        anchor=anchor,
        reference_id=reference_id,
        chunk_index=chunk_index,
        evidence_quote=evidence_quote,
        quote_start=quote_start,
        quote_end=quote_end,
        stance=stance,
        workspaces_dir=workspaces_dir,
    )


DOC = "preamble\n\n## Claims\nThe sky is blue.\n"


def test_happy_path_inserts_marker_and_ledger(pg_session_factory, tmp_path):
    owner_id, ws_id, ref_id = _seed_ws_ref(pg_session_factory, chunk_text="azure today")
    repo = _init_repo(tmp_path, ws_id, DOC)
    bc, bs = _base(repo, 1)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        out = _call(
            s, ws, reference_id=ref_id, anchor="The sky is blue.",
            evidence_quote="azure", quote_start=0, quote_end=5,
            base_commit_sha=bc, base_section_sha=bs, workspaces_dir=tmp_path, user_id=owner_id,
        )
        s.commit()
    assert set(out) == {"ev_id", "citekey", "commit_sha"}
    new_doc = (repo / PROPOSAL_FILENAME).read_text()
    assert f"[@{out['citekey']}]<!-- ev:{out['ev_id']} -->" in new_doc
    # marker trails the anchor
    assert "The sky is blue.[@" in new_doc
    ledger = load_ledger((repo / CITATIONS_FILENAME).read_text())
    row = next(r for r in ledger["evidence"] if r["id"] == out["ev_id"])
    assert row["evidence_quote"] == "azure"
    assert row["evidence_quote_offsets"] == [0, 5]
    # claim_context is the LIVE claim sentence around the marker (canonical
    # extractor), with the trailing terminator stripped — NOT the raw anchor.
    assert row["claim_context"]["text"] == "The sky is blue"


def test_repeated_span_uses_the_selected_occurrence(pg_session_factory, tmp_path):
    """Two identical substrings in one chunk: the stored offsets pin the user's
    actual occurrence (the exact-offset design — no false 'ambiguous' failure)."""
    owner_id, ws_id, ref_id = _seed_ws_ref(pg_session_factory, chunk_text="blue ... blue")
    repo = _init_repo(tmp_path, ws_id, DOC)
    bc, bs = _base(repo, 1)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        out = _call(  # the SECOND "blue" is at code points 9..13
            s, ws, reference_id=ref_id, anchor="The sky is blue.",
            evidence_quote="blue", quote_start=9, quote_end=13,
            base_commit_sha=bc, base_section_sha=bs, workspaces_dir=tmp_path,
        )
        s.commit()
    ledger = load_ledger((repo / CITATIONS_FILENAME).read_text())
    row = next(r for r in ledger["evidence"] if r["id"] == out["ev_id"])
    assert row["evidence_quote_offsets"] == [9, 13]
    assert row["evidence_quote"] == "blue"


def test_stale_section_raises(pg_session_factory, tmp_path):
    owner_id, ws_id, ref_id = _seed_ws_ref(pg_session_factory, chunk_text="azure sky")
    repo = _init_repo(tmp_path, ws_id, DOC)
    bc, bs = _base(repo, 1)
    # Change the cited section AND move HEAD underneath the captured base.
    (repo / PROPOSAL_FILENAME).write_text("preamble\n\n## Claims\nThe sky is grey.\n")
    subprocess.run(["git", "commit", "-aqm", "x"], cwd=repo, check=True, env=_git_env())
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        with pytest.raises(CiteStale):
            _call(s, ws, reference_id=ref_id, anchor="The sky is blue.",
                  evidence_quote="azure", quote_start=0, quote_end=5,
                  base_commit_sha=bc, base_section_sha=bs, workspaces_dir=tmp_path)


def test_other_section_changed_still_succeeds(pg_session_factory, tmp_path):
    """save_section predicate: HEAD moved, but the CITED section is unchanged."""
    owner_id, ws_id, ref_id = _seed_ws_ref(pg_session_factory, chunk_text="azure sky")
    doc = "preamble\n\n## Claims\nThe sky is blue.\n\n## Other\nuntouched.\n"
    repo = _init_repo(tmp_path, ws_id, doc)
    bc, bs = _base(repo, 1)  # the Claims section
    # Edit a DIFFERENT section (index 2), moving HEAD; Claims bytes unchanged.
    (repo / PROPOSAL_FILENAME).write_text(
        "preamble\n\n## Claims\nThe sky is blue.\n\n## Other\nCHANGED.\n"
    )
    subprocess.run(["git", "commit", "-aqm", "other"], cwd=repo, check=True, env=_git_env())
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        out = _call(s, ws, reference_id=ref_id, anchor="The sky is blue.",
                    evidence_quote="azure", quote_start=0, quote_end=5,
                    base_commit_sha=bc, base_section_sha=bs, workspaces_dir=tmp_path)
        s.commit()
    assert "ev_id" in out


def test_anchor_ambiguous_raises_invalid(pg_session_factory, tmp_path):
    owner_id, ws_id, ref_id = _seed_ws_ref(pg_session_factory, chunk_text="azure sky")
    repo = _init_repo(tmp_path, ws_id, "preamble\n\n## Claims\nfoo. foo.\n")
    bc, bs = _base(repo, 1)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        with pytest.raises(CiteInvalid):
            _call(s, ws, reference_id=ref_id, anchor="foo.",
                  evidence_quote="azure", quote_start=0, quote_end=5,
                  base_commit_sha=bc, base_section_sha=bs, workspaces_dir=tmp_path)


def test_quote_mismatch_raises_invalid(pg_session_factory, tmp_path):
    owner_id, ws_id, ref_id = _seed_ws_ref(pg_session_factory, chunk_text="azure sky")
    repo = _init_repo(tmp_path, ws_id, DOC)
    bc, bs = _base(repo, 1)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        with pytest.raises(CiteInvalid):
            _call(s, ws, reference_id=ref_id, anchor="The sky is blue.",
                  evidence_quote="NOTMATCHING", quote_start=0, quote_end=5,
                  base_commit_sha=bc, base_section_sha=bs, workspaces_dir=tmp_path)


def test_codepoint_offsets_with_non_bmp_char(pg_session_factory, tmp_path):
    """An emoji (non-BMP) before the selection: code-point offsets slice correctly."""
    chunk = "🚀 azure sky"  # "🚀" is ONE code point (index 0); " " at 1; "azure" at 2..7
    owner_id, ws_id, ref_id = _seed_ws_ref(pg_session_factory, chunk_text=chunk)
    repo = _init_repo(tmp_path, ws_id, DOC)
    bc, bs = _base(repo, 1)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        out = _call(s, ws, reference_id=ref_id, anchor="The sky is blue.",
                    evidence_quote="azure", quote_start=2, quote_end=7,
                    base_commit_sha=bc, base_section_sha=bs, workspaces_dir=tmp_path)
        s.commit()
    ledger = load_ledger((repo / CITATIONS_FILENAME).read_text())
    row = next(r for r in ledger["evidence"] if r["id"] == out["ev_id"])
    assert row["evidence_quote"] == "azure"
    assert row["evidence_quote_offsets"] == [2, 7]


def test_foreign_reference_raises_not_found(pg_session_factory, tmp_path):
    owner_id, ws_id, _ = _seed_ws_ref(pg_session_factory, chunk_text="azure sky")
    repo = _init_repo(tmp_path, ws_id, DOC)
    bc, bs = _base(repo, 1)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        with pytest.raises(CiteRefNotFound):
            _call(s, ws, reference_id=uuid.uuid4(), anchor="The sky is blue.",
                  evidence_quote="azure", quote_start=0, quote_end=5,
                  base_commit_sha=bc, base_section_sha=bs, workspaces_dir=tmp_path)


def test_anchor_outside_validated_section_raises_stale(pg_session_factory, tmp_path):
    """Security: a valid (section_index, base_section_sha) for section 1, but an
    anchor that only exists in section 2, must NOT land the marker in section 2 —
    the anchor-in-live.text guard rejects it (the marker may only land in the
    section whose bytes were validated)."""
    owner_id, ws_id, ref_id = _seed_ws_ref(pg_session_factory, chunk_text="azure sky")
    doc = "preamble\n\n## Claims\nThe sky is blue.\n\n## Other\nElsewhere only.\n"
    repo = _init_repo(tmp_path, ws_id, doc)
    bc, bs = _base(repo, 1)  # validate section 1 (Claims)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        with pytest.raises(CiteStale):
            _call(s, ws, reference_id=ref_id, anchor="Elsewhere only.",  # lives in section 2
                  evidence_quote="azure", quote_start=0, quote_end=5,
                  base_commit_sha=bc, base_section_sha=bs, workspaces_dir=tmp_path)
    assert "ev:" not in (repo / PROPOSAL_FILENAME).read_text()  # nothing committed
