"""D9: tell a board's profile owner when it has gone underfull (Plan C Task 8).

THE TRIGGER (Open-2): a PUBLISHED board whose count of RENDERABLE approved
items (board_items joined to calls with a future deadline) is below its
STORED items_per_page (D7 -- never re-derived from orientation).

THE RECIPIENT (D9): `profiles.created_by` for the board's OWN profile, not
`boards.created_by` -- an admin can build a board on a profile they do not
own (spec Sec 4.3), and telling them a queue they do not own is short-telling
the wrong person.

THE ESCALATION (Open-3): when the owner is undeliverable -- `User.status !=
'active'` OR `digest_disabled_at IS NOT NULL` (the same two conditions
`digest/send.py:38` filters on) -- every active admin is notified instead,
and `owner_undeliverable` is True so the board detail screen can say so.

FIXTURES. `pg_session_factory` (tests/conftest.py:151) is a FACTORY --
`with pg_session_factory() as s:`. `profile_factory` (:184) returns a
`(profile_id, user_id)` tuple and, unlike the real `POST /api/profiles`
route (`profiles/create.py:79`), does NOT insert a `Subscription` row for
the owner -- that absence is exactly the fixture
`test_a_MISSING_subscription_row_is_reported_not_skipped` needs, and every
other test below must call `_subscribe` explicitly or it would
(incorrectly) hit that same path.

## Fix round 1 additions (Task 8 review)

- Important 1: `send_board_alerts` is the sending half of this module (the
  pure `boards_needing_attention` above never had a caller). Its own tests
  use `_FakeEmail`/`_FakeSettings`, the SAME fake/recording shape
  `tests/test_digest_send.py` uses for `run_digest` -- no test here makes a
  live network call.
- Important 2: `test_owner_undeliverable_is_surfaced_on_the_board_detail_route`
  now calls `_subscribe` so its "owner reachable" half is genuinely backed by
  a Subscription row, and a new test pins the missing-row case the SAME way
  on both `boards_needing_attention` and the detail route.
- Important 3/4: `test_a_DISABLED_owner_escalates_to_active_admins` gained a
  second, disabled admin who must NOT appear in `recipient_ids`; a new test
  does the same for a bounce-disabled admin.
- Important 5: a new test pins "muted AND undeliverable still escalates";
  `test_board_alerts_enabled_false_suppresses_the_alert` already pins the
  other half ("muted AND deliverable produces no alert" -- its owner is
  never touched, so it stays reachable) and is annotated to say so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from grant_lab.boards.alerts import boards_needing_attention, send_board_alerts
from grant_lab.db.models import Board, BoardItem, Call, Source, Subscription, User
from grant_lab.email.client import SendResult

FUTURE = datetime.now(UTC) + timedelta(days=30)
PAST = datetime.now(UTC) - timedelta(days=1)


@dataclass
class _FakeSettings:
    """Settings duck-type stand-in -- send_board_alerts only reads this
    field. Same shape as tests/test_digest_send.py's own _FakeSettings."""

    admin_dashboard_url: str = "http://test/"


class _FakeEmail:
    """Records every send() call instead of making one. Same shape as
    tests/test_digest_send.py's own _FakeEmail (no live network call)."""

    def __init__(self):
        self.calls = []

    def send(self, *, to, subject, html, text):
        self.calls.append({"to": to, "subject": subject, "html": html, "text": text})
        return SendResult(ok=True, message_id=f"msg-{len(self.calls)}")


def _send_alerts(pg_session_factory, email_client=None):
    email_client = email_client if email_client is not None else _FakeEmail()
    summary = send_board_alerts(
        session_factory=pg_session_factory,
        email_client=email_client,
        settings=_FakeSettings(),
    )
    return email_client, summary


