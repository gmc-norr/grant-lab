from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from call_manager.db.models import (
    DigestRun,
    User,
)
from call_manager.digest.send import run_digest
from call_manager.email.client import EmailTransportError, SendResult


@dataclass
class _FakeSettings:
    """Settings duck-type stand-in. run_digest only reads these two fields."""

    digest_dry_run: bool = False
    admin_dashboard_url: str = "http://test/"


class _FakeEmail:
    def __init__(self, results=None, error_at=None):
        # results: list of SendResult to return in order; error_at: index → raise
        self._results = list(results or [])
        self._error_at = error_at or {}
        self.calls = []

    def send(self, *, to, subject, html, text):
        idx = len(self.calls)
        self.calls.append({"to": to, "subject": subject, "html": html, "text": text})
        if idx in self._error_at:
            raise self._error_at[idx]
        if self._results:
            return self._results.pop(0)
        return SendResult(ok=True, message_id=f"msg-{idx}")


def _seed_user_with_payload(s, *, email):
    """Reuse the helper from test_digest_assemble — copy/paste shape here."""
    from call_manager.db.models import (
        Call,
        CallRanking,
        Profile,
        ProfileKind,
        Source,
        Subscription,
    )

    u = User(email=email)
    s.add(u)
    s.flush()
    p = Profile(
        name=f"P-{email}",
        kind=ProfileKind.GROUP,
        description="",
        structured={
            "areas": [],
            "budget_min": None,
            "budget_max": None,
            "trl_min": None,
            "trl_max": None,
            "must_include": [],
            "hard_exclusions": [],
        },
        created_by=u.id,
    )
    s.add(p)
    s.flush()
    s.add(Subscription(user_id=u.id, profile_id=p.id, digest_enabled=True))
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id,
        external_id=f"E-{email}",
        url=f"https://x/{email}",
        title=f"Call-{email}",
        published_at=datetime.now(UTC),
    )
    s.add(c)
    s.flush()
    s.add(
        CallRanking(
            call_id=c.id,
            profile_id=p.id,
            aggregate_score=80,
            rationale="r",
            model="gemma4:e4b-q8",
            prompt_version="v2.0",
            ranked_at=datetime.now(UTC),
        )
    )
    s.flush()
    return u.id


@pytest.fixture()
def now():
    return datetime(2026, 4, 20, 7, 0, tzinfo=UTC)  # a Monday


def test_run_digest_sends_and_logs(pg_session_factory, now):
    with pg_session_factory() as s:
        uid = _seed_user_with_payload(s, email="alice@ki.se")
        s.commit()
    email = _FakeEmail()
    summary = run_digest(
        session_factory=pg_session_factory,
        email_client=email,
        settings=_FakeSettings(),
        now=now,
    )
    assert summary.emails_sent == 1
    assert summary.bounces == 0
    assert len(email.calls) == 1
    assert email.calls[0]["to"] == "alice@ki.se"
    with pg_session_factory() as s:
        runs = s.execute(select(DigestRun).where(DigestRun.user_id == uid)).scalars().all()
    assert len(runs) == 1


def test_run_digest_skips_users_with_no_payload(pg_session_factory, now):
    with pg_session_factory() as s:
        u = User(email="empty@ki.se")
        s.add(u)
        s.commit()
    email = _FakeEmail()
    summary = run_digest(
        session_factory=pg_session_factory,
        email_client=email,
        settings=_FakeSettings(),
        now=now,
    )
    assert summary.emails_sent == 0
    assert email.calls == []


def test_run_digest_idempotent_within_week(pg_session_factory, now):
    with pg_session_factory() as s:
        uid = _seed_user_with_payload(s, email="dup@ki.se")
        s.commit()
    email = _FakeEmail()
    run_digest(
        session_factory=pg_session_factory,
        email_client=email,
        settings=_FakeSettings(),
        now=now,
    )
    # Second call same week → nothing new
    run_digest(
        session_factory=pg_session_factory,
        email_client=email,
        settings=_FakeSettings(),
        now=now,
    )
    assert len(email.calls) == 1
    with pg_session_factory() as s:
        runs = s.execute(select(DigestRun).where(DigestRun.user_id == uid)).scalars().all()
    assert len(runs) == 1


def test_run_digest_bounce_increments_counter(pg_session_factory, now):
    with pg_session_factory() as s:
        uid = _seed_user_with_payload(s, email="bouncey@ki.se")
        s.commit()
    email = _FakeEmail(results=[SendResult(ok=False, bounce_reason="invalid_email")])
    summary = run_digest(
        session_factory=pg_session_factory,
        email_client=email,
        settings=_FakeSettings(),
        now=now,
    )
    assert summary.bounces == 1
    with pg_session_factory() as s:
        u = s.get(User, uid)
        assert u.digest_bounce_count == 1
        assert u.digest_disabled_at is None
    # No digest_runs row written for a bounce
    with pg_session_factory() as s:
        runs = s.execute(select(DigestRun).where(DigestRun.user_id == uid)).scalars().all()
    assert runs == []


