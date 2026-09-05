"""Adapter lifecycle hardening tests (Defects 2 + 3).

Defect 2: ANTHROPIC_API_KEY must reach the subprocess via options.env when the
adapter is constructed with a key (a .env-only key never lands in os.environ,
so inheritance is not enough).

Defect 3: a client.disconnect() that raises must fall back to force-killing the
OS subprocess instead of orphaning it.
"""

import uuid

import pytest

from grant_lab.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter
from grant_lab.proposal_assist.drafting.contract import AgentHandle, LaunchContext


def _adapter(**kw):
    return ClaudeAgentSDKAdapter(
        model="claude-test",
        workspaces_dir="/tmp/ws",
        **kw,
    )


# ---------------------------------------------------------------------------
# Defect 2 — ANTHROPIC_API_KEY in options.env
# ---------------------------------------------------------------------------


def test_options_env_includes_api_key_when_set():
    adapter = _adapter(anthropic_api_key="sk-ant-secret")
    ctx = LaunchContext(workspace_id=uuid.uuid4())
    kwargs = adapter._build_options_kwargs(ctx)
    assert kwargs["env"]["ANTHROPIC_API_KEY"] == "sk-ant-secret"


def test_options_env_omits_api_key_when_unset():
    adapter = _adapter()  # no key
    ctx = LaunchContext(workspace_id=uuid.uuid4())
    kwargs = adapter._build_options_kwargs(ctx)
    # Not forced to empty (which would break inherited-env auth); just absent.
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]


def test_api_key_does_not_clobber_scrubbed_secrets():
    """Adding the key must not drop the secret-scrubbing entries."""
    adapter = _adapter(anthropic_api_key="sk-ant-secret")
    ctx = LaunchContext(workspace_id=uuid.uuid4())
    kwargs = adapter._build_options_kwargs(ctx)
    assert kwargs["env"]["DATABASE_URL"] == ""
    assert kwargs["env"]["SESSION_SECRET"] == ""


# ---------------------------------------------------------------------------
# Defect 3 — disconnect failure force-kills the subprocess
# ---------------------------------------------------------------------------


class _FakeProcess:
    def __init__(self):
        self.killed = False
        self.terminated = False

    def kill(self):
        self.killed = True

    def terminate(self):
        self.terminated = True


class _FakeTransport:
    def __init__(self, process):
        self._process = process


class _BadClient:
    """A client whose disconnect() raises; exposes a transport+process so the
    adapter's fallback can force-kill it."""

    def __init__(self, process):
        self._transport = _FakeTransport(process)

    async def disconnect(self):
        raise RuntimeError("disconnect blew up")


@pytest.mark.asyncio
async def test_terminate_force_kills_subprocess_on_disconnect_failure():
    proc = _FakeProcess()
    client = _BadClient(proc)
    adapter = _adapter()
    handle = AgentHandle(workspace_id=uuid.uuid4(), state={"client": client})

    # Must not raise; must force-kill the OS process.
    await adapter.terminate(handle)

    assert proc.killed is True, "disconnect failure must escalate to a force kill"
    # Handle's client reference is cleared so it can't be reused.
    assert handle.state.get("client") is None


@pytest.mark.asyncio
async def test_terminate_clean_disconnect_does_not_kill():
    proc = _FakeProcess()

    class _GoodClient:
        def __init__(self):
            self._transport = _FakeTransport(proc)

        async def disconnect(self):
            pass

    adapter = _adapter()
    handle = AgentHandle(workspace_id=uuid.uuid4(), state={"client": _GoodClient()})
    await adapter.terminate(handle)
    assert proc.killed is False
    assert proc.terminated is False
