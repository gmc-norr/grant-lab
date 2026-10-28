"""Every SDK role is bounded per request, and no failure path loses its spend.

Review 2026-10-26:

  M-PA-2 — no SDK call carried `max_turns`, `max_budget_usd` or `effort`. The
  only bound on a drafting turn was the 600 s wall clock, which is why a loop
  could run flat out for ten minutes. The bound is per ROLE: an interactive
  chat turn is allowed a long tool-using conversation, a background pass is a
  single scripted question, and one shared number would be wrong for both.

  M-PA-1 — and the turn that hit that wall clock was precisely the one whose
  spend went unrecorded. The four background pass families each reach the
  billed tally through ONE shared helper (`contract.done_or_billed`), and the
  AST gate below keeps all four calling it.

  That gate is a ROSTER check and nothing more. Read on its own it claimed
  more than it could deliver: the first fix pass shipped with it green and all
  four families still recording 0 on a timeout, because the helper was being
  called correctly and being handed an exception whose tally sat on
  `__cause__`. The behaviour lives in
  `tests/proposal_assist/coherence/test_pass_runner.py`, which now covers BOTH
  failure shapes (raised exception, and tally-on-`__cause__`), plus the walk's
  own termination guards at the bottom of this file.

NOTHING here runs the real Claude CLI or reaches the Anthropic API: the key is
a fake literal assigned onto Settings, and `_build_options_kwargs` is a pure
kwargs builder (no filesystem, no network, no subprocess).
"""

from __future__ import annotations

import ast
import pathlib
import uuid

import pytest

from grant_lab.config import Settings
from grant_lab.proposal_assist.drafting.contract import LaunchContext
from tests.proposal_assist.test_sdk_adapter_credentials import (
    _BUILDER_IDS,
    _BUILDERS,
    _settings,
)

# Which setting each role's cap must come from. The interactive chat session is
# the only one that gets the interactive budget; the four background families
# share the pass budget. A role in the roster and not in this map is a new role
# whose bound nobody chose.
_EXPECTED_SETTING = {
    "interactive": "proposal_assist_max_turns_interactive",
    "evaluator": "proposal_assist_max_turns_pass",
    "coherence_keeper": "proposal_assist_max_turns_pass",
    "review_panel": "proposal_assist_max_turns_pass",
    "uncited_claims": "proposal_assist_max_turns_pass",
}


def test_the_two_turn_bounds_have_the_decided_defaults(_settings_env):
    """A deployment that sets neither still gets a bound. 60 interactive turns
    is room for a long tool-using drafting conversation; 20 is well above what
    any background pass has ever needed and still stops a loop."""
    settings = Settings()
    assert settings.proposal_assist_max_turns_interactive == 60
    assert settings.proposal_assist_max_turns_pass == 20


@pytest.mark.parametrize("role, build", _BUILDERS, ids=_BUILDER_IDS)
def test_every_sdk_adapter_bounds_its_turns_from_settings(
    role, build, tmp_path, pg_session_factory, _settings_env, monkeypatch
):
    """`max_turns` must reach ClaudeAgentOptions, and it must be THIS role's
    setting. Non-default values are used so a hardcoded 60/20 in the adapter
    would pass the defaults test and fail here."""
    monkeypatch.setenv("DRAFTING_ADAPTER", "claude")
    settings = _settings(tmp_path)
    settings.proposal_assist_max_turns_interactive = 11
    settings.proposal_assist_max_turns_pass = 7

    adapter = build(settings, pg_session_factory)
    kwargs = adapter._build_options_kwargs(LaunchContext(workspace_id=uuid.uuid4()))

    assert "max_turns" in kwargs, f"{role} starts an unbounded agent loop"
    assert kwargs["max_turns"] == getattr(settings, _EXPECTED_SETTING[role])


def test_the_five_roles_do_not_share_one_cap(
    tmp_path, pg_session_factory, _settings_env, monkeypatch
):
    """The shape a `settings`-level `setdefault` in the shared constructor would
    produce: all five roles on one number. Asserted as a difference so that
    collapse cannot pass."""
    monkeypatch.setenv("DRAFTING_ADAPTER", "claude")
    settings = _settings(tmp_path)
    settings.proposal_assist_max_turns_interactive = 11
    settings.proposal_assist_max_turns_pass = 7

    caps = {}
    for role, build in _BUILDERS:
        adapter = build(settings, pg_session_factory)
        caps[role] = adapter._build_options_kwargs(
            LaunchContext(workspace_id=uuid.uuid4())
        )["max_turns"]

    assert caps["interactive"] == 11
    assert {caps[r] for r in caps if r != "interactive"} == {7}


# ---------------------------------------------------------------------------
# M-PA-1 — the four pass families route their failure path through the helper
# ---------------------------------------------------------------------------

