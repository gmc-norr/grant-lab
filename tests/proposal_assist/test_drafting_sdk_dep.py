"""Smoke test: claude-agent-sdk is installed and importable.

This is the only test in B3 that touches the real SDK during Tasks 1-9.
If the SDK isn't pinned or moves to a different package name, this test
fails fast (rather than at Task 10 when we'd otherwise discover it)."""


def test_claude_agent_sdk_is_importable():
    import claude_agent_sdk  # noqa: F401
