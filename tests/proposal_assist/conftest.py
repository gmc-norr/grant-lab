"""Shared fixtures for proposal_assist tests. See plan's
'Project Conventions Reference' section for context."""

import socket
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Call, Source, User
from call_manager.main import create_app

# Hostnames that reference-fetch tests mock via pytest-httpx. The SSRF
# resolver in fetcher.py validates via socket.getaddrinfo BEFORE httpx
# opens a connection, so we have to fake getaddrinfo too — pytest-httpx
# only mocks the httpx transport layer. Map known mocked hostnames to a
# real public IP (8.8.8.8); fall through to the real resolver for
# everything else.
#
# We use 8.8.8.8 rather than a TEST-NET address because Python's
# ipaddress.is_private treats the documentation ranges (192.0.2/24,
# 198.51.100/24, 203.0.113/24) as private, which would defeat the SSRF
# check.
_FAKE_PUBLIC_HOSTS: frozenset[str] = frozenset(
    {
        "api.crossref.org",
        "www.nejm.org",
        "eutils.ncbi.nlm.nih.gov",
        "arxiv.org",
        "example.com",
    }
)


@pytest.fixture
def fake_public_dns(monkeypatch):
    """Patch socket.getaddrinfo so known mocked hostnames resolve to a
    public IP. Non-autouse — tests that exercise the resolver itself
    (e.g., the SSRF rebinding-validation test) request it by name only
    where they need it, so they remain free to install their own
    getaddrinfo patches.

    Consolidated from 4 near-identical copies across
    tests/proposal_assist/{references,tools,}*.py."""
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, *args, **kwargs):
        if host in _FAKE_PUBLIC_HOSTS or host.endswith(".example.com"):
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("8.8.8.8", 0),
                )
            ]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return fake_getaddrinfo


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


# ---------------------------------------------------------------------------
# Shared auth client fixtures — used by workspace router smoke tests (B6).
# Pattern mirrors test_rubric_review_endpoint.py's _authed_client() helper
# but exposed as pytest fixtures so tests don't need boilerplate.
# ---------------------------------------------------------------------------


@pytest.fixture
def _app(_settings_env):
    """A create_app() instance for the current test (env already patched by
    _settings_env). The lifespan is entered/exited by the TestClient context
    managers below."""
    return create_app()


@pytest.fixture
def current_user(_app, pg_session_factory):
    """A User row inserted into the testcontainer DB. The app's session_factory
    is replaced with the test one so get_current_user() resolves against the
    same DB the test writes to."""
    with pg_session_factory() as s:
        u = User(email="owner-fixture@ki.se")
        s.add(u)
        s.commit()
        uid = u.id
    # Point the app's session_factory at the test DB (same engine).
    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        return s.get(User, uid)


@pytest.fixture
def client_authed_owner(_app, current_user, pg_session_factory):
    """TestClient with a valid session cookie for current_user (owner role)."""
    _app.state.session_factory = pg_session_factory
    client = TestClient(_app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = _app.state.session_signer
    client.cookies.set("session", signer.make(user_id=current_user.id))
    yield client
    client.__exit__(None, None, None)


@pytest.fixture
def client_unauth(_app, pg_session_factory):
    """TestClient with no session (anonymous visitor)."""
    _app.state.session_factory = pg_session_factory
    client = TestClient(_app, raise_server_exceptions=False)
    client.__enter__()
    yield client
    client.__exit__(None, None, None)


# Alias: the plan uses both names interchangeably.
client_authed_user = client_authed_owner


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
    from call_manager.db.models import User

    u = User(email=email)
    session.add(u)
    session.flush()
    return u


def make_profile(session, *, name: str = "Test PI Profile") -> "Profile":  # noqa: F821
    """Build a Profile owned by a fresh User. Real Profile requires
    `kind: ProfileKind` and `created_by` FK — no `slug`."""
    from call_manager.db.models import Profile, ProfileKind

    u = make_user(session, email=f"profile-owner-{name.lower().replace(' ', '-')}@ki.se")
    p = Profile(
        name=name,
        kind=ProfileKind.ORGANIZATIONAL_UNIT,
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
    from call_manager.db.models import Workspace

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
    from call_manager.db.models import ChatTurn

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
    from call_manager.db.models import ChatSummary

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
