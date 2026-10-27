"""Every Agent SDK adapter gets the ORG key and an app-owned CLI config dir,
and a resume id the CLI no longer holds recovers instead of wedging the chat.

Review 2026-10-26:

  H4 — four of the five Agent SDK adapters never received
  `settings.anthropic_api_key`; only the interactive one did. pydantic-settings
  keeps a `.env`-only key OUT of `os.environ`, so the CLI subprocess those four
  builders start had no key — and with `HOME=/home/<dev>` and a
  `~/.claude/.credentials.json` on the box it would authenticate as the
  DEVELOPER'S PERSONAL account instead. Wrong-account billing and a credential
  leak at once. Hence both halves of the fix: one shared constructor that
  supplies the key, and `CLAUDE_CONFIG_DIR` pointed at an app-owned directory
  so `~/.claude` is never the credential source.

  M-PA-4 — when the CLI refuses a `resume=` with "No conversation found with
  session ID", nothing cleared `workspaces.last_sdk_session_id`, so every later
  turn re-sent the same dead id and failed identically.

NOTHING here runs the real Claude CLI or reaches the Anthropic API: the key is a
fake literal assigned onto Settings (no env var is set), and the lost-session
tests raise the SDK's ProcessError from a fake adapter.
"""

import stat
import uuid

import pytest
from claude_agent_sdk import ProcessError
from sqlalchemy import select

from grant_lab.config import Settings
from grant_lab.db.models import ChatSummary, ChatTurn, TurnJob, Workspace
from grant_lab.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    LaunchContext,
    TurnDoneEvent,
)
from grant_lab.proposal_assist.drafting.controller import WorkspaceController
from grant_lab.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from grant_lab.proposal_assist.drafting.registry import SubprocessRegistry
from grant_lab.proposal_assist.turns.processor import _SUMMARY_PREAMBLE, process_turn_once
from grant_lab.proposal_assist.turns.submit import submit_user_turn
from tests.proposal_assist.conftest import make_chat_turn, make_user, make_workspace

# The directory name the app owns, spelled out here rather than imported: the
# test pins the name, it does not echo whatever the implementation chose.
AGENT_SDK_CONFIG_DIRNAME = ".agent-sdk-config"

# Never a real key, never exported to the environment.
_FAKE_KEY = "sk-ant-api03-FAKE-task12-not-a-real-key"

_DEAD_SESSION = "dead-session-0001"
_LOST_SESSION_STDERR = f"No conversation found with session ID: {_DEAD_SESSION}"
_SUMMARY_TEXT = "The user wants a grant on cancer immunotherapy."


def _settings(tmp_path) -> Settings:
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    settings.anthropic_api_key = _FAKE_KEY
    return settings


# ---------------------------------------------------------------------------
# The five builders. Every one of them must route through the shared
# constructor, so every one of them must carry the org key.
# ---------------------------------------------------------------------------


def _build_interactive(settings, session_factory):
    from grant_lab.proposal_assist.drafting.factory import _build_adapter

    return _build_adapter(settings, session_factory)


def _build_evaluator(settings, session_factory):
    from grant_lab.proposal_assist.evaluator.agent import build_evaluator_adapter

    return build_evaluator_adapter(
        settings=settings,
        session_factory=session_factory,
        workspace_id=uuid.uuid4(),
        requested_by=1,
        job_id=uuid.uuid4(),
        draft_section_count=3,
        spec_section_count=1,
        rubric_criteria=[],
        lifecycle_generation=0,
    )


def _build_keeper(settings, session_factory):
    from grant_lab.proposal_assist.coherence.agent import build_keeper_adapter

    return build_keeper_adapter(
        settings=settings,
        session_factory=session_factory,
        workspace_id=uuid.uuid4(),
        requested_by=1,
        job_id=uuid.uuid4(),
        locked=False,
        lifecycle_generation=0,
    )


def _build_review(settings, session_factory):
    from grant_lab.proposal_assist.review.agent import build_review_adapter

    return build_review_adapter(
        settings=settings,
        session_factory=session_factory,
        workspace_id=uuid.uuid4(),
        review_run_id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        kind="fit_check",
        lifecycle_generation=0,
    )


def _build_uncited_claims(settings, session_factory):
    from grant_lab.proposal_assist.uncited_claims.agent import (
        build_uncited_claims_adapter,
    )

    return build_uncited_claims_adapter(
        settings=settings,
        session_factory=session_factory,
        workspace_id=uuid.uuid4(),
        requested_by=1,
        job_id=uuid.uuid4(),
        draft_section_count=3,
        lifecycle_generation=0,
    )


_BUILDERS = [
    ("interactive", _build_interactive),
    ("evaluator", _build_evaluator),
    ("coherence_keeper", _build_keeper),
    ("review_panel", _build_review),
    ("uncited_claims", _build_uncited_claims),
]
_BUILDER_IDS = [name for name, _ in _BUILDERS]


