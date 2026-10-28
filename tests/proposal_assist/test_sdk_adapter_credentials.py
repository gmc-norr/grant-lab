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
    AgentHandle,
    AssistantTextEvent,
    LaunchContext,
    TurnDoneEvent,
    TurnUsage,
    attach_turn_usage,
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

    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path.resolve() / AGENT_SDK_CONFIG_DIRNAME)


@pytest.mark.parametrize("role, build", _BUILDERS, ids=_BUILDER_IDS)
def test_no_sdk_adapter_reads_credentials_from_the_home_claude_dir(
    role, build, tmp_path, pg_session_factory, _settings_env, monkeypatch
):
    """The DIRECTION of H4, asserted explicitly: a config dir that is set but
    points at (or under) ~/.claude would still read the developer's personal
    credentials, and a path check alone would not notice.

    Every assertion is on the RESOLVED path (review 2026-10-26, F2): a lexical
    `!=` / `is_relative_to` / `in .parents` check passes for a path that is a
    symlink INTO ~/.claude, which is the guard this test exists to be.
    """
    import pathlib

    monkeypatch.setenv("DRAFTING_ADAPTER", "claude")
    settings = _settings(tmp_path)

    env = _options_env(settings, pg_session_factory, build)

    config_dir = pathlib.Path(env["CLAUDE_CONFIG_DIR"]).resolve()
    home_claude = (pathlib.Path.home() / ".claude").resolve()
    assert config_dir != home_claude
    assert not config_dir.is_relative_to(home_claude)
    assert home_claude not in config_dir.parents
    # Positively: it is inside the app's own workspaces tree.
    assert config_dir.is_relative_to(pathlib.Path(settings.workspaces_dir).resolve())


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


def test_the_cli_config_dir_is_absolute_even_from_a_relative_workspaces_dir(
    tmp_path, monkeypatch
):
    """Review 2026-10-26, F1 — LIVE, not latent.

    WORKSPACES_DIR defaults to the RELATIVE `var/workspaces` and `.env.example`
    ships that default; the live `.env` does not set it. The CLI subprocess runs
    with `cwd=<workspaces_dir>/<uuid>/repo` and resolves a relative
    CLAUDE_CONFIG_DIR against ITS OWN cwd, so an unresolved value meant the app
    created and chmod'd `<app cwd>/var/workspaces/.agent-sdk-config` while the
    CLI used `<workspace repo>/var/workspaces/.agent-sdk-config`: not 0700, one
    tree per workspace, and inside a git repo `phases/setup.py` commits with
    `git add -A`.

    The other config-dir tests cannot catch this because `tmp_path` is already
    absolute — the "passes only because of the fixture" shape. `monkeypatch.chdir`
    first so `resolve()` cannot reach outside tmp_path even in principle; this
    test creates nothing either way (`_build_options_kwargs` is pure).
    """
    import pathlib

    from grant_lab.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter

    monkeypatch.chdir(tmp_path)
    adapter = ClaudeAgentSDKAdapter(model="claude-haiku-4-5", workspaces_dir="var/workspaces")

    env = adapter._build_options_kwargs(LaunchContext(workspace_id=uuid.uuid4()))["env"]

    config_dir = pathlib.Path(env["CLAUDE_CONFIG_DIR"])
    assert config_dir.is_absolute()
    assert config_dir == tmp_path.resolve() / "var" / "workspaces" / AGENT_SDK_CONFIG_DIRNAME


def test_the_cli_config_dir_refuses_a_symlink(tmp_path):
    """Review 2026-10-26, F2.

    `mkdir(parents=True, exist_ok=True)` ACCEPTS a symlink-to-directory (such a
    link *is* `is_dir()`) and `Path.chmod` FOLLOWS it, so a link planted at this
    name made the app chmod 0700 an arbitrary victim directory and then hand the
    CLI that victim as its credential store — `~/.claude` included. This is the
    same mechanism that chmod'd a real `~/.claude` during the task's own
    mutation 2, so it is a property of the shipped helper, not of the mutation.

    Both the link and its victim are inside `tmp_path`. Never point this at a
    real path: the function under test mutates the filesystem.
    """
    from grant_lab.proposal_assist.drafting.claude_adapter import (
        AgentSdkConfigDirError,
        ensure_agent_sdk_config_dir,
    )

    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    victim = tmp_path / "victim-stands-in-for-home-claude"
    victim.mkdir()
    victim.chmod(0o777)
    (workspaces / AGENT_SDK_CONFIG_DIRNAME).symlink_to(victim, target_is_directory=True)

    with pytest.raises(AgentSdkConfigDirError):
        ensure_agent_sdk_config_dir(workspaces)

    # The victim is untouched — no chmod followed the link.
    assert stat.S_IMODE(victim.stat().st_mode) == 0o777


