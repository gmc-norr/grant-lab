"""JSON /api workspace settings + collaborator endpoints (Cockpit Slice D)."""

from sqlalchemy import func, select

from tests.proposal_assist._refs_helpers import add_member, authed_client, build_app
from tests.proposal_assist.conftest import make_user, make_workspace


def _setup(pg_session_factory):
    """Return (ws_id, owner_id, member_id, stranger_id). member is a viewer."""
    with pg_session_factory() as s:
        owner = make_user(s, email="owner@umu.se")
        member = make_user(s, email="member@umu.se")
        stranger = make_user(s, email="stranger@umu.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        add_member(s, workspace_id=ws.id, user=member)
        return ws.id, owner.id, member.id, stranger.id


def test_settings_get_owner_returns_fields(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).get(f"/api/workspaces/{ws_id}/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"title", "status", "deadline", "citation_style"}


def test_settings_get_member_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=member_id).get(f"/api/workspaces/{ws_id}/settings")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_settings_patch_owner_updates(monkeypatch, pg_url, tmp_path, pg_session_factory):
    from grant_lab.db.models import Workspace

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).patch(
        f"/api/workspaces/{ws_id}/settings",
        json={
            "title": "Renamed",
            "status": "submitted",
            "deadline": "2026-09-01",
            "citation_style": "author-year",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Renamed"
    assert r.json()["deadline"] == "2026-09-01"
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        assert ws.title == "Renamed" and ws.status == "submitted"
        assert ws.citation_style == "author-year" and ws.deadline is not None


def test_settings_patch_clears_deadline(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    c = authed_client(app, user_id=owner_id)
    c.patch(
        f"/api/workspaces/{ws_id}/settings",
        json={
            "title": "T",
            "status": "drafting",
            "deadline": "2026-09-01",
            "citation_style": "vancouver",
        },
    )
    r = c.patch(
        f"/api/workspaces/{ws_id}/settings",
        json={"title": "T", "status": "drafting", "deadline": None, "citation_style": "vancouver"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["deadline"] is None


def test_settings_patch_bad_status_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).patch(
        f"/api/workspaces/{ws_id}/settings",
        json={"title": "T", "status": "bogus", "deadline": None, "citation_style": "vancouver"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_settings_patch_bad_citation_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).patch(
        f"/api/workspaces/{ws_id}/settings",
        json={"title": "T", "status": "drafting", "deadline": None, "citation_style": "mla"},
    )
    assert r.status_code == 400


def test_settings_patch_empty_title_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).patch(
        f"/api/workspaces/{ws_id}/settings",
        json={
            "title": "   ",
            "status": "drafting",
            "deadline": None,
            "citation_style": "vancouver",
        },  # noqa: E501
    )
    assert r.status_code == 400


def test_settings_patch_member_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=member_id).patch(
        f"/api/workspaces/{ws_id}/settings",
        json={"title": "T", "status": "drafting", "deadline": None, "citation_style": "vancouver"},
    )
    assert r.status_code == 403


def _add_user(pg_session_factory, email):
    with pg_session_factory() as s:
        u = make_user(s, email=email)
        s.commit()
        return u.id


def test_members_add_owner_201_and_event(monkeypatch, pg_url, tmp_path, pg_session_factory):
    from grant_lab.db.models import WorkspaceEvent, WorkspaceViewer

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    new_id = _add_user(pg_session_factory, "newcollab@umu.se")
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/members", json={"email": "newcollab@umu.se"}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body == {"user_id": new_id, "email": "newcollab@umu.se", "role": "member"}
    with pg_session_factory() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(WorkspaceViewer)
                .where(WorkspaceViewer.workspace_id == ws_id, WorkspaceViewer.user_id == new_id)
            )
            == 1
        )
        ev = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "member_added"
            )
        )
        assert ev is not None and ev.actor_id == owner_id
        assert ev.payload["target_user_id"] == new_id
        assert ev.payload["target_email"] == "newcollab@umu.se"


def test_members_add_unknown_email_400_generic(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/members", json={"email": "nobody@umu.se"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_members_add_duplicate_idempotent_200(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, member_id, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/members", json={"email": "member@umu.se"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == member_id


def test_members_add_owner_email_idempotent_200(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/members", json={"email": "owner@umu.se"}
    )
    assert r.status_code == 200
    assert r.json()["role"] == "owner"


def test_members_add_non_owner_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=member_id).post(
        f"/api/workspaces/{ws_id}/members", json={"email": "member@umu.se"}
    )
    assert r.status_code == 403


def test_members_delete_owner_204_and_event(monkeypatch, pg_url, tmp_path, pg_session_factory):
    from grant_lab.db.models import WorkspaceEvent, WorkspaceViewer

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, member_id, _ = _setup(pg_session_factory)
    c = authed_client(app, user_id=owner_id)
    r = c.delete(f"/api/workspaces/{ws_id}/members/{member_id}")
    assert r.status_code == 204
    with pg_session_factory() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(WorkspaceViewer)
                .where(WorkspaceViewer.workspace_id == ws_id, WorkspaceViewer.user_id == member_id)
            )
            == 0
        )
        ev = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "member_removed"
            )
        )
        assert ev is not None and ev.payload["target_user_id"] == member_id
    # idempotent: deleting again is still 204, writes no second event
    assert c.delete(f"/api/workspaces/{ws_id}/members/{member_id}").status_code == 204
    with pg_session_factory() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(WorkspaceEvent)
                .where(
                    WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "member_removed"
                )
            )
            == 1
        )


def test_members_delete_non_owner_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, stranger_id = _setup(pg_session_factory)
    r = authed_client(app, user_id=stranger_id).delete(
        f"/api/workspaces/{ws_id}/members/{member_id}"
    )
    assert r.status_code == 403