_PASS_FAMILIES = (
    "coherence",
    "evaluator",
    "review",
    "uncited_claims",
)


def _pass_runner_paths() -> dict[str, pathlib.Path]:
    root = pathlib.Path(__file__).resolve().parents[2] / "src/grant_lab/proposal_assist"
    return {p.parent.name: p for p in sorted(root.rglob("pass_runner.py"))}


def test_the_pass_family_roster_is_the_four_this_fix_covers():
    """If a fifth pass family appears, the test below must cover it too —
    otherwise "one test per family is enough, they share a helper" silently
    stops being true."""
    assert sorted(_pass_runner_paths()) == sorted(_PASS_FAMILIES)


@pytest.mark.parametrize("family", _PASS_FAMILIES)
def test_every_pass_family_recovers_the_billed_tally_in_its_except_block(family):
    """`done = done_or_billed(done, exc)` inside the handler, in all four.

    The assignment has to be INSIDE an `except` block: that is the only place
    the exception carrying the tally exists, and every later use of `done` (the
    ledger row, the job's token columns, `_fail`) reads the rebound name. A
    call anywhere else would type-check and record nothing.

    THIS IS A ROSTER CHECK, NOT A BEHAVIOURAL ONE, and the review of the first
    fix pass is the proof: a presence check passes whether or not the helper
    ever receives a tally, so it stayed green while all four families recorded
    0 on a timeout (`turn_usage_from_error` did not follow `__cause__`). It is
    kept because it does catch a deleted line in a family that has no
    end-to-end test of its own; what it cannot do is stand in for one. The
    behaviour is pinned in `tests/proposal_assist/coherence/test_pass_runner.py`
    by BOTH failure shapes — `..._dies_on_an_error_result_...` (the exception is
    the one raised) and `..._times_out_mid_stream_...` (the tally is on
    `__cause__`).
    """
    path = _pass_runner_paths()[family]
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        for stmt in ast.walk(node)
        if isinstance(stmt, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "done" for t in stmt.targets)
        and isinstance(stmt.value, ast.Call)
        and isinstance(stmt.value.func, ast.Name)
        and stmt.value.func.id == "done_or_billed"
    ]
    assert found, (
        f"{family}/pass_runner.py never recovers the billed tally in its except "
        "block, so a pass that dies before its ResultMessage records 0"
    )


# ---------------------------------------------------------------------------
# M-PA-1 — the cause-chain walk, and the two guards that bound it
#
# The walk itself is pinned BEHAVIOURALLY in
# tests/proposal_assist/coherence/test_pass_runner.py
# (`..._times_out_mid_stream_...`, which drives production's own
# wait_for -> `raise TimeoutError from exc_val` chain). What is left to pin here
# is that the walk always TERMINATES: `__cause__` is writable, so a ring is
# constructible, and losing a tally must never be worse than hanging the
# finalizer that was recording it.
# ---------------------------------------------------------------------------


def _chain(length: int, *, usage_at_end) -> BaseException:
    """`length` exceptions linked by __cause__, tally on the last one only."""
    from grant_lab.proposal_assist.drafting.contract import attach_turn_usage

    links = [RuntimeError(f"link {i}") for i in range(length)]
    for i in range(length - 1):
        links[i].__cause__ = links[i + 1]
    if usage_at_end is not None:
        attach_turn_usage(links[-1], usage_at_end)
    return links[0]


def test_the_cause_walk_terminates_on_a_cyclic_chain():
    """Run in a thread with a join deadline so a regression FAILS instead of
    hanging the whole suite (an unguarded walk over a ring never returns)."""
    import threading

    from grant_lab.proposal_assist.drafting.contract import turn_usage_from_error

    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a  # a ring, with no tally anywhere on it

    out: list[object] = []
    t = threading.Thread(target=lambda: out.append(turn_usage_from_error(a)), daemon=True)
    t.start()
    t.join(timeout=5.0)
    assert not t.is_alive(), "the cause walk never returned: the cycle guard is gone"
    assert out == [None]


def test_the_cause_walk_finds_a_tally_within_its_depth_bound_and_stops_past_it():
    """The bound is slack over the real 2-link chain, not a tuning knob — so
    assert against the constant rather than a literal, in both directions."""
    from grant_lab.proposal_assist.drafting.contract import (
        _MAX_CAUSE_CHAIN_DEPTH,
        TurnUsage,
        turn_usage_from_error,
    )

    tally = TurnUsage(tokens_in=3_000, tokens_out=300, model="claude-opus-5")
    assert turn_usage_from_error(_chain(_MAX_CAUSE_CHAIN_DEPTH, usage_at_end=tally)) == tally
    # One link past the bound the walk gives up rather than keeps going.
    assert turn_usage_from_error(_chain(_MAX_CAUSE_CHAIN_DEPTH + 1, usage_at_end=tally)) is None
