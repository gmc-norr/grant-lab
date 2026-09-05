"""Bounded teardown + generation lifecycle for ClaudeAgentSDKAdapter (Slice A)."""

import asyncio
import uuid

import pytest

from grant_lab.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter
from grant_lab.proposal_assist.drafting.contract import AgentHandle


class _HangingClient:
    """disconnect() ignores cancellation — the shape asyncio.wait_for cannot bound."""

    def __init__(self):
        self.killed = False
        self.reaped = False
        self._transport = self
        self._process = self

    async def disconnect(self):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(3600)

    def kill(self):
        self.killed = True

    async def wait(self):
        self.reaped = True
        return 0


class _CleanClient:
    def __init__(self):
        self.disconnected = False
        self.killed = False

    async def disconnect(self):
        self.disconnected = True

    def kill(self):
        self.killed = True


def _adapter(tmp_path, **kw):
    # Keyword-only; workspaces_dir is typed str.
    return ClaudeAgentSDKAdapter(workspaces_dir=str(tmp_path), model="m", **kw)


def _handle(adapter, client):
    ws = uuid.uuid4()
    launch_id = adapter._live.register(ws, client)
    return (
        ws,
        launch_id,
        AgentHandle(workspace_id=ws, state={"client": client, "launch_id": launch_id}),
    )


@pytest.mark.asyncio
async def test_clean_disconnect_confirms_death_and_clears_the_record(tmp_path):
    adapter = _adapter(tmp_path)
    client = _CleanClient()
    _ws, launch_id, handle = _handle(adapter, client)

    await adapter.terminate(handle)

    assert client.disconnected is True
    assert client.killed is False
    assert adapter._live.get(launch_id) is None
    assert adapter.retiring_count() == 0


@pytest.mark.asyncio
async def test_hanging_disconnect_is_force_killed_at_the_deadline_and_reaped(tmp_path):
    adapter = _adapter(tmp_path, disconnect_deadline_seconds=0.05, reap_deadline_seconds=0.5)
    client = _HangingClient()
    _ws, launch_id, handle = _handle(adapter, client)

    await asyncio.wait_for(adapter.terminate(handle), timeout=5)  # must NOT hang

    assert client.killed is True
    assert client.reaped is True
    assert adapter._live.get(launch_id) is None


@pytest.mark.asyncio
async def test_unreapable_client_stays_retiring_for_the_sweep(tmp_path):
    class _Unkillable(_HangingClient):
        def kill(self):
            raise RuntimeError("no such process")

    adapter = _adapter(tmp_path, disconnect_deadline_seconds=0.05, reap_deadline_seconds=0.2)
    client = _Unkillable()
    _ws, launch_id, handle = _handle(adapter, client)

    await asyncio.wait_for(adapter.terminate(handle), timeout=5)

    rec = adapter._live.get(launch_id)
    assert rec is not None and rec.state == "retiring"  # retained for retry
    assert adapter.retiring_count() == 1  # and counted against capacity


@pytest.mark.asyncio
async def test_connect_failure_still_retires_the_generation(tmp_path, monkeypatch):
    """Registration happens BEFORE connect(), so a connect-time failure is
    discoverable instead of leaving an unowned half-built subprocess."""
    from grant_lab.proposal_assist.drafting.contract import LaunchContext

    adapter = _adapter(tmp_path, disconnect_deadline_seconds=0.05, reap_deadline_seconds=0.5)

    class _BoomConnect(_CleanClient):
        async def connect(self):
            raise RuntimeError("connect failed")

    client = _BoomConnect()
    monkeypatch.setattr(
        "grant_lab.proposal_assist.drafting.claude_adapter.ClaudeSDKClient",
        lambda options: client,
    )

    with pytest.raises(RuntimeError):
        await adapter.launch(LaunchContext(workspace_id=uuid.uuid4()))

    assert client.disconnected is True  # cleanup ran through the inventory
    assert adapter._live.unconfirmed_count() == 0


@pytest.mark.asyncio
async def test_reconcile_retries_every_retiring_record_even_on_live_workspaces(tmp_path):
    """Capacity-eviction / idle-sweep orphans belong to LIVE workspaces — they must
    be retried too, not only deleted ones."""
    adapter = _adapter(tmp_path, disconnect_deadline_seconds=0.05, reap_deadline_seconds=0.5)
    client = _HangingClient()
    ws = uuid.uuid4()
    launch_id = adapter._live.register(ws, client)
    adapter._live.mark_retiring(launch_id)

    n = await adapter.reconcile_live_clients(is_deleted=lambda _ws: False)

    assert n >= 1
    await asyncio.sleep(0.3)
    assert client.killed is True
    assert adapter._live.get(launch_id) is None


@pytest.mark.asyncio
async def test_reconcile_retires_live_records_of_deleted_workspaces(tmp_path):
    adapter = _adapter(tmp_path, disconnect_deadline_seconds=0.05, reap_deadline_seconds=0.5)
    client = _CleanClient()
    ws = uuid.uuid4()
    launch_id = adapter._live.register(ws, client)

    await adapter.reconcile_live_clients(is_deleted=lambda w: w == ws)
    await asyncio.sleep(0.2)

    assert client.disconnected is True
    assert adapter._live.get(launch_id) is None


@pytest.mark.asyncio
async def test_reconcile_does_not_spawn_duplicate_teardowns(tmp_path):
    adapter = _adapter(tmp_path, disconnect_deadline_seconds=5, reap_deadline_seconds=5)
    client = _HangingClient()
    launch_id = adapter._live.register(uuid.uuid4(), client)
    adapter._live.mark_retiring(launch_id)

    await adapter.reconcile_live_clients(is_deleted=lambda _w: False)
    first = adapter._live.get(launch_id).teardown
    await adapter.reconcile_live_clients(is_deleted=lambda _w: False)
    second = adapter._live.get(launch_id).teardown

    assert first is second  # one tracked task per record, reused
    first.cancel()


# --- Slice C1 spike: the protocol PASS_REGISTRY depends on -------------------


def test_unconfirmed_launch_ids_reports_the_real_inventory(tmp_path):
    """'Still in the inventory' IS 'not confirmed dead' — confirm_dead pops the
    record (live_clients.py:64), there is no flag to read.

    PASS_REGISTRY calls this on the real adapter, so without a test here, deleting
    the method would break production silently.
    """
    adapter = _adapter(tmp_path)
    ws, launch_id, _ = _handle(adapter, _CleanClient())

    assert launch_id in adapter.unconfirmed_launch_ids()

    adapter._live.confirm_dead(launch_id)
    assert adapter.unconfirmed_launch_ids() == []


@pytest.mark.asyncio
async def test_retire_unconfirmed_tears_down_every_open_generation(tmp_path):
    """The reconciliation retry path, and the one retirement uses when no handle
    was ever returned (cancellation during launch)."""
    adapter = _adapter(tmp_path)
    clients = [_CleanClient(), _CleanClient()]
    for c in clients:
        adapter._live.register(uuid.uuid4(), c)

    n = await adapter.retire_unconfirmed()

    assert n == 2
    assert all(c.disconnected for c in clients)
    assert adapter.unconfirmed_launch_ids() == []
