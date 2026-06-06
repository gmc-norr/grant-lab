"""References web routes (Slice 1 — list + upload + raw serve)."""

import io

from fastapi.testclient import TestClient

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Reference, User, Workspace
from call_manager.main import create_app


def _build_app(monkeypatch, pg_url, tmp_path):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))
    return create_app()


def _authed(app, *, user_id):
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=user_id))
    return client


def _seed_owner_and_workspace(session, *, email="owner@ki.se"):
    owner = User(email=email)
    session.add(owner)
    session.flush()
    ws = Workspace(owner_id=owner.id, title="T")
    session.add(ws)
    session.commit()
    return owner.id, ws.id


def test_panel_renders_for_owner_empty_list(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    client = _authed(app, user_id=uid)
    try:
        resp = client.get(f"/workspaces/{wsid}/references")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    assert "references" in resp.text.lower()


def test_panel_returns_403_for_non_viewer(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        _, wsid = _seed_owner_and_workspace(s)
        other = User(email="other@ki.se")
        s.add(other)
        s.commit()
        other_id = other.id
    client = _authed(app, user_id=other_id)
    try:
        resp = client.get(f"/workspaces/{wsid}/references")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403


def test_upload_markdown_creates_row(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    client = _authed(app, user_id=uid)
    md = b"# Hello\n\nworld\n"
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references",
            data={"kind": "paper"},
            files={"file": ("hello.md", io.BytesIO(md), "text/markdown")},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text
    assert "hello.md" in resp.text or "Hello" in resp.text

    from sqlalchemy import select

    with pg_session_factory() as s:
        refs = list(s.scalars(select(Reference).where(Reference.workspace_id == wsid)))
    assert len(refs) == 1
    assert refs[0].text_status == "done"


def test_upload_returns_403_for_non_owner(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        _, wsid = _seed_owner_and_workspace(s)
        viewer = User(email="viewer@ki.se")
        s.add(viewer)
        s.commit()
        viewer_id = viewer.id
        # Add viewer permission (mirrors B7 pattern; viewers CAN read, can't upload).
        from call_manager.db.models import WorkspaceViewer

        s.add(WorkspaceViewer(workspace_id=wsid, user_id=viewer_id))
        s.commit()
    client = _authed(app, user_id=viewer_id)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references",
            data={"kind": "paper"},
            files={"file": ("x.md", io.BytesIO(b"# t"), "text/markdown")},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403


def test_upload_rejects_bad_mime(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    client = _authed(app, user_id=uid)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references",
            data={"kind": "paper"},
            files={"file": ("x.exe", io.BytesIO(b"MZ\x00\x00"), "application/x-msdownload")},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 400


def test_upload_failed_extraction_persists_row_with_failed_status(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Phase 2 failure: corrupted PDF; row + file retained; UI surfaces failure."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    client = _authed(app, user_id=uid)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references",
            data={"kind": "paper"},
            files={"file": ("bad.pdf", io.BytesIO(b"not really a pdf"), "application/pdf")},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text
    assert "failed" in resp.text.lower()

    from sqlalchemy import select

    with pg_session_factory() as s:
        refs = list(s.scalars(select(Reference).where(Reference.workspace_id == wsid)))
    assert len(refs) == 1
    assert refs[0].text_status == "failed"
    assert refs[0].text_error is not None


def test_raw_serves_bytes_with_attachment_disposition(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Codex High #3 rev4 — references serve a wide MIME range; force
    download with attachment disposition + nosniff to prevent app-origin
    XSS from HTML/SVG/etc."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    client = _authed(app, user_id=uid)
    md = b"# t\n"
    try:
        up = client.post(
            f"/workspaces/{wsid}/references",
            data={"kind": "paper"},
            files={"file": ("hello.md", io.BytesIO(md), "text/markdown")},
        )
        assert up.status_code == 200
        from sqlalchemy import select

        with pg_session_factory() as s:
            ref = s.scalar(select(Reference).where(Reference.workspace_id == wsid))
        resp = client.get(f"/workspaces/{wsid}/references/{ref.id}/raw")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    assert resp.content == md
    # Force download — NEVER inline.
    cd = resp.headers["content-disposition"]
    assert cd.startswith("attachment;")
    # nosniff to prevent MIME-sniffing into HTML.
    assert resp.headers["x-content-type-options"] == "nosniff"
    # FileResponse streams the file and sets Content-Length (rather than
    # buffering the whole file in memory via path.read_bytes()).
    assert int(resp.headers["content-length"]) == len(md)


def test_soft_delete_scrubs_reference_bytes(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """Compliance: soft-deleting a workspace must remove the references
    tree (original bytes + extracted text.txt) from disk, mirroring the
    attachment scrub. DB rows are intentionally retained for the audit
    trail; only the on-disk bytes are gone."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    client = _authed(app, user_id=uid)
    md = b"# Hello\n\nworld\n"
    try:
        up = client.post(
            f"/workspaces/{wsid}/references",
            data={"kind": "paper"},
            files={"file": ("hello.md", io.BytesIO(md), "text/markdown")},
        )
        assert up.status_code == 200, up.text

        refs_dir = tmp_path / str(wsid) / "references"
        assert refs_dir.exists(), "reference bytes should be on disk after upload"

        resp = client.post(f"/workspaces/{wsid}/delete", follow_redirects=False)
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code in (303, 307)
    assert not refs_dir.exists(), "references tree must be scrubbed on soft-delete"


def test_raw_returns_404_for_other_workspace_id(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """Cross-workspace IDOR check: passing the right ref_id but the
    wrong workspace_id in the URL returns 404, not 403 — don't leak
    existence."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid_a, wsid_a = _seed_owner_and_workspace(s, email="a@ki.se")
        uid_b, wsid_b = _seed_owner_and_workspace(s, email="b@ki.se")
    client = _authed(app, user_id=uid_a)
    md = b"# t\n"
    try:
        client.post(
            f"/workspaces/{wsid_a}/references",
            data={"kind": "paper"},
            files={"file": ("t.md", io.BytesIO(md), "text/markdown")},
        )
        from sqlalchemy import select

        with pg_session_factory() as s:
            ref = s.scalar(select(Reference).where(Reference.workspace_id == wsid_a))
        # Now: workspace_a's owner asks for ref_a's bytes via workspace_b's URL.
        resp = client.get(f"/workspaces/{wsid_b}/references/{ref.id}/raw")
    finally:
        client.__exit__(None, None, None)
    # owner_a isn't allowed on workspace_b → 403 from the workspace check.
    # That's fine (doesn't leak existence). Accept either 403 or 404.
    assert resp.status_code in (403, 404)


def test_raw_returns_404_when_same_owner_of_both_workspaces(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Residuals fix: previous test softly accepted 403 OR 404 because
    the owner check fires first. Tighten with a same-owner setup so the
    serve route reaches the cross-workspace check and returns 404
    (don't leak existence)."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        # ONE owner with TWO workspaces.
        owner = User(email="dual-owner@ki.se")
        s.add(owner)
        s.flush()
        ws_a = Workspace(owner_id=owner.id, title="A")
        ws_b = Workspace(owner_id=owner.id, title="B")
        s.add(ws_a)
        s.add(ws_b)
        s.commit()
        uid, wsid_a, wsid_b = owner.id, ws_a.id, ws_b.id

    client = _authed(app, user_id=uid)
    md = b"# t\n"
    try:
        up = client.post(
            f"/workspaces/{wsid_a}/references",
            data={"kind": "paper"},
            files={"file": ("hello.md", io.BytesIO(md), "text/markdown")},
        )
        assert up.status_code == 200
        from sqlalchemy import select

        with pg_session_factory() as s:
            ref = s.scalar(select(Reference).where(Reference.workspace_id == wsid_a))
        # Same owner; owner check passes for wsid_b too. The serve
        # route's ref.workspace_id != ws.id check is what should 404.
        resp = client.get(f"/workspaces/{wsid_b}/references/{ref.id}/raw")
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 404, (
        f"cross-workspace serve must return 404 (not leak existence); got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Slice 1B Task 6 — metadata review/correct/approve routes
# ---------------------------------------------------------------------------


def _seed_reference_with_extracted(session, *, workspace_id, uploaded_by):
    """Seed a Reference row with csl_json_extracted set."""
    ref = Reference(
        workspace_id=workspace_id,
        kind="paper",
        source_type="uploaded",
        filename="test.pdf",
        storage_path="fake/path",
        mime_type="application/pdf",
        uploaded_by=uploaded_by,
        csl_json_extracted={
            "title": "Extracted",
            "author": [{"family": "Smith", "given": "J"}],
            "issued": {"date-parts": [[2021]]},
        },
        metadata_status="extracted",
    )
    session.add(ref)
    session.commit()
    return ref.id


def test_metadata_get_owner_sees_extracted(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """GET /workspaces/<id>/references/<id>/metadata as owner returns 200
    and renders the extracted CSL data (title + first author)."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
        ref_id = _seed_reference_with_extracted(s, workspace_id=wsid, uploaded_by=uid)
    client = _authed(app, user_id=uid)
    try:
        resp = client.get(f"/workspaces/{wsid}/references/{ref_id}/metadata")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    assert "Extracted" in resp.text
    assert "Smith" in resp.text


def test_metadata_post_owner_approves(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """POST as owner with edited fields produces an approved Reference."""
    from sqlalchemy import select

    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
        ref_id = _seed_reference_with_extracted(s, workspace_id=wsid, uploaded_by=uid)
    client = _authed(app, user_id=uid)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references/{ref_id}/metadata",
            data={
                "title": "Corrected Title",
                "authors": "Roe, Jane\nDoe, John",
                "year": "2022",
                "venue": "Nature",
                "doi": "10/x",
            },
            follow_redirects=False,
        )
    finally:
        client.__exit__(None, None, None)
    # Expect a redirect (303) to the references list.
    assert resp.status_code == 303

    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.id == ref_id))
    assert ref.metadata_status == "approved"
    assert ref.csl_json["title"] == "Corrected Title"
    assert ref.csl_json["author"] == [
        {"family": "Roe", "given": "Jane"},
        {"family": "Doe", "given": "John"},
    ]
    assert ref.csl_json["issued"] == {"date-parts": [[2022]]}
    assert ref.csl_json["container-title"] == "Nature"
    assert ref.csl_json["DOI"] == "10/x"
    assert ref.title == "Corrected Title"


def test_metadata_get_non_owner_returns_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """Non-owner GET on metadata review → 403."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
        ref_id = _seed_reference_with_extracted(s, workspace_id=wsid, uploaded_by=uid)
        other = User(email="noowner@ki.se")
        s.add(other)
        s.commit()
        other_id = other.id
    client = _authed(app, user_id=other_id)
    try:
        resp = client.get(f"/workspaces/{wsid}/references/{ref_id}/metadata")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403


def test_metadata_post_non_owner_returns_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """Non-owner POST on metadata review → 403."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
        ref_id = _seed_reference_with_extracted(s, workspace_id=wsid, uploaded_by=uid)
        other = User(email="noowner2@ki.se")
        s.add(other)
        s.commit()
        other_id = other.id
    client = _authed(app, user_id=other_id)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references/{ref_id}/metadata",
            data={"title": "X"},
            follow_redirects=False,
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403


def test_metadata_get_cross_workspace_returns_404(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Reference belonging to a different workspace → 404 (cross-workspace IDOR)."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        # Owner has two workspaces; ref is in ws_a.
        owner = User(email="cross-owner@ki.se")
        s.add(owner)
        s.flush()
        ws_a = Workspace(owner_id=owner.id, title="A")
        ws_b = Workspace(owner_id=owner.id, title="B")
        s.add(ws_a)
        s.add(ws_b)
        s.commit()
        uid, wsid_a, wsid_b = owner.id, ws_a.id, ws_b.id
        ref_id = _seed_reference_with_extracted(s, workspace_id=wsid_a, uploaded_by=uid)
    client = _authed(app, user_id=uid)
    try:
        # Fetch ref_a's metadata via ws_b's URL.
        resp = client.get(f"/workspaces/{wsid_b}/references/{ref_id}/metadata")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 404


def test_metadata_post_cross_workspace_returns_404(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """POST to ws_b's metadata URL with ws_a's reference id → 404 (cross-workspace IDOR)."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        # Same owner with two workspaces; ref lives in ws_a.
        owner = User(email="cross-post-owner@ki.se")
        s.add(owner)
        s.flush()
        ws_a = Workspace(owner_id=owner.id, title="A")
        ws_b = Workspace(owner_id=owner.id, title="B")
        s.add(ws_a)
        s.add(ws_b)
        s.commit()
        uid, wsid_a, wsid_b = owner.id, ws_a.id, ws_b.id
        ref_id = _seed_reference_with_extracted(s, workspace_id=wsid_a, uploaded_by=uid)
    client = _authed(app, user_id=uid)
    try:
        # Post to ws_b's URL with ws_a's ref_id — must not update anything.
        resp = client.post(
            f"/workspaces/{wsid_b}/references/{ref_id}/metadata",
            data={"title": "Should not persist"},
            follow_redirects=False,
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 404


def test_metadata_author_family_only_round_trips(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """Structured family-only authors must survive the textarea round-trip.

    ``{"family": "WHO"}`` → ``_authors_to_lines`` → ``"WHO,"``
    → ``_parse_authors`` → ``{"family": "WHO"}``  (NOT ``{"literal": "WHO"}``).

    Also verifies that "Roe, Jane" still produces ``{"family":"Roe","given":"Jane"}``.
    """
    from sqlalchemy import select

    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s, email="roundtrip@ki.se")
        ref_id = _seed_reference_with_extracted(s, workspace_id=wsid, uploaded_by=uid)
    client = _authed(app, user_id=uid)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references/{ref_id}/metadata",
            data={
                "title": "RT Test",
                # "WHO," — trailing comma signals structured family-only
                # "Roe, Jane" — standard family, given
                "authors": "WHO,\nRoe, Jane",
                "year": "2024",
            },
            follow_redirects=False,
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 303

    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.id == ref_id))
    authors = ref.csl_json["author"]
    # Family-only author must remain structured (not demoted to literal).
    assert {"family": "WHO"} in authors, f"expected structured family, got: {authors}"
    # Standard family+given must still work.
    assert {"family": "Roe", "given": "Jane"} in authors, f"expected Roe/Jane, got: {authors}"