def _make_board(
    pg_session_factory, *, profile_id, created_by, slug, items_per_page=8, is_published=True
):
    with pg_session_factory() as s:
        b = Board(
            slug=slug,
            title="Corridor board",
            profile_id=profile_id,
            created_by=created_by,
            items_per_page=items_per_page,
            is_published=is_published,
        )
        s.add(b)
        s.commit()
        return b.id


def _make_call(pg_session_factory, *, external_id, deadline=FUTURE):
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id=external_id,
            url=f"https://example.org/{external_id}",
            title=f"Call {external_id}",
            deadline=deadline,
        )
        s.add(c)
        s.commit()
        return c.id


def _approve(pg_session_factory, *, board_id, call_id, approved_by, position=0):
    with pg_session_factory() as s:
        s.add(
            BoardItem(
                board_id=board_id, call_id=call_id, position=position, approved_by=approved_by
            )
        )
        s.commit()


def _approve_n_future(pg_session_factory, *, board_id, approved_by, n, prefix):
    for i in range(n):
        call_id = _make_call(pg_session_factory, external_id=f"{prefix}-{i}")
        _approve(pg_session_factory, board_id=board_id, call_id=call_id, approved_by=approved_by)


def _new_user(
    pg_session_factory,
    email,
    *,
    is_admin=False,
    status="active",
    digest_disabled_at=None,
):
    with pg_session_factory() as s:
        u = User(
            email=email,
            is_admin=is_admin,
            status=status,
            digest_disabled_at=digest_disabled_at,
        )
        s.add(u)
        s.commit()
        return u.id


def _subscribe(
    pg_session_factory, *, user_id, profile_id, digest_enabled=True, board_alerts_enabled=True
):
    with pg_session_factory() as s:
        s.add(
            Subscription(
                user_id=user_id,
                profile_id=profile_id,
                digest_enabled=digest_enabled,
                board_alerts_enabled=board_alerts_enabled,
            )
        )
        s.commit()


def _run_alerts(pg_session_factory):
    with pg_session_factory() as s:
        return boards_needing_attention(s)


def _get_user(pg_session_factory, user_id):
    with pg_session_factory() as s:
        return s.get(User, user_id)


def _alert_for(alerts, board_id):
    matches = [a for a in alerts if a.board_id == board_id]
    assert len(matches) == 1, f"expected exactly one alert for board {board_id}, got {matches!r}"
    return matches[0]


def test_an_underfull_published_board_alerts_its_PROFILE_OWNER(pg_session_factory, profile_factory):
    pid, owner_id = profile_factory(name="Underfull Owner")
    _subscribe(pg_session_factory, user_id=owner_id, profile_id=pid)
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="a" * 16)
    _approve_n_future(
        pg_session_factory, board_id=board_id, approved_by=owner_id, n=3, prefix="underfull"
    )

    alert = _alert_for(_run_alerts(pg_session_factory), board_id)

    assert alert.shortfall == 5  # items_per_page (8) - renderable (3)
    assert alert.recipient_ids == [owner_id]
    assert alert.owner_undeliverable is False


def test_a_FULL_board_does_not_alert(pg_session_factory, profile_factory):
    pid, owner_id = profile_factory(name="Full Board Owner")
    _subscribe(pg_session_factory, user_id=owner_id, profile_id=pid)
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="b" * 16)
    _approve_n_future(
        pg_session_factory, board_id=board_id, approved_by=owner_id, n=8, prefix="full"
    )

    alerts = _run_alerts(pg_session_factory)

    assert board_id not in [a.board_id for a in alerts]


def test_an_UNPUBLISHED_underfull_board_does_not_alert(pg_session_factory, profile_factory):
    pid, owner_id = profile_factory(name="Unpublished Owner")
    _subscribe(pg_session_factory, user_id=owner_id, profile_id=pid)
    board_id = _make_board(
        pg_session_factory,
        profile_id=pid,
        created_by=owner_id,
        slug="c" * 16,
        is_published=False,
    )
    _approve_n_future(
        pg_session_factory, board_id=board_id, approved_by=owner_id, n=1, prefix="unpublished"
    )

    alerts = _run_alerts(pg_session_factory)

    assert board_id not in [a.board_id for a in alerts]


