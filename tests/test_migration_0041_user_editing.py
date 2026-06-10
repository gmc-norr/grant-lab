from sqlalchemy import text

from call_manager.db.models import (
    ClaimAnalysisJob,
    ClaimClassification,
    User,
    UserClaimAnnotation,
    Workspace,
)


def _mk_workspace(session) -> Workspace:
    user = User(email="owner@example.org", is_admin=False)
    session.add(user)
    session.flush()
    ws = Workspace(owner_id=user.id, title="t")
    session.add(ws)
    session.flush()
    return ws


def test_claim_analysis_job_dedup_index(pg_session_factory):
    with pg_session_factory() as s:
        ws = _mk_workspace(s)
        s.add(
            ClaimAnalysisJob(
                workspace_id=ws.id,
                section_index=1,
                section_heading="A",
                section_sha="a" * 64,
                commit_sha="c" * 40,
            )
        )
        s.commit()
        dup = s.execute(
            text(
                "INSERT INTO claim_analysis_jobs "
                "(workspace_id, section_index, section_heading, section_sha, commit_sha, status) "
                "VALUES (:w, 2, 'B', :sha, :c, 'queued') ON CONFLICT DO NOTHING RETURNING id"
            ),
            {"w": str(ws.id), "sha": "a" * 64, "c": "d" * 40},
        ).first()
        assert dup is None


def test_annotation_uniqueness_includes_occurrence_and_evidence(pg_session_factory):
    with pg_session_factory() as s:
        ws = _mk_workspace(s)
        base = dict(
            workspace_id=ws.id,
            section_sha="a" * 64,
            section_heading="A",
            sentence_ordinal=0,
            char_offset=0,
            claim_sha="b" * 64,
            claim_text="x",
            kind="uncited_no_match",
        )
        s.add(UserClaimAnnotation(section_occurrence=0, **base))
        s.add(UserClaimAnnotation(section_occurrence=1, **base))
        s.commit()
        s.add(
            UserClaimAnnotation(
                workspace_id=ws.id,
                section_sha="a" * 64,
                section_occurrence=0,
                section_heading="A",
                sentence_ordinal=1,
                char_offset=10,
                claim_sha="c" * 64,
                claim_text="y",
                kind="cited_unsupported",
                evidence_id="ev-aaaaaa111111",
            )
        )
        s.add(
            UserClaimAnnotation(
                workspace_id=ws.id,
                section_sha="a" * 64,
                section_occurrence=0,
                section_heading="A",
                sentence_ordinal=1,
                char_offset=10,
                claim_sha="c" * 64,
                claim_text="y",
                kind="cited_unsupported",
                evidence_id="ev-bbbbbb222222",
            )
        )
        s.commit()
        assert s.query(UserClaimAnnotation).count() == 4


def test_classification_unique_per_prompt_version(pg_session_factory):
    with pg_session_factory() as s:
        ws = _mk_workspace(s)
        s.add(
            ClaimClassification(
                workspace_id=ws.id,
                claim_sha="a" * 64,
                prompt_version="v1",
                is_claim=False,
                outcome="not_claim",
            )
        )
        s.add(
            ClaimClassification(
                workspace_id=ws.id,
                claim_sha="a" * 64,
                prompt_version="v2",
                is_claim=True,
                outcome="no_match",
            )
        )
        s.commit()
        assert s.query(ClaimClassification).count() == 2
