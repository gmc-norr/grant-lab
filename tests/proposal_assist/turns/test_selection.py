# tests/proposal_assist/turns/test_selection.py
import subprocess
from pathlib import Path

import pytest

from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.turns.selection import (
    MAX_SELECTION_CODEPOINTS,
    SelectionConflictError,
    SelectionInput,
    build_selection_block,
)


def _repo(tmp_path: Path, proposal: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "proposal.md").write_text(proposal)
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": __import__("os").environ["PATH"],
    }
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=repo, check=True, env=env)
    return repo


DOC = "preamble\n\n## Approach\n\nWe will do the work carefully.\n"


def _sel(  # noqa: PLR0913
    repo: Path, doc: str, idx: int, frm: int, to: int, anchor: str, rt="ask", *, base_sha=None
):
    secs = split_sections(doc)
    sha = base_sha if base_sha is not None else secs[idx].sha
    return SelectionInput(
        request_type=rt, section_index=idx, base_commit_sha="0" * 40,
        base_section_sha=sha, anchor=anchor, from_offset=frm, to_offset=to,
    )


def test_verified_ask_builds_trusted_block(tmp_path):
    repo = _repo(tmp_path, DOC)
    secs = split_sections(DOC)
    text = secs[1].text  # "## Approach\n\nWe will do the work carefully.\n"
    frm, to = text.index("carefully"), text.index("carefully") + len("carefully")
    block = build_selection_block(repo_dir=repo, sel=_sel(repo, DOC, 1, frm, to, "carefully"))
    assert block == {
        "type": "selection", "request_type": "ask", "section_index": 1,
        "anchor": "carefully", "from_offset": frm, "to_offset": to,
        "verified": True, "heading": "Approach",
    }


def test_unverified_ask_allowed_and_labeled(tmp_path):
    repo = _repo(tmp_path, DOC)
    block = build_selection_block(
        repo_dir=repo,
        sel=_sel(repo, DOC, 1, 0, 5, "DIFFERENT", base_sha="stale" * 8),
    )
    assert block["verified"] is False
    assert block["request_type"] == "ask"
    assert block["anchor"] == "DIFFERENT"  # client buffer text, not validated


def test_unverified_modify_raises_conflict(tmp_path):
    repo = _repo(tmp_path, DOC)
    with pytest.raises(SelectionConflictError):
        build_selection_block(
            repo_dir=repo,
            sel=_sel(repo, DOC, 1, 0, 5, "DIFFERENT", rt="modify", base_sha="stale" * 8),
        )


def test_verified_anchor_mismatch_raises(tmp_path):
    repo = _repo(tmp_path, DOC)
    with pytest.raises(SelectionConflictError):
        build_selection_block(repo_dir=repo, sel=_sel(repo, DOC, 1, 0, 5, "WRONGTEXT"))


def test_offset_out_of_range_raises(tmp_path):
    repo = _repo(tmp_path, DOC)
    with pytest.raises(SelectionConflictError):
        build_selection_block(repo_dir=repo, sel=_sel(repo, DOC, 1, 0, 99999, "x"))


def test_too_long_raises(tmp_path):
    big = "## H\n\n" + ("a" * (MAX_SELECTION_CODEPOINTS + 10)) + "\n"
    repo = _repo(tmp_path, big)
    secs = split_sections(big)
    text = secs[1].text
    with pytest.raises(SelectionConflictError):
        build_selection_block(
            repo_dir=repo,
            sel=_sel(repo, big, 1, 0, len(text), text),
        )


def test_codepoint_offsets_index_non_bmp(tmp_path):
    doc = "## H\n\nx😀yZZ\n"  # 😀 is 1 code point
    repo = _repo(tmp_path, doc)
    secs = split_sections(doc)
    text = secs[1].text
    frm = text.index("ZZ")  # code-point index past the emoji
    block = build_selection_block(
        repo_dir=repo, sel=_sel(repo, doc, 1, frm, frm + 2, "ZZ"),
    )
    assert block["verified"] is True and block["anchor"] == "ZZ"