def test_the_shortfall_counts_against_the_STORED_items_per_page(
    pg_session_factory, profile_factory
):
    """D7: items_per_page is an override an author may have set directly, not
    re-derived from ORIENTATION_ITEMS_PER_PAGE. A board stored at 3 (not the
    landscape default of 8) with 1 renderable item has a shortfall of 2, not 7
    -- proving the code reads the STORED column."""
    pid, owner_id = profile_factory(name="Stored Cap Owner")
    _subscribe(pg_session_factory, user_id=owner_id, profile_id=pid)
    board_id = _make_board(
        pg_session_factory,
        profile_id=pid,
        created_by=owner_id,
        slug="d" * 16,
        items_per_page=3,
    )
    _approve_n_future(
        pg_session_factory, board_id=board_id, approved_by=owner_id, n=1, prefix="stored"
    )

    alert = _alert_for(_run_alerts(pg_session_factory), board_id)

    assert alert.shortfall == 2


def test_an_item_whose_deadline_passed_counts_as_MISSING(pg_session_factory, profile_factory):
    pid, owner_id = profile_factory(name="Expired Item Owner")
    _subscribe(pg_session_factory, user_id=owner_id, profile_id=pid)
    board_id = _make_board(
        pg_session_factory,
        profile_id=pid,
        created_by=owner_id,
        slug="e" * 16,
        items_per_page=2,
    )
    live_call = _make_call(pg_session_factory, external_id="still-open", deadline=FUTURE)
    expired_call = _make_call(pg_session_factory, external_id="expired", deadline=PAST)
    _approve(pg_session_factory, board_id=board_id, call_id=live_call, approved_by=owner_id)
    _approve(
        pg_session_factory,
        board_id=board_id,
        call_id=expired_call,
        approved_by=owner_id,
        position=1,
    )

    alert = _alert_for(_run_alerts(pg_session_factory), board_id)

    # items_per_page(2) - renderable(1, the expired one does not count) == 1.
    assert alert.shortfall == 1


def test_the_BOARD_CREATOR_is_not_notified_when_they_are_not_the_owner(
    pg_session_factory, profile_factory
):
    pid, owner_id = profile_factory(name="Admin Built This")
    admin_id = _new_user(pg_session_factory, "admin-builder@umu.se", is_admin=True)
    _subscribe(pg_session_factory, user_id=owner_id, profile_id=pid)
    # The admin, not the owner, creates the board -- exactly what a mutation
    # keying off board.created_by would coincidentally get right if the two
    # were the same person.
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=admin_id, slug="f" * 16)
    _approve_n_future(
        pg_session_factory, board_id=board_id, approved_by=admin_id, n=1, prefix="admin-built"
    )

    alert = _alert_for(_run_alerts(pg_session_factory), board_id)

    assert alert.recipient_ids == [owner_id]
    assert admin_id not in alert.recipient_ids


def test_board_alerts_enabled_false_suppresses_the_alert(pg_session_factory, profile_factory):
    """Also THE test for Important 5's other half: this owner is never
    touched, so they stay reachable (`deliverable_clause()` passes) -- a
    muted AND deliverable owner produces no alert. The paired half, a muted
    AND UNDELIVERABLE owner escalating anyway, is
    `test_a_muted_AND_undeliverable_owner_still_escalates` below."""
    pid, owner_id = profile_factory(name="Muted Owner")
    _subscribe(
        pg_session_factory,
        user_id=owner_id,
        profile_id=pid,
        digest_enabled=True,
        board_alerts_enabled=False,
    )
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="g" * 16)
    _approve_n_future(
        pg_session_factory, board_id=board_id, approved_by=owner_id, n=1, prefix="muted"
    )

    alerts = _run_alerts(pg_session_factory)

    assert board_id not in [a.board_id for a in alerts]


