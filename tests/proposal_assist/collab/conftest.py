import pytest

from grant_lab.proposal_assist.collab.room import MANAGER


@pytest.fixture(autouse=True)
def _reset_collab_rooms():
    """The module-global room registry survives across tests. Clear it around
    every test so a room/keylock left behind by one test can't leak into the
    next (evict-on-empty + random workspace UUIDs make this benign today, but a
    future poison/route test could leave state)."""
    MANAGER._rooms.clear()
    MANAGER._keylocks.clear()
    yield
    MANAGER._rooms.clear()
    MANAGER._keylocks.clear()
