"""End-to-end service-level flow (Task 14): user save → Tier-2 claim
analysis → citation insert from the finding → further edits → self-healing
coverage reconcile. Spec 2026-06-10 §1, §3, §4, §6.

No web layer: drives save_section, drain_analysis_queue,
insert_from_annotation and reconcile_analysis_coverage directly against the
production <workspaces_dir>/<workspace_id>/repo layout. Every sha asserted
below is recomputed via split_sections on the LIVE file at that step; job,
annotation and provenance assertions are exact (counts + statuses + shas).
"""

from call_manager.db.models import (
    ArtifactVersion,
    ClaimAnalysisJob,
    UserClaimAnnotation,
    Workspace,
    WorkspaceEvent,
)
from call_manager.proposal_assist.analysis.insert import insert_from_annotation
from call_manager.proposal_assist.analysis.reconcile import reconcile_analysis_coverage
from call_manager.proposal_assist.citations.audit import validate_citations_deterministic
from call_manager.proposal_assist.citations.gitio import current_head
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.editing.save import save_section
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.ports.stamp import stamp_for_workspace
from tests.test_analysis_pipeline import (
    FakeCitationService,
    _drain,
    _init_repo,
    _seed_reference,
    _ws,
)

# Non-empty preamble on purpose: it is never enqueued by any save in this flow,
# so step 7's reconcile has exactly one uncovered live section to heal.
DOC = "# Title\nintro\n## Background\nOld background text.\n## Aims\nInitial aims text.\n"
CHUNK_TEXT = "Background. verified words appear here. End."
QUOTE = "verified words"  # FakeCitationService's verdict quote; present in CHUNK_TEXT
CLAIM_1 = "This is a factual claim"  # "claim" → FakeCitationService classifies True
CLAIM_2 = "This rewritten sentence makes a different claim"


def _live(repo):
    doc = (repo / "proposal.md").read_text()
    return doc, split_sections(doc)


def _save(session, ws, user_id, repo, tmp_path, *, section_index, new_text):
    """save_section from a fresh edit form: base = live HEAD + live section sha."""
    _, secs = _live(repo)
    return save_section(
        session=session,
        workspace=ws,
        stamp=stamp_for_workspace(ws),
        user_id=user_id,
        section_index=section_index,
        new_text=new_text,
        base_commit_sha=current_head(repo),
        base_section_sha=secs[section_index].sha,
        workspaces_dir=tmp_path,
        base_branch=None,
    )