def test_digest_enabled_false_does_NOT_suppress_the_board_alert(
    pg_session_factory, profile_factory
):
    """THE test for Open-1: the digest mute and the board-alert mute are two
    different columns. If this plan's flag decision is overruled in review,
    this is the test that must change."""
    pid, owner_id = profile_factory(name="Digest Muted Owner")
    _subscribe(
        pg_session_factory,
        user_id=owner_id,
        profile_id=pid,
        digest_enabled=False,
        board_alerts_enabled=True,
    )
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="h" * 16)
    _approve_n_future(
        pg_session_factory, board_id=board_id, approved_by=owner_id, n=1, prefix="digest-muted"
    )

    alert = _alert_for(_run_alerts(pg_session_factory), board_id)

    assert alert.recipient_ids == [owner_id]
    assert alert.owner_undeliverable is False


def test_a_DISABLED_owner_escalates_to_active_admins(pg_session_factory, profile_factory):
    """Important 3: every escalation test used to create exactly one admin,
    and that admin was active -- so deleting the `status` filter out of
    `_active_admin_ids` reddened nothing. A second, DISABLED admin here
    means the filter actually has something to exclude."""
    pid, owner_id = profile_factory(name="Disabled Owner")
    with pg_session_factory() as s:
        s.get(User, owner_id).status = "disabled"
        s.commit()
    _subscribe(pg_session_factory, user_id=owner_id, profile_id=pid)
    admin_id = _new_user(pg_session_factory, "active-admin-1@umu.se", is_admin=True)
    disabled_admin_id = _new_user(
        pg_session_factory, "disabled-admin-1@umu.se", is_admin=True, status="disabled"
    )
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="i" * 16)
    _approve_n_future(
        pg_session_factory, board_id=board_id, approved_by=owner_id, n=1, prefix="disabled-owner"
    )

    alert = _alert_for(_run_alerts(pg_session_factory), board_id)

    assert alert.recipient_ids == [admin_id]
    assert owner_id not in alert.recipient_ids
    assert disabled_admin_id not in alert.recipient_ids
    assert alert.owner_undeliverable is True


def test_a_BOUNCE_DISABLED_admin_is_excluded_from_escalation(pg_session_factory, profile_factory):
    """Important 4: `_active_admin_ids` must use `deliverable_clause()`
    (status AND digest_disabled_at), not `status == 'active'` alone --
    otherwise a bounce-disabled admin receives an escalation that exists
    precisely BECAUSE someone's mail bounces."""
    pid, owner_id = profile_factory(name="Disabled Owner For Bounce Admin Test")
    with pg_session_factory() as s:
        s.get(User, owner_id).status = "disabled"
        s.commit()
    _subscribe(pg_session_factory, user_id=owner_id, profile_id=pid)
    admin_id = _new_user(pg_session_factory, "active-admin-4@umu.se", is_admin=True)
    bounce_admin_id = _new_user(
        pg_session_factory,
        "bounce-admin-1@umu.se",
        is_admin=True,
        digest_disabled_at=datetime.now(UTC),
    )
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="n" * 16)
    _approve_n_future(
        pg_session_factory, board_id=board_id, approved_by=owner_id, n=1, prefix="bounce-admin"
    )

    alert = _alert_for(_run_alerts(pg_session_factory), board_id)

    assert alert.recipient_ids == [admin_id]
    assert bounce_admin_id not in alert.recipient_ids
    assert alert.owner_undeliverable is True


