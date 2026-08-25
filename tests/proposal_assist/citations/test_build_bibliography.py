"""Tests for the fail-closed `build_bibliography` orchestration (Slice 4 Task 4).

Resolution is via the LEDGER (canonical reference_id), the fail-closed gate
refuses to render if ANY cited reference is unapproved/unwaived/tombstoned/
unresolved (no partial write), only marker-present citekeys are rendered, and
bibliography.md is a separate artifact NEVER merged into proposal.md.
"""

from __future__ import annotations

import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from call_manager.db.models import Reference
from call_manager.proposal_assist.citations.bibliography import (
    BIBLIOGRAPHY_FILENAME,
    BuildBibliographyBlocked,
    build_bibliography,
    compose_export,
)
from call_manager.proposal_assist.citations.markers import build_marker
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from tests.proposal_assist.conftest import make_user, make_workspace

SMITH_CSL = {
    "author": [{"family": "Smith", "given": "Jane"}],
    "title": "Adjuvant therapy outcomes",
    "container-title": "Journal of Oncology",
    "volume": "27",
    "issue": "4",
    "page": "511-520",
    "issued": {"date-parts": [[2021]]},
    "DOI": "10.1000/abc",
}
JONES_CSL = {
    "author": [{"family": "Jones", "given": "Alan"}],
    "title": "Background mechanisms",
    "container-title": "Cell Reports",
    "issued": {"date-parts": [[2019]]},
}


def _make_workspace(pg_session_factory, *, owner_email="owner@ki.se"):
    with pg_session_factory() as s:
        owner = make_user(s, email=owner_email)
        ws = make_workspace(s, owner=owner)
        s.commit()
        return ws.id


def _seed_reference(
    pg_session_factory,
    workspace_id,
    *,
    citekey,
    csl_json,
    csl_json_extracted=None,
    metadata_status="approved",
    metadata_waiver=False,
    tombstoned_at=None,
) -> uuid.UUID:
    with pg_session_factory() as s:
        ref = Reference(
            workspace_id=workspace_id,
            kind="paper",
            source_type="uploaded",
            filename="seed.pdf",
            storage_path="seed/seed.pdf",
            mime_type="application/pdf",
            citekey=citekey,
            csl_json=csl_json,
            csl_json_extracted=csl_json_extracted,
            metadata_status=metadata_status,
            metadata_waiver=metadata_waiver,
            tombstoned_at=tombstoned_at,
        )
        s.add(ref)
        s.flush()
        rid = ref.id
        s.commit()
    return rid


def _ledger_row(*, citekey, reference_id, ev_id):
    return {"id": ev_id, "citekey": citekey, "reference_id": str(reference_id)}


def _ledger(rows):
    return {"version": 2, "evidence": rows}


def _proposal(*markers: str) -> str:
    body = " ".join(markers)
    return f"# Proposal\n\nWe argue {body} in our population.\n"


