"""POSITIVE fixture: fully converted. One port write, no session, no commit.

Lives under tests/fencing/fixtures/ and is never imported — find_fenced_tool_commits
is handed its source with a FAKE tools/ path, because the rule is scoped by path.
"""


async def submit_scorecard_impl(ctx, *, ports, invocation):
    ports.effects.submit_scorecard(invocation.stamp(), cmd)  # noqa: F821
    return {"stored": True}