def test_a_BOUNCE_DISABLED_owner_escalates_too(pg_session_factory, profile_factory):
    pid, owner_id = profile_factory(name="Bounce Disabled Owner")
    with pg_session_factory() as s:
        u = s.get(User, owner_id)
        u.digest_bounce_count = 3
        u.digest_disabled_at = datetime.now(UTC)
        s.commit()
    _subscribe(pg_session_factory, user_id=owner_id, profile_id=pid)
    admin_id = _new_user(pg_session_factory, "active-admin-2@umu.se", is_admin=True)
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="j" * 16)
    _approve_n_future(
        pg_session_factory, board_id=board_id, approved_by=owner_id, n=1, prefix="bounce-disabled"
    )

    alert = _alert_for(_run_alerts(pg_session_factory), board_id)

    assert alert.recipient_ids == [admin_id]
    assert owner_id not in alert.recipient_ids
    assert alert.owner_undeliverable is True


def test_a_bounce_disabled_owner_is_still_status_active(pg_session_factory, profile_factory):
    """Guards the fixture the previous test relies on: a bounce-disabled
    owner (digest_disabled_at set by BOUNCE_DISABLE_THRESHOLD bounces,
    send.py:183-184) keeps `status == 'active'`. If a future edit to that
    fixture accidentally also flipped status, the escalation test above would
    keep passing for the WRONG reason (the status branch of the OR, not the
    digest_disabled_at branch) and this distinction would silently stop being
    tested at all."""
    _pid, owner_id = profile_factory(name="Status Stays Active")
    with pg_session_factory() as s:
        u = s.get(User, owner_id)
        u.digest_bounce_count = 3
        u.digest_disabled_at = datetime.now(UTC)
        s.commit()

    owner = _get_user(pg_session_factory, owner_id)

    assert owner.status == "active"
    assert owner.digest_disabled_at is not None


def test_a_muted_AND_undeliverable_owner_still_escalates(pg_session_factory, profile_factory):
    """Important 5: the CONTROLLER's decision, implemented -- deliverability
    is evaluated BEFORE the mute for the escalation path. An owner who has
    left the organization (status='disabled') AND muted board alerts on the
    way out must still escalate to admins: `board_alerts_enabled` is a
    per-user "don't email ME" preference, it cannot speak for admins, and an
    undeliverable owner isn't receiving anything anyway. Before this fix,
    `_alert_for` checked the mute FIRST and returned None here -- silencing
    the board permanently, with nobody told."""
    pid, owner_id = profile_factory(name="Muted And Disabled Owner")
    with pg_session_factory() as s:
        s.get(User, owner_id).status = "disabled"
        s.commit()
    _subscribe(
        pg_session_factory,
        user_id=owner_id,
        profile_id=pid,
        board_alerts_enabled=False,
    )
    admin_id = _new_user(pg_session_factory, "active-admin-5@umu.se", is_admin=True)
    board_id = _make_board(
        pg_session_factory, profile_id=pid, created_by=owner_id, slug="o" * 16
    )
    _approve_n_future(
        pg_session_factory, board_id=board_id, approved_by=owner_id, n=1, prefix="muted-disabled"
    )

    alert = _alert_for(_run_alerts(pg_session_factory), board_id)

    assert alert.recipient_ids == [admin_id]
    assert owner_id not in alert.recipient_ids
    assert alert.owner_undeliverable is True


def test_a_MISSING_subscription_row_is_reported_not_skipped(pg_session_factory, profile_factory):
    """D10: the per-(user, profile) Subscription row is a CONVENTION, not a
    constraint (no CHECK, FK or unique index enforces it). `profile_factory`
    does not insert one, so this test never calls `_subscribe` at all -- a
    `for sub in subscriptions:` loop would silently skip this board and pass
    every other test in this file; asserting the row and reporting the
    absence (the same way an undeliverable owner is reported) is the fix."""
    pid, owner_id = profile_factory(name="No Subscription Row")
    admin_id = _new_user(pg_session_factory, "active-admin-3@umu.se", is_admin=True)
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="k" * 16)
    _approve_n_future(
        pg_session_factory, board_id=board_id, approved_by=owner_id, n=1, prefix="no-subscription"
    )

    alert = _alert_for(_run_alerts(pg_session_factory), board_id)

    assert alert.recipient_ids == [admin_id]
    assert owner_id not in alert.recipient_ids
    assert alert.owner_undeliverable is True