def _options_env(settings, session_factory, build):
    adapter = build(settings, session_factory)
    kwargs = adapter._build_options_kwargs(LaunchContext(workspace_id=uuid.uuid4()))
    return kwargs["env"]


# ---------------------------------------------------------------------------
# H4 part 1 — the org key reaches all five subprocess environments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role, build", _BUILDERS, ids=_BUILDER_IDS)
def test_every_sdk_adapter_passes_the_org_api_key(
    role, build, tmp_path, pg_session_factory, _settings_env, monkeypatch
):
    """Without the key in ClaudeAgentOptions.env the CLI falls back to whatever
    credentials HOME happens to hold — on this box, a human's personal login."""
    monkeypatch.setenv("DRAFTING_ADAPTER", "claude")
    settings = _settings(tmp_path)

    env = _options_env(settings, pg_session_factory, build)

    assert env.get("ANTHROPIC_API_KEY") == settings.anthropic_api_key == _FAKE_KEY


def test_a_role_cannot_override_the_org_key(tmp_path, _settings_env):
    """The shared constructor supplies the key positionally-by-keyword, so a
    role passing its own `anthropic_api_key` is a TypeError, not a silent
    substitution. This is what makes "all five use the org key" structural."""
    from grant_lab.proposal_assist.drafting.factory import build_sdk_adapter

    settings = _settings(tmp_path)
    with pytest.raises(TypeError):
        build_sdk_adapter(settings, anthropic_api_key="sk-ant-SOME-OTHER-KEY")


# ---------------------------------------------------------------------------
# H4 part 2 — an app-owned CLAUDE_CONFIG_DIR, never ~/.claude
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role, build", _BUILDERS, ids=_BUILDER_IDS)
def test_every_sdk_adapter_uses_an_app_owned_cli_config_dir(
    role, build, tmp_path, pg_session_factory, _settings_env, monkeypatch
):
    monkeypatch.setenv("DRAFTING_ADAPTER", "claude")
    settings = _settings(tmp_path)

    env = _options_env(settings, pg_session_factory, build)

    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / AGENT_SDK_CONFIG_DIRNAME)


@pytest.mark.parametrize("role, build", _BUILDERS, ids=_BUILDER_IDS)
def test_no_sdk_adapter_reads_credentials_from_the_home_claude_dir(
    role, build, tmp_path, pg_session_factory, _settings_env, monkeypatch
):
    """The DIRECTION of H4, asserted explicitly: a config dir that is set but
    points at (or under) ~/.claude would still read the developer's personal
    credentials, and a path check alone would not notice."""
    import pathlib

    monkeypatch.setenv("DRAFTING_ADAPTER", "claude")
    settings = _settings(tmp_path)

    env = _options_env(settings, pg_session_factory, build)

    config_dir = pathlib.Path(env["CLAUDE_CONFIG_DIR"])
    home_claude = pathlib.Path.home() / ".claude"
    assert config_dir != home_claude
    assert not config_dir.is_relative_to(home_claude)
    assert home_claude not in config_dir.parents
    # Positively: it is inside the app's own workspaces tree.
    assert config_dir.is_relative_to(pathlib.Path(settings.workspaces_dir))


def test_the_cli_config_dir_is_created_private_to_the_app(tmp_path):
    """It holds the CLI's own state for the org account, so 0700: no group or
    other access. Also tightens an existing looser directory."""
    from grant_lab.proposal_assist.drafting.claude_adapter import (
        ensure_agent_sdk_config_dir,
    )

    loose = tmp_path / AGENT_SDK_CONFIG_DIRNAME
    loose.mkdir(mode=0o755)

    created = ensure_agent_sdk_config_dir(tmp_path)

    assert created == loose
    assert created.is_dir()
    assert stat.S_IMODE(created.stat().st_mode) == 0o700
    # Idempotent: a second launch must not raise.
    assert ensure_agent_sdk_config_dir(tmp_path) == created


# ---------------------------------------------------------------------------
# M-PA-4 — a resume id the CLI no longer holds
# ---------------------------------------------------------------------------


def test_the_lost_session_predicate_reads_the_cli_text():
    """The marker arrives either as the ProcessError's stderr or, once the SDK
    replaces it with a ResultError, on the __cause__ chain."""
    from grant_lab.proposal_assist.drafting.controller import is_lost_sdk_session

    lost = ProcessError("Command failed with exit code 1", 1, _LOST_SESSION_STDERR)
    assert is_lost_sdk_session(lost)

    wrapped = RuntimeError("Claude Code returned an error result")
    wrapped.__cause__ = lost
    assert is_lost_sdk_session(wrapped)

    assert not is_lost_sdk_session(ProcessError("Command failed with exit code 1", 1, "boom"))