def _plant_symlink_shape(tmp_path, shape: str):
    """A workspaces dir whose `.agent-sdk-config` is a symlink of `shape`.
    Every path stays inside tmp_path — this helper's caller mutates the
    filesystem, and pointing it at a real path is how a real `~/.claude` got
    chmod'd once already."""
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    link = workspaces / AGENT_SDK_CONFIG_DIRNAME
    if shape == "to_dir_inside":
        target = workspaces / "sibling"
        target.mkdir()
        link.symlink_to(target, target_is_directory=True)
    elif shape == "to_dir_outside":
        target = tmp_path / "outside-the-tree"
        target.mkdir()
        link.symlink_to(target, target_is_directory=True)
    elif shape == "to_file":
        target = tmp_path / "a-file"
        target.write_text("not a directory")
        link.symlink_to(target)
    elif shape == "dangling":
        link.symlink_to(tmp_path / "does-not-exist", target_is_directory=True)
    else:  # pragma: no cover - guard against a typo in the parametrize list
        raise AssertionError(f"unknown shape {shape}")
    return workspaces


@pytest.mark.parametrize(
    "shape", ["to_dir_inside", "to_dir_outside", "to_file", "dangling"]
)
def test_the_cli_config_dir_refuses_every_symlink_shape(tmp_path, shape):
    """Re-review of F2 (M4): the refusal has to run BEFORE mkdir.

    The first fix checked `is_symlink()` AFTER
    `mkdir(parents=True, exist_ok=True)`. That is fine for a
    symlink-to-directory (mkdir accepts it, exist_ok), but the other three
    shapes make mkdir itself raise a BARE `FileExistsError` first — so the
    named error never fired, and `turns/processor.py` saw a class it could not
    classify and left the turn `queued` for a 5-second retry loop forever. All
    four shapes must now raise `AgentSdkConfigDirError`, and specifically NOT a
    bare FileExistsError.
    """
    from grant_lab.proposal_assist.drafting.claude_adapter import (
        AgentSdkConfigDirError,
        ensure_agent_sdk_config_dir,
    )

    workspaces = _plant_symlink_shape(tmp_path, shape)

    with pytest.raises(AgentSdkConfigDirError) as excinfo:
        ensure_agent_sdk_config_dir(workspaces)

    # The distinction is the whole point of the finding: a FileExistsError is
    # not in the processor's non-retryable allowlist, an AgentSdkConfigDirError
    # is. AgentSdkConfigDirError is a RuntimeError, so this cannot pass by
    # accident through a shared base class.
    assert not isinstance(excinfo.value, OSError)
    assert AGENT_SDK_CONFIG_DIRNAME in str(excinfo.value)


def test_the_cli_config_dir_refuses_a_plain_file_at_its_name(tmp_path):
    """The non-symlink half of the same trigger: a plain file at that name (or
    at one of its parents) makes mkdir raise FileExistsError / NotADirectoryError.
    Deterministic, so it must also be the named class the processor terminalizes
    on rather than a bare OSError it retries."""
    from grant_lab.proposal_assist.drafting.claude_adapter import (
        AgentSdkConfigDirError,
        ensure_agent_sdk_config_dir,
    )

    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    (workspaces / AGENT_SDK_CONFIG_DIRNAME).write_text("squatting on the name")

    with pytest.raises(AgentSdkConfigDirError):
        ensure_agent_sdk_config_dir(workspaces)


