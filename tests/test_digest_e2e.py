from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from call_monitor.db.models import (
    Call,
    CallRanking,
    DigestRun,
    Profile,
    ProfileKind,
    Source,
    Subscription,
    User,
    UserCallDismissal,
)
from call_monitor.digest.send import run_digest
from call_monitor.email.client import SendResult


class _RecordingEmail:
    def __init__(self):
        self.calls = []

    def send(self, *, to, subject, html, text):
        self.calls.append({"to": to, "subject": subject, "html": html, "text": text})
        return SendResult(ok=True, message_id=f"msg-{len(self.calls)}")


@dataclass
class _FakeSettings:
    digest_dry_run: bool = False
    admin_dashboard_url: str = "http://app/"


@pytest.fixture()
def now():
    return datetime(2026, 4, 20, 7, 0, tzinfo=UTC)


def test_e2e_two_users_one_dismissed_one_must_include(pg_session_factory, now):
    """Drives the full digest path:
    - Two users
    - User A: subscribes to profile P1 (must_include=['pathology'])
    - User B: subscribes to profile P2 (no must_include); also has a dismissal
    - Two calls: one matches P1's must_include, one is high-scoring for P2
      and dismissed by user B
    Assert: A receives the must-include call; B receives nothing.
    """
    with pg_session_factory() as s:
        ua = User(email="alice@ki.se")
        ub = User(email="bob@ki.se")
        s.add_all([ua, ub])
        s.flush()
        p1 = Profile(
            name="P1", kind=ProfileKind.GROUP, description="", structured={
                "areas": [], "budget_min": None, "budget_max": None,
                "trl_min": None, "trl_max": None,
                "must_include": ["pathology"], "hard_exclusions": [],
            }, created_by=ua.id,
        )
        p2 = Profile(
            name="P2", kind=ProfileKind.GROUP, description="", structured={
                "areas": [], "budget_min": None, "budget_max": None,
                "trl_min": None, "trl_max": None,
                "must_include": [], "hard_exclusions": [],
            }, created_by=ub.id,
        )
        s.add_all([p1, p2])
        s.flush()
        s.add_all([
            Subscription(user_id=ua.id, profile_id=p1.id, digest_enabled=True),
            Subscription(user_id=ub.id, profile_id=p2.id, digest_enabled=True),
        ])
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c1 = Call(
            source_id=src.id, external_id="c1", url="https://x/1",
            title="Pathology AI breakthrough", summary="big news",
            published_at=now,
        )
        c2 = Call(
            source_id=src.id, external_id="c2", url="https://x/2",
            title="High-score for P2", published_at=now,
        )
        s.add_all([c1, c2])
        s.flush()
        # Score c2 high for P2; c1 low for both
        s.add(CallRanking(
            call_id=c2.id, profile_id=p2.id, aggregate_score=85, rationale="r",
            model="gemma4:e4b-q8", prompt_version="v2.0", ranked_at=now,
        ))
        # Bob dismisses c2
        s.add(UserCallDismissal(user_id=ub.id, call_id=c2.id))
        s.commit()
        ua_id = ua.id
        c1_id = c1.id
        ub_id = ub.id  # noqa: F841

    email = _RecordingEmail()
    summary = run_digest(
        session_factory=pg_session_factory, email_client=email,
        settings=_FakeSettings(), now=now,
    )

    # Alice gets her must-include call; Bob's only candidate is dismissed.
    assert summary.emails_sent == 1
    assert email.calls[0]["to"] == "alice@ki.se"
    assert "P1" in email.calls[0]["subject"]
    assert "Pathology AI breakthrough" in email.calls[0]["html"]
    assert "High-score for P2" not in email.calls[0]["html"]

    with pg_session_factory() as s:
        runs_a = s.execute(select(DigestRun).where(DigestRun.user_id == ua_id)).scalars().all()
        runs_b = s.execute(select(DigestRun).where(DigestRun.user_id == ub_id)).scalars().all()
    assert len(runs_a) == 1
    assert runs_a[0].included_call_ids == [c1_id]
    assert runs_b == []  # bob got nothing — no run logged
