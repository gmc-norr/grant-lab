import uuid

from call_manager.proposal_assist.artifacts import Artifact, filename_for
from call_manager.proposal_assist.collab import canonical, store
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from tests.proposal_assist.conftest import make_user, make_workspace


def _seed_log(pg_session_factory, wid, artifact, md):
    """Persist a single Yjs update reconstructing `md` into the durable log."""
    seed = pm_json_to_ydoc(markdown_to_pm(md)).get_update()
    with pg_session_factory() as s:
        store.append_update(s, workspace_id=wid, artifact=artifact, actor_id=None, update=seed)
        s.commit()


def test_canonical_from_nonempty_log(pg_session_factory):
    md = "## A\n\nBody a.\n"
    # A real workspace row first — yjs_updates.workspace_id is an FK to workspaces.
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="canon-log@ki.se"))
        s.commit()
        wid = ws.id
    _seed_log(pg_session_factory, wid, "draft", md)
    with pg_session_factory() as s:
        out = canonical.canonical_artifact_markdown(
            s, workspace_id=wid, artifact="draft", repo_dir="/nonexistent"
        )
    # Round-trips to the normalized form (log is canonical; repo_dir is ignored).
    assert "## A" in out and "Body a." in out


def test_canonical_empty_log_normalizes_head(pg_session_factory, tmp_path):
    wid = uuid.uuid4()
    (tmp_path / filename_for(Artifact.DRAFT)).write_text("## A\nBody a.\n")  # tight (no blank line)
    with pg_session_factory() as s:
        out = canonical.canonical_artifact_markdown(
            s, workspace_id=wid, artifact="draft", repo_dir=tmp_path
        )
    # Normalized round-trip inserts the blank line after the heading.
    assert out == pm_to_markdown(markdown_to_pm("## A\nBody a.\n"))


def test_canonical_empty_log_whitespace_head_no_crash(pg_session_factory, tmp_path):
    wid = uuid.uuid4()
    (tmp_path / filename_for(Artifact.DRAFT)).write_text("   \n")
    with pg_session_factory() as s:
        out = canonical.canonical_artifact_markdown(
            s, workspace_id=wid, artifact="draft", repo_dir=tmp_path
        )
    assert out == pm_to_markdown(canonical._EMPTY_DOC)  # no ValueError


def test_canonical_absent_file(pg_session_factory, tmp_path):
    # Absent file must return "" (not the empty-doc markdown) so the captured base
    # is byte-identical to an un-seeded room's serialization (""). The final review
    # adjudicated the original pm_to_markdown(_EMPTY_DOC) behaviour wrong because
    # hydration does NOT seed for an absent file — a false-stale mismatch resulted.
    wid = uuid.uuid4()
    with pg_session_factory() as s:
        out = canonical.canonical_artifact_markdown(
            s, workspace_id=wid, artifact="draft", repo_dir=tmp_path
        )
    assert out == ""


def test_markdown_to_pm_or_empty_maps_blank_to_empty_doc():
    assert canonical.markdown_to_pm_or_empty(None) == canonical._EMPTY_DOC
    assert canonical.markdown_to_pm_or_empty("   \n") == canonical._EMPTY_DOC
    assert canonical.markdown_to_pm_or_empty("## A\n")["type"] == "doc"


def test_hydrate_shares_the_canonicalizer():
    # Divergence guard: hydrate must use the SAME empty-doc canonicalizer, else
    # the read projection and the hydration seed drift and the false-stale bug
    # returns on the whitespace-HEAD path.
    from call_manager.proposal_assist.collab import hydrate

    assert hydrate.markdown_to_pm_or_empty is canonical.markdown_to_pm_or_empty
    assert hydrate._EMPTY_DOC is canonical._EMPTY_DOC
