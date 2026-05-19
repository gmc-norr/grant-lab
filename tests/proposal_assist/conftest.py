"""Shared fixtures for proposal_assist tests. See plan's
'Project Conventions Reference' section for context."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from call_monitor.db.models import Call, Source


@pytest.fixture
def _settings_env(monkeypatch, pg_url):
    """Populate the env vars Settings() requires.

    Without these, pydantic validation raises during Settings(), so any test
    that constructs Settings directly OR via create_app() must depend on this.
    """
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


def make_call(
    session,
    *,
    external_id: str = "EXT-1",
    full_text: str = "Body about eligibility, scope, budget, "
    "expected outcomes, restrictions, mandatory.",
    source_slug: str = "vinnova",
) -> Call:
    """Build a Call row attached to one of the auto-seeded sources.

    `vinnova` and `eu_portal` are seeded by pg_session_factory's teardown;
    look them up by slug rather than constructing a new Source.
    """
    src = session.execute(select(Source).where(Source.slug == source_slug)).scalar_one()
    call = Call(
        source_id=src.id,
        external_id=external_id,
        title="Precision medicine programme",
        url="https://example.org/calls/" + external_id,
        summary="Funding for AI-based diagnostics.",
        full_text=full_text,
        deadline=datetime(2026, 12, 1, tzinfo=UTC),
    )
    session.add(call)
    session.flush()
    return call


def make_user(session, *, email: str = "owner@ki.se") -> "User":  # noqa: F821
    """Build a User row. Each call uses a unique email by default so multiple
    callers in the same test don't UNIQUE-collide. Callers that care about a
    specific email pass it explicitly."""
    from call_monitor.db.models import User
    u = User(email=email)
    session.add(u)
    session.flush()
    return u