def test_owner_undeliverable_is_surfaced_on_the_board_detail_route(
    monkeypatch, pg_url, pg_session_factory, profile_factory, test_client_factory
):
    """Open-3 half 2: 'a delivery the system knows it suppressed is the one
    case where an in-app surface is not optional.' Reads the SAME board twice
    through GET /api/boards/{id} as an ADMIN (whose own session survives the
    owner's status flip): once while the owner is reachable, once after.

    Important 2 fix: this test now calls `_subscribe` so the first read's
    `False` is genuinely backed by a subscribed, reachable owner -- before
    the fix, this board had NO Subscription row (profile_factory never
    inserts one) and the route still said `False`, while
    `test_a_MISSING_subscription_row_is_reported_not_skipped` proved
    `boards_needing_attention` would treat the identical setup as
    `owner_undeliverable=True`. See
    `test_a_MISSING_subscription_row_reads_the_SAME_on_the_detail_route`
    below for that exact contradiction, now pinned as agreeing."""
    from grant_lab.main import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()

    pid, owner_id = profile_factory(name="Detail Route Owner")
    _subscribe(pg_session_factory, user_id=owner_id, profile_id=pid)
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="l" * 16)
    admin_id = _new_user(pg_session_factory, "detail-admin@umu.se", is_admin=True)

    client = test_client_factory(app)
    client.cookies.set("session", app.state.session_signer.make(user_id=admin_id))

    r = client.get(f"/api/boards/{board_id}")
    assert r.status_code == 200
    assert r.json()["owner_undeliverable"] is False

    with pg_session_factory() as s:
        s.get(User, owner_id).status = "disabled"
        s.commit()

    r = client.get(f"/api/boards/{board_id}")
    assert r.status_code == 200
    assert r.json()["owner_undeliverable"] is True


def test_a_MISSING_subscription_row_reads_the_SAME_on_the_detail_route(
    monkeypatch, pg_url, pg_session_factory, profile_factory, test_client_factory
):
    """Important 2: the exact contradiction the review found.
    `profile_factory` inserts no Subscription row; without the fix,
    `owner_undeliverable_map` (which feeds this route) ignored that and said
    `False`, while `boards_needing_attention`/`_alert_for` (Gate C,
    `test_a_MISSING_subscription_row_is_reported_not_skipped`) would
    escalate the SAME board with `owner_undeliverable=True`. Both must now
    say `True` for a board with no Subscription row, whether or not it is
    actually underfull (this route never checks items_per_page)."""
    from grant_lab.main import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()

    pid, owner_id = profile_factory(name="No Subscription Detail Route Owner")
    # Deliberately no _subscribe() call -- see test_a_MISSING_subscription_
    # row_is_reported_not_skipped for why this is the case D10 exists for.
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="p" * 16)
    admin_id = _new_user(pg_session_factory, "detail-admin-2@umu.se", is_admin=True)

    client = test_client_factory(app)
    client.cookies.set("session", app.state.session_signer.make(user_id=admin_id))

    r = client.get(f"/api/boards/{board_id}")

    assert r.status_code == 200
    assert r.json()["owner_undeliverable"] is True