def test_building_options_kwargs_touches_no_filesystem(tmp_path):
    """Review 2026-10-26, F3.

    `_build_options_kwargs`'s own docstring says it is split out from launch()
    "so the security-boundary wiring can be unit tested without a live SDK
    subprocess" — and a mkdir+chmod in it made nine such unit tests create
    directories, including `/tmp/.agent-sdk-config` and `/tmp/x/.agent-sdk-config`
    from the two existing suites that pass `workspaces_dir="/tmp"` and
    `"/tmp/x"`. On a shared box `/tmp/.agent-sdk-config` is also a squat target:
    another user owning it first makes the app's chmod raise PermissionError on
    every launch.
    """
    from grant_lab.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter

    workspaces = tmp_path / "never-created"
    adapter = ClaudeAgentSDKAdapter(model="claude-haiku-4-5", workspaces_dir=str(workspaces))

    env = adapter._build_options_kwargs(LaunchContext(workspace_id=uuid.uuid4()))["env"]

    assert env["CLAUDE_CONFIG_DIR"] == str(workspaces / AGENT_SDK_CONFIG_DIRNAME)
    assert not workspaces.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_launch_creates_the_app_owned_cli_config_dir(tmp_path, monkeypatch):
    """The other half of F3: the mkdir+chmod moved to launch(), where a
    subprocess really is started. No subprocess here — the SDK client is faked
    and its connect() raises, which is enough to prove the directory already
    exists by the time the client is built."""
    from grant_lab.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter

    class _BoomConnect:
        async def connect(self):
            raise RuntimeError("no SDK subprocess in tests")

        async def disconnect(self):
            return None

        def kill(self):
            return None

    workspaces = tmp_path / "workspaces"
    monkeypatch.setattr(
        "grant_lab.proposal_assist.drafting.claude_adapter.ClaudeSDKClient",
        lambda options: _BoomConnect(),
    )
    adapter = ClaudeAgentSDKAdapter(
        model="claude-haiku-4-5",
        workspaces_dir=str(workspaces),
        disconnect_deadline_seconds=0.05,
        reap_deadline_seconds=0.5,
    )

    with pytest.raises(RuntimeError):
        await adapter.launch(LaunchContext(workspace_id=uuid.uuid4()))

    created = workspaces / AGENT_SDK_CONFIG_DIRNAME
    assert created.is_dir()
    assert stat.S_IMODE(created.stat().st_mode) == 0o700


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


class _LostMidTurnAdapter(FakeAgentAdapter):
    """Raises the refused-resume error only AFTER the turn has already written a
    reply (`reply_first`) or billed tokens (`billed`). A retry would then send the
    same user message twice, write a second answer and bill the turn twice."""

    def __init__(self, *, reply_first: bool = False, billed: bool = False) -> None:
        super().__init__()
        self._reply_first = reply_first
        self._billed = billed

    async def send_turn(self, handle, user_message):
        self.last_user_message = user_message
        self.turns.append((handle, user_message))
        if self._reply_first:
            yield AssistantTextEvent(text="Half an answer.")
        error = ProcessError("Command failed with exit code 1", 1, _LOST_SESSION_STDERR)
        if self._billed:
            attach_turn_usage(error, TurnUsage(tokens_in=50, tokens_out=20, model="fake"))
        raise error


@pytest.mark.asyncio
async def test_a_lost_session_after_a_reply_is_not_relaunched(
    tmp_path, pg_session_factory, _settings_env
):
    """The retry is safe only while nothing of the turn reached the transcript.
    Once an assistant row exists, re-sending would duplicate it."""
    uid, wsid = _workspace_with_a_dead_session(pg_session_factory)
    adapter = _LostMidTurnAdapter(reply_first=True)
    controller = _controller_for(pg_session_factory, adapter, tmp_path)
    job_id = _submit(pg_session_factory, wsid, uid)

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
        workspaces_dir=tmp_path,
    )

    assert len(adapter.turns) == 1
    assert [lc.resume_session_id for lc in adapter.launches] == [_DEAD_SESSION]
    with pg_session_factory() as s:
        texts = [
            t.content.get("text")
            for t in s.scalars(
                select(ChatTurn).where(ChatTurn.workspace_id == wsid, ChatTurn.role == "assistant")
            ).all()
        ]
        assert texts.count("Half an answer.") == 1
        assert s.scalar(select(TurnJob.status).where(TurnJob.id == job_id)) == "failed"