def _init_repo(repo_dir: Path, initial_content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(initial_content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo_dir, check=True)


def _head(repo_dir: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


# --------------------------------------------------------------------------- #


def test_all_approved_writes_bibliography_not_proposal(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    rid_a = _seed_reference(pg_session_factory, wsid, citekey="smith2021", csl_json=SMITH_CSL)
    rid_b = _seed_reference(pg_session_factory, wsid, citekey="jones2019", csl_json=JONES_CSL)
    proposal = _proposal(
        build_marker("smith2021", "ev-aaaaaaaaaaaa"),
        build_marker("jones2019", "ev-bbbbbbbbbbbb"),
    )
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir, proposal)
    proposal_before = (repo_dir / PROPOSAL_FILENAME).read_text()

    with pg_session_factory() as session:
        result = build_bibliography(
            proposal_text=proposal,
            ledger=_ledger(
                [
                    _ledger_row(citekey="smith2021", reference_id=rid_a, ev_id="ev-aaaaaaaaaaaa"),
                    _ledger_row(citekey="jones2019", reference_id=rid_b, ev_id="ev-bbbbbbbbbbbb"),
                ]
            ),
            session=session,
            workspace_id=wsid,
            style="vancouver",
            repo_dir=repo_dir,
            commit=True,
        )

    assert result["citekey_count"] == 2
    assert "Smith" in result["bibliography_md"]
    assert "Jones" in result["bibliography_md"]
    assert result["commit_sha"]

    # bibliography.md written; proposal.md untouched.
    assert (repo_dir / BIBLIOGRAPHY_FILENAME).exists()
    assert (repo_dir / PROPOSAL_FILENAME).read_text() == proposal_before
    # Exactly one new commit, and it touched bibliography.md (not proposal.md).
    changed = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert changed == [BIBLIOGRAPHY_FILENAME]


def test_only_marker_present_keys_included(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    rid_a = _seed_reference(pg_session_factory, wsid, citekey="smith2021", csl_json=SMITH_CSL)
    rid_b = _seed_reference(pg_session_factory, wsid, citekey="jones2019", csl_json=JONES_CSL)
    # Only smith2021 is cited in the proposal text.
    proposal = _proposal(build_marker("smith2021", "ev-aaaaaaaaaaaa"))

    with pg_session_factory() as session:
        result = build_bibliography(
            proposal_text=proposal,
            ledger=_ledger(
                [
                    _ledger_row(citekey="smith2021", reference_id=rid_a, ev_id="ev-aaaaaaaaaaaa"),
                    _ledger_row(citekey="jones2019", reference_id=rid_b, ev_id="ev-bbbbbbbbbbbb"),
                ]
            ),
            session=session,
            workspace_id=wsid,
            style="vancouver",
            repo_dir=tmp_path,
            commit=False,
        )

    assert result["citekey_count"] == 1
    assert "Smith" in result["bibliography_md"]
    assert "Jones" not in result["bibliography_md"]


def test_document_order_vancouver(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    rid_a = _seed_reference(pg_session_factory, wsid, citekey="smith2021", csl_json=SMITH_CSL)
    rid_b = _seed_reference(pg_session_factory, wsid, citekey="jones2019", csl_json=JONES_CSL)
    # jones2019 appears FIRST in the document.
    proposal = _proposal(
        build_marker("jones2019", "ev-bbbbbbbbbbbb"),
        build_marker("smith2021", "ev-aaaaaaaaaaaa"),
    )

    with pg_session_factory() as session:
        result = build_bibliography(
            proposal_text=proposal,
            ledger=_ledger(
                [
                    _ledger_row(citekey="smith2021", reference_id=rid_a, ev_id="ev-aaaaaaaaaaaa"),
                    _ledger_row(citekey="jones2019", reference_id=rid_b, ev_id="ev-bbbbbbbbbbbb"),
                ]
            ),
            session=session,
            workspace_id=wsid,
            style="vancouver",
            repo_dir=tmp_path,
            commit=False,
        )

    md = result["bibliography_md"]
    # [1] is Jones (first in document), [2] is Smith.
    assert md.index("[1] Jones") < md.index("[2] Smith")


def test_fail_closed_unapproved_no_write(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        citekey="smith2021",
        csl_json=SMITH_CSL,
        metadata_status="extracted",
        metadata_waiver=False,
    )
    proposal = _proposal(build_marker("smith2021", "ev-aaaaaaaaaaaa"))
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir, proposal)
    head_before = _head(repo_dir)

    with pg_session_factory() as session, pytest.raises(BuildBibliographyBlocked) as exc:
        build_bibliography(
            proposal_text=proposal,
            ledger=_ledger(
                [_ledger_row(citekey="smith2021", reference_id=rid, ev_id="ev-aaaaaaaaaaaa")]
            ),
            session=session,
            workspace_id=wsid,
            style="vancouver",
            repo_dir=repo_dir,
            commit=True,
        )

    blocked = exc.value.blocked
    assert len(blocked) == 1
    assert blocked[0]["citekey"] == "smith2021"
    assert blocked[0]["reason"] == "metadata_unapproved"
    assert blocked[0]["reference_id"] == str(rid)
    assert blocked[0]["metadata_status"] == "extracted"

    # NO write: bibliography.md absent, HEAD unchanged.
    assert not (repo_dir / BIBLIOGRAPHY_FILENAME).exists()
    assert _head(repo_dir) == head_before


def test_waiver_allows_unapproved(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        citekey="smith2021",
        csl_json=SMITH_CSL,
        metadata_status="extracted",
        metadata_waiver=True,
    )
    proposal = _proposal(build_marker("smith2021", "ev-aaaaaaaaaaaa"))

    with pg_session_factory() as session:
        result = build_bibliography(
            proposal_text=proposal,
            ledger=_ledger(
                [_ledger_row(citekey="smith2021", reference_id=rid, ev_id="ev-aaaaaaaaaaaa")]
            ),
            session=session,
            workspace_id=wsid,
            style="vancouver",
            repo_dir=tmp_path,
            commit=False,
        )

    assert result["citekey_count"] == 1
    assert "Smith" in result["bibliography_md"]


def test_tombstoned_blocked(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        citekey="smith2021",
        csl_json=SMITH_CSL,
        metadata_status="approved",
        tombstoned_at=datetime.now(UTC),
    )
    proposal = _proposal(build_marker("smith2021", "ev-aaaaaaaaaaaa"))

    with pg_session_factory() as session, pytest.raises(BuildBibliographyBlocked) as exc:
        build_bibliography(
            proposal_text=proposal,
            ledger=_ledger(
                [_ledger_row(citekey="smith2021", reference_id=rid, ev_id="ev-aaaaaaaaaaaa")]
            ),
            session=session,
            workspace_id=wsid,
            style="vancouver",
            repo_dir=tmp_path,
            commit=False,
        )

    assert exc.value.blocked[0]["reason"] == "reference_tombstoned"


def test_unresolved_citekey_blocked(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    # [@ghost] has no ledger row.
    proposal = _proposal(build_marker("ghost", "ev-cccccccccccc"))

    with pg_session_factory() as session, pytest.raises(BuildBibliographyBlocked) as exc:
        build_bibliography(
            proposal_text=proposal,
            ledger=_ledger([]),
            session=session,
            workspace_id=wsid,
            style="vancouver",
            repo_dir=tmp_path,
            commit=False,
        )

    blocked = exc.value.blocked[0]
    assert blocked["reason"] == "unresolved_citekey"
    assert blocked["citekey"] == "ghost"
    assert blocked["reference_id"] is None


def test_reference_in_other_workspace_blocked(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    other_wsid = _make_workspace(pg_session_factory, owner_email="other@ki.se")
    rid = _seed_reference(pg_session_factory, other_wsid, citekey="smith2021", csl_json=SMITH_CSL)
    proposal = _proposal(build_marker("smith2021", "ev-aaaaaaaaaaaa"))

    with pg_session_factory() as session, pytest.raises(BuildBibliographyBlocked) as exc:
        build_bibliography(
            proposal_text=proposal,
            ledger=_ledger(
                [_ledger_row(citekey="smith2021", reference_id=rid, ev_id="ev-aaaaaaaaaaaa")]
            ),
            session=session,
            workspace_id=wsid,
            style="vancouver",
            repo_dir=tmp_path,
            commit=False,
        )

    assert exc.value.blocked[0]["reason"] == "reference_missing"


def test_style_override(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid, citekey="smith2021", csl_json=SMITH_CSL)
    proposal = _proposal(build_marker("smith2021", "ev-aaaaaaaaaaaa"))
    ledger = _ledger([_ledger_row(citekey="smith2021", reference_id=rid, ev_id="ev-aaaaaaaaaaaa")])

    with pg_session_factory() as session:
        vancouver = build_bibliography(
            proposal_text=proposal,
            ledger=ledger,
            session=session,
            workspace_id=wsid,
            style="vancouver",
            repo_dir=tmp_path,
            commit=False,
        )
        author_year = build_bibliography(
            proposal_text=proposal,
            ledger=ledger,
            session=session,
            workspace_id=wsid,
            style="author-year",
            repo_dir=tmp_path,
            commit=False,
        )

    assert vancouver["bibliography_md"] != author_year["bibliography_md"]
    assert "[1] Smith" in vancouver["bibliography_md"]
    assert "[1]" not in author_year["bibliography_md"]


def test_empty_no_markers(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    proposal = "# Proposal\n\nNo citations here.\n"

    with pg_session_factory() as session:
        result = build_bibliography(
            proposal_text=proposal,
            ledger=_ledger([]),
            session=session,
            workspace_id=wsid,
            style="vancouver",
            repo_dir=tmp_path,
            commit=False,
        )

    assert result["citekey_count"] == 0
    assert result["bibliography_md"] == ""
    assert result["commit_sha"] is None


def test_empty_with_commit_writes_no_file(pg_session_factory, tmp_path):
    # No markers + commit=True must NOT create or truncate bibliography.md.
    wsid = _make_workspace(pg_session_factory)
    proposal = "# Proposal\n\nNo citations here.\n"
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir, proposal)
    head_before = _head(repo_dir)

    with pg_session_factory() as session:
        result = build_bibliography(
            proposal_text=proposal,
            ledger=_ledger([]),
            session=session,
            workspace_id=wsid,
            style="vancouver",
            repo_dir=repo_dir,
            commit=True,
        )

    assert result["citekey_count"] == 0
    assert result["bibliography_md"] == ""
    assert result["commit_sha"] is None
    assert not (repo_dir / BIBLIOGRAPHY_FILENAME).exists()
    assert _head(repo_dir) == head_before  # no commit created


def test_inconsistent_reference_ids_for_one_citekey_blocked(pg_session_factory, tmp_path):
    # Two ledger rows for the same citekey disagreeing on reference_id is drift:
    # fail closed (unresolved_citekey), do not silently trust the first row.
    wsid = _make_workspace(pg_session_factory)
    rid_a = _seed_reference(pg_session_factory, wsid, citekey="smith2021", csl_json=SMITH_CSL)
    rid_b = _seed_reference(pg_session_factory, wsid, citekey="smith2021b", csl_json=JONES_CSL)
    proposal = _proposal(build_marker("smith2021", "ev-aaaaaaaaaaaa"))

    with pg_session_factory() as session, pytest.raises(BuildBibliographyBlocked) as exc:
        build_bibliography(
            proposal_text=proposal,
            ledger=_ledger(
                [
                    _ledger_row(citekey="smith2021", reference_id=rid_a, ev_id="ev-aaaaaaaaaaaa"),
                    _ledger_row(citekey="smith2021", reference_id=rid_b, ev_id="ev-cccccccccccc"),
                ]
            ),
            session=session,
            workspace_id=wsid,
            style="vancouver",
            repo_dir=tmp_path,
            commit=False,
        )

    blocked = exc.value.blocked
    assert len(blocked) == 1
    assert blocked[0]["citekey"] == "smith2021"
    assert blocked[0]["reason"] == "unresolved_citekey"


# --------------------------------------------------------------------------- #
# compose_export (inline citations + appended bibliography)
# --------------------------------------------------------------------------- #


def test_compose_vancouver_inline_numbers_document_order(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    rid_a = _seed_reference(pg_session_factory, wsid, citekey="smith2021", csl_json=SMITH_CSL)
    rid_b = _seed_reference(pg_session_factory, wsid, citekey="jones2019", csl_json=JONES_CSL)
    # smith, jones, then smith AGAIN — repeated citekey must reuse [1].
    proposal = _proposal(
        build_marker("smith2021", "ev-aaaaaaaaaaaa"),
        build_marker("jones2019", "ev-bbbbbbbbbbbb"),
        build_marker("smith2021", "ev-cccccccccccc"),
    )
    ledger = _ledger(
        [
            _ledger_row(citekey="smith2021", reference_id=rid_a, ev_id="ev-aaaaaaaaaaaa"),
            _ledger_row(citekey="jones2019", reference_id=rid_b, ev_id="ev-bbbbbbbbbbbb"),
            _ledger_row(citekey="smith2021", reference_id=rid_a, ev_id="ev-cccccccccccc"),
        ]
    )

    with pg_session_factory() as session:
        out = compose_export(
            proposal_text=proposal,
            ledger=ledger,
            session=session,
            workspace_id=wsid,
            style="vancouver",
        )

    # No marker syntax leaks.
    assert "[@" not in out
    assert "<!-- ev:" not in out
    # smith=1 (twice), jones=2 (once).
    assert out.count("[1]") == 2
    assert out.count("[2]") == 1


def test_compose_author_year_inline_form_and_suffix(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid, citekey="smith2021a", csl_json=SMITH_CSL)
    proposal = _proposal(build_marker("smith2021a", "ev-aaaaaaaaaaaa"))
    ledger = _ledger([_ledger_row(citekey="smith2021a", reference_id=rid, ev_id="ev-aaaaaaaaaaaa")])

    with pg_session_factory() as session:
        out = compose_export(
            proposal_text=proposal,
            ledger=ledger,
            session=session,
            workspace_id=wsid,
            style="author-year",
        )

    assert "[@" not in out
    assert "<!-- ev:" not in out
    assert "(Smith 2021a)" in out  # single author, year + suffix from citekey


def test_compose_appends_bibliography(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid, citekey="smith2021", csl_json=SMITH_CSL)
    proposal = _proposal(build_marker("smith2021", "ev-aaaaaaaaaaaa"))
    ledger = _ledger([_ledger_row(citekey="smith2021", reference_id=rid, ev_id="ev-aaaaaaaaaaaa")])

    with pg_session_factory() as session:
        out = compose_export(
            proposal_text=proposal,
            ledger=ledger,
            session=session,
            workspace_id=wsid,
            style="vancouver",
            bibliography_text="# References\n\n[1] Smith J. ...\n",
        )

    assert "# References" in out
    assert out.rstrip().endswith("[1] Smith J. ...")


def test_compose_fail_closed_on_unapproved(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        citekey="smith2021",
        csl_json=SMITH_CSL,
        metadata_status="pending",
    )
    proposal = _proposal(build_marker("smith2021", "ev-aaaaaaaaaaaa"))
    ledger = _ledger([_ledger_row(citekey="smith2021", reference_id=rid, ev_id="ev-aaaaaaaaaaaa")])

    with pg_session_factory() as session, pytest.raises(BuildBibliographyBlocked):
        compose_export(
            proposal_text=proposal,
            ledger=ledger,
            session=session,
            workspace_id=wsid,
            style="vancouver",
        )


def test_compose_fail_closed_on_bare_marker(pg_session_factory, tmp_path):
    # A bare "[@orphan]" (no ev-comment) bypasses MARKER_RE — the defensive
    # scrub must fail closed rather than leak it into the exported document.
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid, citekey="smith2021", csl_json=SMITH_CSL)
    proposal = (
        f"# P\n\nWell-formed {build_marker('smith2021', 'ev-aaaaaaaaaaaa')} and "
        "a bare [@orphan] one.\n"
    )
    ledger = _ledger([_ledger_row(citekey="smith2021", reference_id=rid, ev_id="ev-aaaaaaaaaaaa")])

    with pg_session_factory() as session, pytest.raises(BuildBibliographyBlocked) as exc:
        compose_export(
            proposal_text=proposal,
            ledger=ledger,
            session=session,
            workspace_id=wsid,
            style="vancouver",
        )

    reasons = {b["reason"] for b in exc.value.blocked}
    assert "unresolved_marker" in reasons
    assert any(b["citekey"] == "orphan" for b in exc.value.blocked)


def test_compose_no_markers_returns_proposal_unchanged(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    proposal = "# P\n\nNo citations at all.\n"
    with pg_session_factory() as session:
        out = compose_export(
            proposal_text=proposal,
            ledger=_ledger([]),
            session=session,
            workspace_id=wsid,
            style="vancouver",
            bibliography_text=None,
        )
    assert out == proposal
    assert "# References" not in out


# --------------------------------------------------------------------------- #
# C1 regression: verified_external + waived refs hold metadata in
# csl_json_extracted (csl_json is NULL) — they must render real titles, not
# "Untitled".
# --------------------------------------------------------------------------- #


def test_verified_external_renders_from_extracted_not_untitled(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    # Fetched-by-DOI shape: authoritative metadata in csl_json_extracted, NULL
    # csl_json, status verified_external (passes the gate without approval).
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        citekey="smith2021",
        csl_json=None,
        csl_json_extracted=SMITH_CSL,
        metadata_status="verified_external",
    )
    proposal = _proposal(build_marker("smith2021", "ev-aaaaaaaaaaaa"))
    ledger = _ledger([_ledger_row(citekey="smith2021", reference_id=rid, ev_id="ev-aaaaaaaaaaaa")])

    with pg_session_factory() as session:
        result = build_bibliography(
            proposal_text=proposal,
            ledger=ledger,
            session=session,
            workspace_id=wsid,
            style="vancouver",
            repo_dir=tmp_path,
            commit=False,
        )

    assert result["citekey_count"] == 1
    assert "Smith" in result["bibliography_md"]
    assert "Untitled" not in result["bibliography_md"]


def test_waived_extracted_ref_renders_from_extracted(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    # LLM-extracted (unapproved) but waived by the owner: metadata only in
    # csl_json_extracted. The waiver opens the gate; render must use the metadata.
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        citekey="smith2021",
        csl_json=None,
        csl_json_extracted=SMITH_CSL,
        metadata_status="extracted",
        metadata_waiver=True,
    )
    proposal = _proposal(build_marker("smith2021", "ev-aaaaaaaaaaaa"))
    ledger = _ledger([_ledger_row(citekey="smith2021", reference_id=rid, ev_id="ev-aaaaaaaaaaaa")])

    with pg_session_factory() as session:
        result = build_bibliography(
            proposal_text=proposal,
            ledger=ledger,
            session=session,
            workspace_id=wsid,
            style="vancouver",
            repo_dir=tmp_path,
            commit=False,
        )

    assert result["citekey_count"] == 1
    assert "Smith" in result["bibliography_md"]
    assert "Untitled" not in result["bibliography_md"]


# --------------------------------------------------------------------------- #
# Defect 1: code-fence-aware residual scrub in compose_export.
#
# A bare [@token] inside a fenced code block or inline code span must NOT
# block export.  A bare [@token] in real prose MUST still block (fail-closed).
# --------------------------------------------------------------------------- #


def test_compose_bare_marker_in_fenced_code_block_does_not_block(pg_session_factory, tmp_path):
    """A [@token] inside a fenced code block is literal text, not a citation."""
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid, citekey="smith2021", csl_json=SMITH_CSL)
    # Real marker in prose + bare [@example] inside a fenced block.
    proposal = (
        "# Proposal\n\n"
        f"We argue {build_marker('smith2021', 'ev-aaaaaaaaaaaa')} in our population.\n\n"
        "```python\n"
        "# Use pandoc-style [@smith2021] citations\n"
        "```\n"
    )
    ledger = _ledger([_ledger_row(citekey="smith2021", reference_id=rid, ev_id="ev-aaaaaaaaaaaa")])

    with pg_session_factory() as session:
        # Must NOT raise BuildBibliographyBlocked.
        out = compose_export(
            proposal_text=proposal,
            ledger=ledger,
            session=session,
            workspace_id=wsid,
            style="vancouver",
        )

    # The fenced code block content is preserved verbatim.
    assert "[@smith2021]" in out
    # The real citation was replaced.
    assert "[1]" in out
    assert "<!-- ev:" not in out


def test_compose_bare_marker_in_inline_code_does_not_block(pg_session_factory, tmp_path):
    """A [@token] inside an inline code span is literal text, not a citation."""
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid, citekey="smith2021", csl_json=SMITH_CSL)
    # Real marker in prose + bare [@jones2019] inside backtick inline-code.
    proposal = (
        "# Proposal\n\n"
        f"Real {build_marker('smith2021', 'ev-aaaaaaaaaaaa')} and inline "
        "code `[@jones2019]` shows the format.\n"
    )
    ledger = _ledger([_ledger_row(citekey="smith2021", reference_id=rid, ev_id="ev-aaaaaaaaaaaa")])

    with pg_session_factory() as session:
        out = compose_export(
            proposal_text=proposal,
            ledger=ledger,
            session=session,
            workspace_id=wsid,
            style="vancouver",
        )

    assert "[@jones2019]" in out
    assert "[1]" in out
    assert "<!-- ev:" not in out


def test_compose_bare_marker_in_prose_still_blocks(pg_session_factory, tmp_path):
    """A [@token] in real prose (not inside code) must still raise BuildBibliographyBlocked."""
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid, citekey="smith2021", csl_json=SMITH_CSL)
    # One good marker + one bare marker in PROSE (not inside any code fence/span).
    proposal = (
        "# Proposal\n\n"
        f"Good {build_marker('smith2021', 'ev-aaaaaaaaaaaa')} and a bare [@orphan] here.\n"
    )
    ledger = _ledger([_ledger_row(citekey="smith2021", reference_id=rid, ev_id="ev-aaaaaaaaaaaa")])

    with pg_session_factory() as session, pytest.raises(BuildBibliographyBlocked) as exc:
        compose_export(
            proposal_text=proposal,
            ledger=ledger,
            session=session,
            workspace_id=wsid,
            style="vancouver",
        )

    reasons = {b["reason"] for b in exc.value.blocked}
    assert "unresolved_marker" in reasons
    assert any(b["citekey"] == "orphan" for b in exc.value.blocked)


def test_build_bibliography_does_not_commit_unless_asked(pg_session_factory, tmp_path):
    """The git write is unfenced, so it must never be the default.

    bibliography.py calls write_and_commit_files with no mutex and no
    generation. Both current production callers pass commit=False; the default
    must match, so a future caller who forgets the keyword does not get an
    unfenced git write.
    """
    wsid = _make_workspace(pg_session_factory)
    rid_a = _seed_reference(pg_session_factory, wsid, citekey="smith2021", csl_json=SMITH_CSL)
    rid_b = _seed_reference(pg_session_factory, wsid, citekey="jones2019", csl_json=JONES_CSL)
    proposal = _proposal(
        build_marker("smith2021", "ev-aaaaaaaaaaaa"),
        build_marker("jones2019", "ev-bbbbbbbbbbbb"),
    )
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir, proposal)
    head_before = _head(repo_dir)

    with pg_session_factory() as session:
        result = build_bibliography(
            proposal_text=proposal,
            ledger=_ledger(
                [
                    _ledger_row(citekey="smith2021", reference_id=rid_a, ev_id="ev-aaaaaaaaaaaa"),
                    _ledger_row(citekey="jones2019", reference_id=rid_b, ev_id="ev-bbbbbbbbbbbb"),
                ]
            ),
            session=session,
            workspace_id=wsid,
            style="vancouver",
            repo_dir=repo_dir,
            # commit deliberately omitted — must not produce a git write by default
        )

    assert result["commit_sha"] is None
    assert _head(repo_dir) == head_before