def test_settings_patch_authorization_precedes_validation(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    """A non-owner sending a MALFORMED body must get 403, not 400.

    Slice C2 moved this route's body validation inside the session block, after
    require_owner. Validating first would answer someone with no access at all
    that their body shape was wrong — an authorization decision leaking through
    a validation error."""
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=member_id).patch(
        f"/api/workspaces/{ws_id}/settings",
        # Invalid on three counts: bad status, bad citation_style, empty title.
        json={"title": "", "status": "nonsense", "deadline": None, "citation_style": "nope"},
    )
    assert r.status_code == 403, (
        f"expected 403 (authorization decides first), got {r.status_code}: {r.text}"
    )


def test_add_member_no_op_paths_add_no_rows(monkeypatch, pg_url, tmp_path, pg_session_factory):
    """spec §6. add_member returns early from inside its write scope on two
    no-op paths, so `workspace_write` commits an empty transaction there.

    Asserted as NO NEW ROWS, not "no rows": the already-a-member path is
    reachable only by pre-seeding exactly the WorkspaceViewer row it detects, so
    "leaves no viewer" is unsatisfiable. If someone later adds a write above
    those returns, these counts move and this test says so.
    """
    from grant_lab.db.models import WorkspaceEvent, WorkspaceViewer

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    with pg_session_factory() as s:
        owner = make_user(s, email="noop-owner@umu.se")
        member = make_user(s, email="already-a-member@example.test")
        ws = make_workspace(s, owner=owner)
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=member.id))
        s.commit()
        ws_id, owner_id, member_email = ws.id, owner.id, member.email
        owner_email = owner.email

    def snapshot():
        with pg_session_factory() as s:
            viewers = {
                v.user_id
                for v in s.query(WorkspaceViewer).filter_by(workspace_id=ws_id).all()
            }
            events = s.query(WorkspaceEvent).filter_by(workspace_id=ws_id).count()
        return viewers, events

    c = authed_client(app, user_id=owner_id)
    for label, email in (("owner", owner_email), ("already-a-member", member_email)):
        before = snapshot()
        resp = c.post(f"/api/workspaces/{ws_id}/members", json={"email": email})
        assert resp.status_code == 200, f"{label}: expected 200, got {resp.status_code}"
        assert snapshot() == before, (
            f"{label} is a no-op path but it changed the database: "
            f"viewers/events went from {before} to {snapshot()}"
        )


def test_members_add_normalizes_email_case_and_surrounding_space(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    """`Alice@UMU.se  ` must reach `alice@umu.se`.

    `users.email` is stored lowercase (the DB enforces it) and `/auth/login` and
    `/auth/register` both `.strip().lower()` before they look an address up.
    `add_member` compared raw, so the owner typing a capital or leaving a
    trailing space from a copy-paste got the generic "could not add
    collaborator" — indistinguishable from "no such person" (review 2026-10-26
    A7: `'Alice@umu.se' -> 400`, `'  alice@umu.se' -> 400`).
    """
    from grant_lab.db.models import WorkspaceViewer

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    alice_id = _add_user(pg_session_factory, "alice@umu.se")
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/members", json={"email": "  Alice@UMU.se  "}
    )
    assert r.status_code == 201, r.text
    assert r.json() == {"user_id": alice_id, "email": "alice@umu.se", "role": "member"}
    with pg_session_factory() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(WorkspaceViewer)
                .where(WorkspaceViewer.workspace_id == ws_id, WorkspaceViewer.user_id == alice_id)
            )
            == 1
        )


def test_members_add_a_disabled_user_answers_exactly_like_an_unknown_one(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    """A non-active account must be invisible to share-by-email.

    Review 2026-10-26 A7 measured `'gone@umu.se' (disabled) -> 201` and
    `'pending@umu.se' -> 201`: the route admitted accounts that cannot log in,
    which both grants access to a parked address and tells the owner that
    address is registered. The two responses are compared to EACH OTHER, status
    and body, so the disabled answer cannot drift away from the unknown answer.
    """
    from grant_lab.db.models import WorkspaceViewer

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    with pg_session_factory() as s:
        gone = make_user(s, email="gone@umu.se")
        gone.status = "disabled"
        pend = make_user(s, email="pending@umu.se")
        pend.status = "pending"
        s.commit()
        gone_id, pend_id = gone.id, pend.id

    client = authed_client(app, user_id=owner_id)
    unknown = client.post(f"/api/workspaces/{ws_id}/members", json={"email": "nobody@umu.se"})
    disabled = client.post(f"/api/workspaces/{ws_id}/members", json={"email": "gone@umu.se"})
    pending = client.post(f"/api/workspaces/{ws_id}/members", json={"email": "pending@umu.se"})

    assert unknown.status_code == 400, unknown.text  # precondition
    assert (disabled.status_code, disabled.json()) == (unknown.status_code, unknown.json()), (
        f"a disabled user must be indistinguishable from an unknown one; "
        f"disabled={disabled.status_code} {disabled.text!r} "
        f"unknown={unknown.status_code} {unknown.text!r}"
    )
    assert (pending.status_code, pending.json()) == (unknown.status_code, unknown.json()), (
        f"a pending user must be indistinguishable from an unknown one; "
        f"pending={pending.status_code} {pending.text!r}"
    )
    with pg_session_factory() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(WorkspaceViewer)
                .where(
                    WorkspaceViewer.workspace_id == ws_id,
                    WorkspaceViewer.user_id.in_([gone_id, pend_id]),
                )
            )
            == 0
        ), "no viewer row may be created for a non-active account"