@pytest.mark.asyncio
async def test_a_lost_session_that_already_billed_is_not_relaunched(
    tmp_path, pg_session_factory, _settings_env
):
    """No reply yet, but the request went out and tokens were billed. A retry
    would bill the same turn a second time."""
    uid, wsid = _workspace_with_a_dead_session(pg_session_factory)
    adapter = _LostMidTurnAdapter(billed=True)
    controller = _controller_for(pg_session_factory, adapter, tmp_path)
    job_id = _submit(pg_session_factory, wsid, uid)

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
        workspaces_dir=tmp_path,
    )

    assert len(adapter.turns) == 1
    assert [lc.resume_session_id for lc in adapter.launches] == [_DEAD_SESSION]
    with pg_session_factory() as s:
        assert s.scalar(select(TurnJob.status).where(TurnJob.id == job_id)) == "failed"


class _LostLaunchAdapter(FakeAgentAdapter):
    """Refuses any launch that carries a resume id, the way the SDK surfaces a
    refused resume from inside client.connect(); launches normally without one.
    Never starts a subprocess."""

    def __init__(self) -> None:
        super().__init__()
        self.refusals = 0

    async def launch(self, context):
        if context.resume_session_id is not None:
            self.refusals += 1
            raise ProcessError("Command failed with exit code 1", 1, _LOST_SESSION_STDERR)
        return await super().launch(context)


@pytest.mark.asyncio
async def test_a_refused_pre_claim_launch_clears_the_dead_id(
    tmp_path, pg_session_factory, _settings_env
):
    """Review 2026-10-26, F4.

    `process_turn_once` launches BEFORE claiming, and its `except Exception`
    leaves the row `queued`; `drain.py` re-selects the oldest queued job per
    workspace every tick, so a deterministic launch failure is an unbounded,
    untimed retry. A refused resume is exactly that — the SDK reports it from
    inside `client.connect()`, i.e. from this pre-claim launch, which passes the
    stored id — and nothing cleared the id, so every tick failed identically.

    The two calls below are two drain ticks. Tick 1 must clear the id (that is
    the bound: `recover_lost_session` can only clear once); tick 2 must then
    launch cold and the turn must actually run.
    """
    uid, wsid = _workspace_with_a_dead_session(pg_session_factory)
    adapter = _LostLaunchAdapter()
    controller = _controller_for(pg_session_factory, adapter, tmp_path)
    job_id = _submit(pg_session_factory, wsid, uid)

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
        workspaces_dir=tmp_path,
    )

    assert adapter.refusals == 1
    with pg_session_factory() as s:
        ws = s.scalar(select(Workspace).where(Workspace.id == wsid))
        assert ws.last_sdk_session_id is None, "the dead resume id survived the refusal"
        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        assert job.status == "queued"

    # The next tick: cold launch, no resume id, and the turn runs.
    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
        workspaces_dir=tmp_path,
    )

    assert adapter.refusals == 1
    assert [lc.resume_session_id for lc in adapter.launches] == [None]
    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        assert job.status == "completed"


@pytest.mark.asyncio
async def test_a_raising_terminate_during_recovery_still_finalizes_the_turn(
    tmp_path, pg_session_factory, _settings_env, monkeypatch
):
    """Review 2026-10-26, F5.

    `recover_lost_session` awaits `controller.terminate`, which this codebase
    treats as raisable (`drain.py` wraps it because "a terminate failure can't
    abort the drain tick"), and the retry path also opens a DB session for the
    compaction summary. Both sat OUTSIDE any try, in the retry gate itself, so a
    raise escaped `process_turn_once` and left the turn_job `running` with no
    transcript outcome until the reaper — the hazard a comment in that very
    function exists to prevent.
    """
    uid, wsid = _workspace_with_a_dead_session(pg_session_factory)
    adapter = _LostSessionAdapter(fail_times=1)
    controller = _controller_for(pg_session_factory, adapter, tmp_path)

    async def _raising_terminate(workspace_id):
        raise RuntimeError("terminate failed while recovering")

    monkeypatch.setattr(controller, "terminate", _raising_terminate)
    job_id = _submit(pg_session_factory, wsid, uid)

    # Must not raise: the turn falls through to the normal failure finalization.
    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
        workspaces_dir=tmp_path,
    )

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        assert job.status == "failed", "the row was left running for the reaper"
        # Finalized on the ORIGINAL error, not the terminate failure.
        assert job.error is not None and job.error.startswith("processor_failure:")
        assert "ProcessError" in job.error