def test_save_analyze_insert_full_flow(pg_session_factory, tmp_path):
    # --- Step 1: workspace + production-layout repo (preamble, Background, --- #
    # --- Aims) and one text_status='done' Reference whose single chunk      --- #
    # --- contains the verifiable QUOTE.                                     --- #
    with pg_session_factory() as s:
        ws, user = _ws(s)
        ws_id, user_id = ws.id, user.id
        ref_id = _seed_reference(s, ws_id, CHUNK_TEXT)
        repo = _init_repo(tmp_path, ws_id, DOC)
        s.commit()

    # --- Step 2: user saves Background, adding a factual claim → one queued --- #
    # --- ClaimAnalysisJob addressed at the NEW Background content sha.      --- #
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        res2 = _save(
            s,
            ws,
            user_id,
            repo,
            tmp_path,
            section_index=1,
            new_text="## Background\nOld background text. This is a factual claim.\n",
        )
        s.commit()
        assert res2.changed is True
        assert res2.job_enqueued is True
        assert res2.issues == []  # no markers, no ledger yet → Tier-1 clean
        save2_sha = res2.commit_sha
        assert save2_sha == current_head(repo)

        doc2, secs2 = _live(repo)
        expected_doc2 = (
            "# Title\nintro\n"
            "## Background\nOld background text. This is a factual claim.\n"
            "## Aims\nInitial aims text.\n"
        )
        assert doc2 == expected_doc2
        b1 = secs2[1].sha  # Background sha WITH the claim sentence

        job = s.query(ClaimAnalysisJob).one()  # exactly one queued job
        assert (job.status, job.section_index, job.section_sha, job.commit_sha) == (
            "queued",
            1,
            b1,
            save2_sha,
        )
        av = s.query(ArtifactVersion).one()
        assert (av.created_by, av.commit_sha) == ("user", save2_sha)

    # --- Step 3: drain with fakes → job done; exactly one OPEN              --- #
    # --- uncited_supported annotation carrying the verified quote.          --- #
    counts = _drain(pg_session_factory, tmp_path, FakeCitationService())
    assert counts == {"done": 1, "failed": 0, "superseded": 0}

    with pg_session_factory() as s:
        ann = s.query(UserClaimAnnotation).filter_by(workspace_id=ws_id).one()
        assert ann.kind == "uncited_supported"
        assert ann.status == "open"
        assert ann.section_sha == b1
        assert ann.section_occurrence == 0
        assert ann.section_heading == "Background"
        assert ann.sentence_ordinal == 1  # ordinal 0 is "Old background text"
        assert ann.char_offset == secs2[1].text.index(CLAIM_1)
        assert ann.claim_text == CLAIM_1
        assert ann.evidence_quote == QUOTE
        assert ann.reference_id == ref_id
        assert s.query(ClaimAnalysisJob).one().status == "done"

    # --- Step 4: insert_from_annotation → canonical marker + ledger row in  --- #
    # --- ONE commit; deterministic audit fully clean; provenance exact.     --- #
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        ann = s.query(UserClaimAnnotation).filter_by(workspace_id=ws_id).one()
        res4 = insert_from_annotation(
            session=s, workspace=ws, user_id=user_id, annotation=ann, workspaces_dir=tmp_path
        )
        s.commit()

        ev_id, citekey, insert_sha = res4["ev_id"], res4["citekey"], res4["commit_sha"]
        assert insert_sha == current_head(repo)

        # Canonical marker trails the claim sentence — byte-exact document.
        doc3, secs3 = _live(repo)
        marker = f"[@{citekey}]<!-- ev:{ev_id} -->"
        assert doc3 == expected_doc2.replace(
            "This is a factual claim.\n", f"This is a factual claim.{marker}\n"
        )
        b2 = secs3[1].sha  # marker changed Background's bytes → new sha
        assert b2 != b1

        # citations.json gained exactly the one row, in the same commit.
        ledger = load_ledger((repo / "citations.json").read_text())
        (row,) = ledger["evidence"]
        assert row["id"] == ev_id
        assert row["citekey"] == citekey
        assert row["reference_id"] == str(ref_id)
        assert row["evidence_quote"] == QUOTE

        # Deterministic audit of the final doc+ledger: ZERO issues — in
        # particular no orphan_marker / orphan_row for the new ev.
        issues = validate_citations_deterministic(
            proposal_text=doc3, ledger=ledger, session=s, workspace_id=ws_id
        )
        assert issues == []

        # Annotation resolved to 'inserted' by this user.
        assert ann.status == "inserted"
        assert ann.resolved_by_user_id == user_id
        assert ann.resolved_at is not None

        # ArtifactVersions: exactly one 'user' row for the save + one for the
        # insert; WorkspaceEvents mirror the same two actions.
        avs = s.query(ArtifactVersion).all()
        assert len(avs) == 2
        assert {a.created_by for a in avs} == {"user"}
        assert {a.commit_sha for a in avs} == {save2_sha, insert_sha}
        kinds = sorted(e.kind for e in s.query(WorkspaceEvent).all())
        assert kinds == ["user_citation_insert", "user_edit"]

        # Service-level insert does NOT enqueue analysis of the marker-modified
        # content (the web route does that): B2 has no job row — B1's done job
        # is still the only one.
        jobs = s.query(ClaimAnalysisJob).all()
        assert [(j.section_sha, j.status) for j in jobs] == [(b1, "done")]

    # --- Step 5: editing the OTHER section (Aims) enqueues ONLY the Aims    --- #
    # --- sha; the inserted Background annotation is untouched by the drain. --- #
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        res5 = _save(
            s,
            ws,
            user_id,
            repo,
            tmp_path,
            section_index=2,
            new_text="## Aims\nWe will measure outcomes.\n",
        )
        s.commit()
        assert res5.changed is True
        assert res5.job_enqueued is True
        assert res5.issues == []  # marker + ledger row are intact → still clean
        save5_sha = res5.commit_sha

        _, secs4 = _live(repo)
        assert secs4[1].sha == b2  # Background untouched by the Aims edit
        a2 = secs4[2].sha  # new Aims sha
        queued = s.query(ClaimAnalysisJob).filter_by(status="queued").all()
        assert [(j.section_index, j.section_sha, j.commit_sha) for j in queued] == [
            (2, a2, save5_sha)
        ]
        assert s.query(ClaimAnalysisJob).count() == 2  # B1 done + A2 queued

    counts = _drain(pg_session_factory, tmp_path, FakeCitationService())
    assert counts == {"done": 1, "failed": 0, "superseded": 0}

    with pg_session_factory() as s:
        # "We will measure outcomes" is not a claim → no new annotations; the
        # Background annotation row is exactly as the insert left it.
        ann = s.query(UserClaimAnnotation).filter_by(workspace_id=ws_id).one()
        assert ann.status == "inserted"
        assert ann.section_sha == b1

    # --- Step 6: rewrite the Background claim sentence (drops the marker).  --- #
    # --- Old annotation stays 'inserted', addressed at the now-dead sha B1; --- #
    # --- a fresh analysis of the new content yields the only OPEN finding.  --- #
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        res6 = _save(
            s,
            ws,
            user_id,
            repo,
            tmp_path,
            section_index=1,
            new_text=f"## Background\nOld background text. {CLAIM_2}.\n",
        )
        s.commit()
        assert res6.changed is True
        assert res6.job_enqueued is True
        # The rewrite removed the marker but the ledger row survives → Tier-1
        # reports exactly the orphan_row for our ev, and nothing else.
        assert [(i["type"], i["evidence_id"]) for i in res6.issues] == [("orphan_row", ev_id)]

        _, secs5 = _live(repo)
        b3 = secs5[1].sha  # rewritten Background sha
        assert b3 not in (b1, b2)
        queued = s.query(ClaimAnalysisJob).filter_by(status="queued").all()
        assert [(j.section_index, j.section_sha) for j in queued] == [(1, b3)]

    counts = _drain(pg_session_factory, tmp_path, FakeCitationService())
    assert counts == {"done": 1, "failed": 0, "superseded": 0}

    with pg_session_factory() as s:
        anns = (
            s.query(UserClaimAnnotation)
            .filter_by(workspace_id=ws_id)
            .order_by(UserClaimAnnotation.id)
            .all()
        )
        assert len(anns) == 2
        old, new = anns
        # Step 4's annotation keeps status 'inserted' (fan-out staling touches
        # only OPEN rows of the analyzed sha) and is addressed at B1, which no
        # longer exists at HEAD — so it is never shown as open for live content.
        live_shas = {sec.sha for sec in _live(repo)[1]}
        assert (old.status, old.section_sha) == ("inserted", b1)
        assert b1 not in live_shas
        assert b2 not in live_shas
        # The fresh analysis produced the only open finding, on the live sha.
        assert new.status == "open"
        assert new.kind == "uncited_supported"
        assert new.section_sha == b3
        assert new.claim_text == CLAIM_2
        assert new.evidence_quote == QUOTE
        assert new.reference_id == ref_id

    # --- Step 7: self-healing reconcile. Live HEAD sections: preamble       --- #
    # --- (NEVER enqueued — it predates every save), Background b3 (done),   --- #
    # --- Aims a2 (done). The marker-modified sha b2 was also never enqueued --- #
    # --- (service-level insert doesn't enqueue), but step 6 rewrote it away --- #
    # --- from HEAD, and reconcile covers HEAD content only → it enqueues    --- #
    # --- exactly the preamble sha.                                          --- #
    inserted = reconcile_analysis_coverage(
        session_factory=pg_session_factory, workspaces_dir=tmp_path
    )
    assert inserted == 1
    _, secs6 = _live(repo)
    p_sha = secs6[0].sha
    with pg_session_factory() as s:
        queued = s.query(ClaimAnalysisJob).filter_by(status="queued").all()
        assert [(j.section_index, j.section_sha, j.commit_sha) for j in queued] == [
            (0, p_sha, current_head(repo))
        ]

    counts = _drain(pg_session_factory, tmp_path, FakeCitationService())
    assert counts == {"done": 1, "failed": 0, "superseded": 0}

    # Everything at HEAD is now covered → a second reconcile enqueues nothing.
    assert (
        reconcile_analysis_coverage(session_factory=pg_session_factory, workspaces_dir=tmp_path)
        == 0
    )

    with pg_session_factory() as s:
        # Closure: exactly the four content shas were analyzed, all done.
        jobs = s.query(ClaimAnalysisJob).all()
        assert {j.section_sha: j.status for j in jobs} == {
            b1: "done",
            a2: "done",
            b3: "done",
            p_sha: "done",
        }
        # The preamble ("intro") has no claims → annotation set unchanged.
        assert s.query(UserClaimAnnotation).filter_by(workspace_id=ws_id).count() == 2
