"""Shared fixtures for proposal_assist tests. See plan's
'Project Conventions Reference' section for context."""

import json
import socket
import subprocess
import uuid as _uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Call, Source, User
from call_manager.main import create_app
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME

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
        "export.arxiv.org",
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
        u = User(status="active", email="owner-fixture@ki.se")
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
    """Build a User row. The default email is the fixed literal "owner@ki.se",
    NOT unique per call: a second call in the same test/session with no
    explicit `email` collides on `uq_users_email`. Callers that need more than
    one user must pass a distinct `email` themselves."""
    from call_manager.db.models import User

    u = User(status="active", email=email)
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
    ack_disclosure: bool = False,
    current_phase: str = "active",
    with_call: bool = False,
) -> "Workspace":  # noqa: F821
    """Build a Workspace row. If `owner` not provided, creates a fresh User.

    `ack_disclosure=True` records a `disclosure_acknowledged` event for the
    owner so a subsequent POST /turns clears the server-side disclosure gate
    (spec 2026-06-10 §9). Default False preserves the un-acknowledged state the
    disclosure-modal tests rely on.

    `current_phase` defaults to "active" (post-setup): most tests simulate a
    ready workspace where the composer is live and POST /turns is allowed.
    Pass current_phase="setup" to exercise the Phase-0 gate (status card
    instead of composer; turns rejected with 409). Tests that assert the
    setup default must NOT rely on this helper's default — they pin "setup"
    explicitly or build the row directly.

    `with_call=True` creates and links a Call (via `make_call`, full_text=
    "call body") when `call_id` isn't already given. Phase-0's M1 raises
    without a call_id, so any test that drives the real Phase0Worker needs
    one; callers that already pass `call_id` explicitly are unaffected.
    """
    from call_manager.db.models import Workspace, WorkspaceEvent

    if owner is None:
        owner = make_user(session)
    if with_call and call_id is None:
        call_id = make_call(session, full_text="call body").id
    ws = Workspace(
        title=title,
        owner_id=owner.id,
        call_id=call_id,
        profile_id=profile_id,
        current_phase=current_phase,
    )
    session.add(ws)
    session.flush()
    if ack_disclosure:
        session.add(
            WorkspaceEvent(
                workspace_id=ws.id,
                kind="disclosure_acknowledged",
                actor_id=owner.id,
                payload={},
            )
        )
        session.flush()
    return ws


def stamp_for(session, workspace_id):
    """The fence stamp a Group-D call site expects, minted the way production does.

    Endgame Task 5: the collab kernel + checkpoint + hydrate + flip functions take a
    `stamp: FenceStamp` threaded from their caller's ACCESS CHECK. A unit test that
    calls one of them directly stands in for that caller, so it mints the stamp the
    same way a route does — load the workspace, then `stamp_for_workspace`. Minting
    from the CURRENT row is exactly a live caller's stamp; a test that wants to prove
    the fence rejects a moved-on generation captures the stamp BEFORE bumping the
    generation (see the retire/straddle tests), which this helper supports by being
    called at the moment the caller's belief was formed.
    """
    from call_manager.db.models import Workspace
    from call_manager.proposal_assist.ports.stamp import stamp_for_workspace

    return stamp_for_workspace(session.get(Workspace, workspace_id))


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


def init_workspace_repo(repo: Path, content: str) -> None:
    """Create a workspace git repo with one committed artifact file. Several
    collab tests need a workspace whose git HEAD has content — hydration seeds
    from it, and checkpoint no-ops without it."""
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


@pytest.fixture
def make_workspace_repo(tmp_path):
    """Write proposal.md + citations.json under a temp workspaces_dir and return
    (settings_like, workspace_id). settings_like only needs `.workspaces_dir`."""

    class _S:
        def __init__(self, d):
            self.workspaces_dir = str(d)

    def _make(*, proposal="", ledger=None, ledger_raw=None):
        wid = _uuid.uuid4()
        repo = Path(tmp_path) / str(wid) / "repo"
        repo.mkdir(parents=True)
        (repo / "proposal.md").write_text(proposal)
        if ledger_raw is not None:
            (repo / "citations.json").write_text(ledger_raw)
        elif ledger is not None:
            (repo / "citations.json").write_text(json.dumps(ledger))
        return _S(tmp_path), wid

    return _make