# ---------------------------------------------------------------------------
# F6 — fail-closed, but legible
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_without_the_org_key_names_the_missing_setting(tmp_path):
    """Review 2026-10-26, F6. H4 removed the ~/.claude fallback on purpose, so a
    missing key now means no drafting turn can ever authenticate — and the user
    was told "Please try again", the wrong instruction for a misconfiguration.
    The refusal is named, and it happens before any request leaves the process.
    """
    from grant_lab.proposal_assist.drafting.claude_adapter import (
        ClaudeAgentSDKAdapter,
        MissingAnthropicKeyError,
    )

    adapter = ClaudeAgentSDKAdapter(
        model="claude-haiku-4-5", workspaces_dir=str(tmp_path), anthropic_api_key=None
    )
    handle = AgentHandle(workspace_id=uuid.uuid4(), state={"client": object()})

    with pytest.raises(MissingAnthropicKeyError) as excinfo:
        async for _ in adapter.send_turn(handle, "draft the aims"):
            pass

    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


def test_drafting_startup_warns_when_the_org_key_is_unset(
    tmp_path, pg_session_factory, _settings_env, monkeypatch, caplog
):
    """F6's other half: say it at startup, in the shape scheduler.py's hosted
    ticks use. The warning fires for DRAFTING_ADAPTER=claude, which is the only
    choice that needs the key — 'fake' needs none and is not warned about."""
    import logging

    from grant_lab.proposal_assist.drafting.factory import build_workspace_controller

    monkeypatch.setenv("DRAFTING_ADAPTER", "claude")
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    assert not settings.anthropic_api_key

    with caplog.at_level(logging.WARNING):
        build_workspace_controller(
            settings=settings,
            session_factory=pg_session_factory,
            lock_session_factory=pg_session_factory,
        )

    assert "ANTHROPIC_API_KEY not set" in caplog.text

    # And nothing is warned about when the key IS configured.
    caplog.clear()
    settings.anthropic_api_key = _FAKE_KEY
    with caplog.at_level(logging.WARNING):
        build_workspace_controller(
            settings=settings,
            session_factory=pg_session_factory,
            lock_session_factory=pg_session_factory,
        )
    assert "ANTHROPIC_API_KEY" not in caplog.text


def test_the_agent_sdk_construction_roster_is_five_shared_constructor_calls():
    """H4 happened four separate times because four builders constructed the
    adapter directly. `build_sdk_adapter`'s docstring claims "a NEW role cannot
    be added without the key" — nothing enforced that claim until this walk.
    Both halves matter: no direct construction anywhere in src/, and the roster
    of roles reaching the shared constructor is exactly five."""
    import ast
    import pathlib

    src_root = pathlib.Path(__file__).resolve().parents[2] / "src"
    direct: list[str] = []
    shared: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # BOTH call forms. A Name-only walk (what this gate shipped with)
            # is blind to the attribute form: `ca.ClaudeAgentSDKAdapter(...)`
            # after `from ... import claude_adapter as ca` evaded it, proven
            # green on a synthetic sixth construction (re-review, M5). `.attr`
            # is the trailing name in `a.b.Call()`, which is the only part a
            # roster of constructor names can key on.
            if isinstance(node.func, ast.Name):
                name: str | None = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            else:
                name = None
            if name not in ("ClaudeAgentSDKAdapter", "build_sdk_adapter"):
                continue
            where = f"{path.relative_to(src_root)}:{node.lineno}"
            (direct if name == "ClaudeAgentSDKAdapter" else shared).append(where)

    assert direct == [], f"Agent SDK adapter constructed outside the shared one: {direct}"
    assert len(shared) == 5, f"expected 5 roles on the shared constructor, got {shared}"