class _LostSessionAdapter(FakeAgentAdapter):
    """Raises the CLI's refused-resume ProcessError for the first `fail_times`
    turns, then streams a normal turn. Never starts a subprocess."""

    def __init__(self, *, fail_times: int) -> None:
        super().__init__()
        self._fail_times = fail_times
        self.failures = 0

    async def send_turn(self, handle, user_message):
        self.last_user_message = user_message
        self.turns.append((handle, user_message))
        if self.failures < self._fail_times:
            self.failures += 1
            raise ProcessError("Command failed with exit code 1", 1, _LOST_SESSION_STDERR)
        yield AssistantTextEvent(text="Picking up where we left off.")
        yield TurnDoneEvent(tokens_in=7, tokens_out=11, model="fake", session_id="new-session")


def _workspace_with_a_dead_session(pg_session_factory):
    """A workspace whose stored SDK session id the CLI no longer has, plus a
    compaction summary — the context the recovery relies on re-injecting."""
    with pg_session_factory() as s:
        user = make_user(s)
        ws = make_workspace(s, owner=user)
        ws.last_sdk_session_id = _DEAD_SESSION
        s.flush()
        boundary = make_chat_turn(
            s, ws, role="assistant", content={"type": "text", "text": "earlier"}
        )
        s.flush()
        s.add(
            ChatSummary(
                workspace_id=ws.id,
                summary=_SUMMARY_TEXT,
                summarized_through=boundary.id,
                tokens=12,
                model="claude-haiku-4-5",
            )
        )
        s.commit()
        return user.id, ws.id


def _controller_for(pg_session_factory, adapter, tmp_path):
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    return WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
        workspaces_dir=tmp_path,
    )


def _submit(pg_session_factory, wsid, uid, text="Continue please"):
    with pg_session_factory() as s:
        job = submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text=text,
            submitted_by_user_id=uid,
            settings=Settings(),
        )
        s.commit()
        return job.id


@pytest.mark.asyncio
async def test_a_dead_resume_id_is_cleared_and_the_turn_relaunched_once(
    tmp_path, pg_session_factory, _settings_env
):
    uid, wsid = _workspace_with_a_dead_session(pg_session_factory)
    adapter = _LostSessionAdapter(fail_times=1)
    controller = _controller_for(pg_session_factory, adapter, tmp_path)
    job_id = _submit(pg_session_factory, wsid, uid)

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
        workspaces_dir=tmp_path,
    )

    # Relaunched EXACTLY once, and the second launch carries no resume.
    assert [lc.resume_session_id for lc in adapter.launches] == [_DEAD_SESSION, None]
    assert len(adapter.turns) == 2
    assert adapter.failures == 1
    # The retry is not context-free: the existing summary re-injection fires
    # because the session is now cold.
    first_message, second_message = adapter.turns[0][1], adapter.turns[1][1]
    assert first_message == "Continue please"
    assert _SUMMARY_PREAMBLE in second_message
    assert _SUMMARY_TEXT in second_message

    with pg_session_factory() as s:
        ws = s.scalar(select(Workspace).where(Workspace.id == wsid))
        # The dead id is gone, replaced by the live session's own id.
        assert ws.last_sdk_session_id == "new-session"
        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        assert job.status == "completed"
        # Graceful: the user sees the answer, not a failure notice. A system
        # turn_outcome row is what 'failed' and 'orphaned' both write.
        system_rows = s.scalars(
            select(ChatTurn).where(ChatTurn.workspace_id == wsid, ChatTurn.role == "system")
        ).all()
        assert system_rows == []
        assistant_texts = [
            t.content.get("text")
            for t in s.scalars(
                select(ChatTurn).where(ChatTurn.workspace_id == wsid, ChatTurn.role == "assistant")
            ).all()
        ]
        assert "Picking up where we left off." in assistant_texts


@pytest.mark.asyncio
async def test_the_lost_session_relaunch_is_bounded_to_one(
    tmp_path, pg_session_factory, _settings_env
):
    """If the fresh session fails the same way, the turn fails once — it must
    not relaunch forever. The count is the assertion."""
    uid, wsid = _workspace_with_a_dead_session(pg_session_factory)
    adapter = _LostSessionAdapter(fail_times=99)
    controller = _controller_for(pg_session_factory, adapter, tmp_path)
    job_id = _submit(pg_session_factory, wsid, uid)

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
        workspaces_dir=tmp_path,
    )

    assert [lc.resume_session_id for lc in adapter.launches] == [_DEAD_SESSION, None]
    assert len(adapter.turns) == 2
    assert adapter.failures == 2

    with pg_session_factory() as s:
        ws = s.scalar(select(Workspace).where(Workspace.id == wsid))
        assert ws.last_sdk_session_id is None
        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        assert job.status == "failed"
        assert job.error is not None and job.error.startswith("processor_failure:")