def test_send_board_alerts_emails_the_reachable_owner(pg_session_factory, profile_factory):
    """Important 1: `boards_needing_attention` had no caller anywhere in
    `src/`, so no board alert was ever delivered. This is the sending half:
    a reachable, subscribed owner gets exactly one message, addressed to
    their own email."""
    pid, owner_id = profile_factory(name="Send Alerts Owner")
    _subscribe(pg_session_factory, user_id=owner_id, profile_id=pid)
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="q" * 16)
    _approve_n_future(
        pg_session_factory, board_id=board_id, approved_by=owner_id, n=1, prefix="send-owner"
    )
    owner_email = _get_user(pg_session_factory, owner_id).email

    fake_email, summary = _send_alerts(pg_session_factory)

    assert summary.alerts_found == 1
    assert summary.messages_sent == 1
    assert summary.transport_errors == 0
    assert summary.no_recipients == 0
    assert [c["to"] for c in fake_email.calls] == [owner_email]
    assert "Corridor board" in fake_email.calls[0]["subject"]
    assert "7" in fake_email.calls[0]["subject"]  # shortfall: items_per_page(8) - renderable(1)


def test_send_board_alerts_emails_admins_not_the_owner_when_escalated(
    pg_session_factory, profile_factory
):
    """An escalated alert's message goes to the admin addresses and NOT the
    owner's -- the owner here is undeliverable (status='disabled'), so a
    mutation that addressed the owner anyway (e.g. reading `[owner_id]`
    unconditionally) would send to an address the escalation exists to
    avoid."""
    pid, owner_id = profile_factory(name="Send Alerts Escalated Owner")
    with pg_session_factory() as s:
        s.get(User, owner_id).status = "disabled"
        s.commit()
    _subscribe(pg_session_factory, user_id=owner_id, profile_id=pid)
    admin_id = _new_user(pg_session_factory, "send-alerts-admin@umu.se", is_admin=True)
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="r" * 16)
    _approve_n_future(
        pg_session_factory, board_id=board_id, approved_by=owner_id, n=1, prefix="send-escalated"
    )
    owner_email = _get_user(pg_session_factory, owner_id).email
    admin_email = _get_user(pg_session_factory, admin_id).email

    fake_email, summary = _send_alerts(pg_session_factory)

    assert summary.messages_sent == 1
    recipients = [c["to"] for c in fake_email.calls]
    assert recipients == [admin_email]
    assert owner_email not in recipients


def test_send_board_alerts_sends_nothing_for_a_muted_board(pg_session_factory, profile_factory):
    """A board_alerts_enabled=False board produces no message: it never even
    becomes a BoardAlert (`_alert_for` returns None), so send_board_alerts
    has nothing to iterate for it."""
    pid, owner_id = profile_factory(name="Send Alerts Muted Owner")
    _subscribe(
        pg_session_factory,
        user_id=owner_id,
        profile_id=pid,
        board_alerts_enabled=False,
    )
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="s" * 16)
    _approve_n_future(
        pg_session_factory, board_id=board_id, approved_by=owner_id, n=1, prefix="send-muted"
    )

    fake_email, summary = _send_alerts(pg_session_factory)

    assert summary.alerts_found == 0
    assert summary.messages_sent == 0
    assert fake_email.calls == []


def test_send_board_alerts_counts_but_does_not_send_when_recipients_are_empty(
    pg_session_factory, profile_factory
):
    """Minor 6: an alert addressed to nobody (owner undeliverable AND zero
    deliverable admins exist) must not look like a delivered one --
    send_board_alerts must not crash on an empty recipient list, must send
    nothing, and must count it distinctly via `no_recipients` rather than
    silently reporting `messages_sent == 0` with no way to tell "nothing was
    wrong" from "nobody could be told"."""
    pid, owner_id = profile_factory(name="No Admins At All Owner")
    with pg_session_factory() as s:
        s.get(User, owner_id).status = "disabled"
        s.commit()
    _subscribe(pg_session_factory, user_id=owner_id, profile_id=pid)
    # Deliberately zero admins anywhere in this database.
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="u" * 16)
    _approve_n_future(
        pg_session_factory, board_id=board_id, approved_by=owner_id, n=1, prefix="no-admins"
    )

    fake_email, summary = _send_alerts(pg_session_factory)

    assert summary.alerts_found == 1
    assert summary.messages_sent == 0
    assert summary.no_recipients == 1
    assert fake_email.calls == []