# ---------------------------------------------------------------------------
# Re-review I2 — a non-retryable launch failure terminalizes the turn
# ---------------------------------------------------------------------------


class _RefusingLaunchAdapter(FakeAgentAdapter):
    """Raises a configured exception from every launch. Never starts a
    subprocess; the exception instance is what the test is about."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self._error = error
        self.attempts = 0

    async def launch(self, context):
        self.attempts += 1
        raise self._error


@pytest.mark.asyncio
async def test_a_non_retryable_launch_failure_fails_the_turn_instead_of_spinning(
    tmp_path, pg_session_factory, _settings_env
):
    """Re-review of F4 (I2).

    `process_turn_once` launches BEFORE the atomic claim, so its `except` cannot
    finalize a `running` row — it returned, the row stayed `queued`, and
    drain.py re-selected the oldest queued row per workspace every
    TURN_DRAIN_TICK_SECONDS (default 5 s) forever, with a fresh stack trace each
    tick and the job never terminalizing. Only the refused-resume trigger was
    handled, and the first fix pass ADDED two more to that path
    (`AgentSdkConfigDirError`, and the bare `FileExistsError` the symlink shapes
    made mkdir raise) — so a misconfigured deploy span at 5 s forever.

    The two calls are two drain ticks: tick 1 must terminalize, and tick 2 must
    find nothing to do (the row is no longer `queued`, so it is not even
    launched again).
    """
    from grant_lab.proposal_assist.drafting.contract import AgentSdkConfigDirError

    with pg_session_factory() as s:
        user = make_user(s)
        ws = make_workspace(s, owner=user)
        s.commit()
        uid, wsid = user.id, ws.id
    adapter = _RefusingLaunchAdapter(
        AgentSdkConfigDirError(
            f"{tmp_path}/{AGENT_SDK_CONFIG_DIRNAME} is a symlink; refusing"
        )
    )
    controller = _controller_for(pg_session_factory, adapter, tmp_path)
    job_id = _submit(pg_session_factory, wsid, uid)

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
        workspaces_dir=tmp_path,
    )

    assert adapter.attempts == 1
    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        assert job.status == "failed", "a misconfigured deploy must fail ONE turn"
        assert job.completed_at is not None
        assert job.error.startswith("launch_failed: AgentSdkConfigDirError:")
        # The member-visible row: no setting value, no path, and NOT "try again".
        row = s.scalars(
            select(ChatTurn)
            .where(ChatTurn.workspace_id == wsid, ChatTurn.role == "system")
            .order_by(ChatTurn.created_at.desc())
        ).first()
        assert row is not None
        text = row.content["text"]
        assert "not set up on this server" in text
        assert "Trying again will not help" in text
        assert AGENT_SDK_CONFIG_DIRNAME not in text
        assert "AgentSdkConfigDirError" not in text

    # The next tick: the row is terminal, so the peek returns before launching.
    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
        workspaces_dir=tmp_path,
    )

    assert adapter.attempts == 1, "a terminalized turn must not be re-dispatched"


@pytest.mark.asyncio
async def test_a_retryable_launch_failure_still_leaves_the_turn_queued(
    tmp_path, pg_session_factory, _settings_env
):
    """The other half of I2, and the one that keeps the fix from being too
    broad: terminalizing every launch failure would kill turns a later tick
    would have run. A database blip, cross-process lock contention and a
    subprocess that failed to start are all genuinely retryable, so anything
    outside `_NON_RETRYABLE_LAUNCH_ERRORS` must still be left `queued`."""
    with pg_session_factory() as s:
        user = make_user(s)
        ws = make_workspace(s, owner=user)
        s.commit()
        uid, wsid = user.id, ws.id
    adapter = _RefusingLaunchAdapter(OSError(28, "No space left on device"))
    controller = _controller_for(pg_session_factory, adapter, tmp_path)
    job_id = _submit(pg_session_factory, wsid, uid)

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
        workspaces_dir=tmp_path,
    )

    assert adapter.attempts == 1
    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        assert job.status == "queued"
        assert job.error is None
        rows = s.scalars(
            select(ChatTurn).where(
                ChatTurn.workspace_id == wsid, ChatTurn.role == "system"
            )
        ).all()
        assert rows == [], "a retryable failure must not write an outcome row"


# ---------------------------------------------------------------------------
# Re-review I3a — the member-visible copy for a condition that cannot succeed
# ---------------------------------------------------------------------------


def test_a_misconfigured_deployment_is_not_told_to_try_again():
    """Review 2026-10-26 F6's residual.

    `outcome_message` already dispatched on a raw_error prefix (`reaped:`,
    `orphaned`), and the two misconfiguration paths produce deterministic
    prefixes — so the member was being told "Please try again" for a condition
    that will NEVER succeed until an administrator changes a setting. One
    branch closes it, and it must leak no diagnostics: not the setting's value,
    not the path, not the exception text.
    """
    from grant_lab.proposal_assist.turns.outcomes import outcome_message

    for raw in (
        "processor_failure: MissingAnthropicKeyError: ANTHROPIC_API_KEY is not "
        "set, so the drafting agent cannot authenticate",
        "launch_failed: AgentSdkConfigDirError: /srv/var/workspaces/"
        ".agent-sdk-config is a symlink; refusing",
    ):
        message = outcome_message("failed", raw)
        assert "try again" not in message.lower()
        assert "Trying again will not help" in message
        # No diagnostics: nothing from raw_error is echoed.
        for leaked in ("ANTHROPIC_API_KEY", "srv", AGENT_SDK_CONFIG_DIRNAME, "symlink"):
            assert leaked not in message

    # Everything else keeps its existing copy — this is one branch, not a
    # rewrite of the vocabulary.
    assert outcome_message("failed", "sdk_turn_error: rate_limit").endswith(
        "Please try again."
    )
    assert outcome_message("failed", None).endswith("Please try again.")
    assert outcome_message("failed", "orphaned") == (
        "This turn was interrupted. Please try again."
    )
    assert outcome_message("cancelled", None) == (
        "This turn was cancelled before it started."
    )


# ---------------------------------------------------------------------------
# Re-review I1 — the one ANTHROPIC_API_KEY-gated test must carry the key
# ---------------------------------------------------------------------------


def test_every_key_gated_test_constructs_the_adapter_with_the_env_key():
    """The `1 skipped` signal structurally CANNOT see this one.

    `send_turn` refuses a falsy key (F6) and the constructor does not read
    `os.environ`, while the skipif gates on `os.environ` — so a key-gated test
    built without the kwarg dies with MissingAnthropicKeyError the moment it is
    un-skipped. In this environment it is ALWAYS skipped, so the gate is green
    either way and the run's "0 skipped means a live call ran" convention is
    meaningless for it. The first fix pass added a fake key to five sibling
    send_turn tests and edited this one, and still missed it (re-review, I1).

    A static walk is the only thing that can see it. Both halves matter: the
    population is non-empty (an empty walk would pass vacuously), and every
    member passes an `anthropic_api_key`.
    """
    import ast
    import pathlib

    tests_root = pathlib.Path(__file__).resolve().parent
    gated: list[str] = []
    missing: list[str] = []
    for path in sorted(tests_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "ANTHROPIC_API_KEY" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = "\n".join(ast.unparse(d) for d in node.decorator_list)
            if "skipif" not in decorators or "ANTHROPIC_API_KEY" not in decorators:
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr
                    if isinstance(func, ast.Attribute)
                    else None
                )
                if name != "ClaudeAgentSDKAdapter":
                    continue
                where = f"{path.relative_to(tests_root)}::{node.name}"
                gated.append(where)
                kwargs = {kw.arg for kw in call.keywords}
                if "anthropic_api_key" not in kwargs:
                    missing.append(where)

    assert gated, "no ANTHROPIC_API_KEY-gated adapter construction found — the walk is vacuous"
    assert missing == [], (
        "a key-gated test builds the adapter without the key it gated on, so it "
        f"dies on MissingAnthropicKeyError when un-skipped: {missing}"
    )