def test_run_digest_third_bounce_disables_user(pg_session_factory, now):
    with pg_session_factory() as s:
        uid = _seed_user_with_payload(s, email="thrice@ki.se")
        u = s.get(User, uid)
        u.digest_bounce_count = 2
        s.commit()
    email = _FakeEmail(results=[SendResult(ok=False, bounce_reason="invalid_email")])
    summary = run_digest(
        session_factory=pg_session_factory,
        email_client=email,
        settings=_FakeSettings(),
        now=now,
    )
    assert summary.bounces == 1
    assert summary.users_disabled == 1
    with pg_session_factory() as s:
        u = s.get(User, uid)
        assert u.digest_bounce_count == 3
        assert u.digest_disabled_at is not None


def test_run_digest_success_resets_bounce_counter(pg_session_factory, now):
    with pg_session_factory() as s:
        uid = _seed_user_with_payload(s, email="recover@ki.se")
        u = s.get(User, uid)
        u.digest_bounce_count = 2
        s.commit()
    email = _FakeEmail()  # default: ok
    run_digest(
        session_factory=pg_session_factory,
        email_client=email,
        settings=_FakeSettings(),
        now=now,
    )
    with pg_session_factory() as s:
        u = s.get(User, uid)
        assert u.digest_bounce_count == 0


def test_run_digest_transport_error_does_not_count_as_bounce(pg_session_factory, now):
    with pg_session_factory() as s:
        uid = _seed_user_with_payload(s, email="transient@ki.se")
        s.commit()
    email = _FakeEmail(error_at={0: EmailTransportError("boom")})
    summary = run_digest(
        session_factory=pg_session_factory,
        email_client=email,
        settings=_FakeSettings(),
        now=now,
    )
    assert summary.bounces == 0
    assert summary.transport_errors == 1
    with pg_session_factory() as s:
        u = s.get(User, uid)
        assert u.digest_bounce_count == 0
    # Caller will retry next week — no digest_runs written
    with pg_session_factory() as s:
        runs = s.execute(select(DigestRun).where(DigestRun.user_id == uid)).scalars().all()
    assert runs == []


def test_run_digest_dry_run_does_not_send_or_write(pg_session_factory, now):
    with pg_session_factory() as s:
        uid = _seed_user_with_payload(s, email="dry@ki.se")
        s.commit()
    email = _FakeEmail()
    summary = run_digest(
        session_factory=pg_session_factory,
        email_client=email,
        settings=_FakeSettings(digest_dry_run=True),
        now=now,
    )
    assert summary.emails_sent == 0
    assert email.calls == []  # never called
    with pg_session_factory() as s:
        runs = s.execute(select(DigestRun).where(DigestRun.user_id == uid)).scalars().all()
    assert runs == []


def test_run_digest_continues_when_one_user_assembly_fails(pg_session_factory, now, monkeypatch):
    """If assemble_for_user raises for one user, subsequent users still get
    digests. Spec docstring: 'Per-recipient try/except so one bounce doesn't
    stop the batch.'"""
    with pg_session_factory() as s:
        uid_bad = _seed_user_with_payload(s, email="bad@ki.se")
        _seed_user_with_payload(s, email="good@ki.se")
        s.commit()

    real_assemble = __import__(
        "call_manager.digest.send", fromlist=["assemble_for_user"]
    ).assemble_for_user

    def flaky_assemble(session, *, user_id, now):
        if user_id == uid_bad:
            raise RuntimeError("simulated profile-data corruption")
        return real_assemble(session, user_id=user_id, now=now)

    monkeypatch.setattr("call_manager.digest.send.assemble_for_user", flaky_assemble)

    email = _FakeEmail()
    summary = run_digest(
        session_factory=pg_session_factory,
        email_client=email,
        settings=_FakeSettings(),
        now=now,
    )

    # Good user got their digest; bad user was logged and skipped
    assert summary.emails_sent == 1
    assert summary.users_processed == 2
    sent_to = [c["to"] for c in email.calls]
    assert "good@ki.se" in sent_to
    assert "bad@ki.se" not in sent_to


def test_run_digest_skips_disabled_users(pg_session_factory, now):
    with pg_session_factory() as s:
        uid = _seed_user_with_payload(s, email="disabled@ki.se")
        u = s.get(User, uid)
        u.digest_disabled_at = datetime(2026, 1, 1, tzinfo=UTC)
        s.commit()
    email = _FakeEmail()
    summary = run_digest(
        session_factory=pg_session_factory,
        email_client=email,
        settings=_FakeSettings(),
        now=now,
    )
    assert summary.emails_sent == 0
    assert email.calls == []
