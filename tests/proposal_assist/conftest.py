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


def make_profile(session, *, name: str = "Test PI Profile") -> "Profile":  # noqa: F821
    """Build a Profile owned by a fresh User. Real Profile requires
    `kind: ProfileKind` and `created_by` FK — no `slug`."""
    from call_monitor.db.models import Profile, ProfileKind

    u = make_user(session, email=f"profile-owner-{name.lower().replace(' ', '-')}@ki.se")
    p = Profile(
        name=name,
        kind=ProfileKind.GROUP,
        description="AI-assisted diagnosis of cancer biopsies, histopathology.",
        created_by=u.id,
    )
    session.add(p)
    session.flush()
    return p


def make_workspace(
    session,
    *,
    owner=None,
    title: str = "Test workspace",
    call_id: int | None = None,
    profile_id: int | None = None,
) -> "Workspace":  # noqa: F821
    """Build a Workspace row. If `owner` not provided, creates a fresh User."""
    from call_monitor.db.models import Workspace

    if owner is None:
        owner = make_user(session)
    ws = Workspace(
        title=title,
        owner_id=owner.id,
        call_id=call_id,
        profile_id=profile_id,
    )
    session.add(ws)
    session.flush()
    return ws


def make_chat_turn(
    session,
    workspace,
    *,
    role: str,
    content: dict,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    model: str | None = None,
):
    from call_monitor.db.models import ChatTurn

    turn = ChatTurn(
        workspace_id=workspace.id,
        role=role,
        content=content,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        model=model,
    )
    session.add(turn)
    session.flush()
    return turn


def make_chat_summary(
    session,
    workspace,
    summarized_through,
    *,
    summary_text: str = "previous turns summarized here.",
    tokens: int = 100,
    model: str = "claude-haiku-4-5",
):
    from call_monitor.db.models import ChatSummary

    cs = ChatSummary(
        workspace_id=workspace.id,
        summary=summary_text,
        summarized_through=summarized_through.id,
        tokens=tokens,
        model=model,
    )
    session.add(cs)
    session.flush()
    return cs
