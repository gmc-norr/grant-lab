from datetime import UTC, datetime

from call_monitor.db.models import Call, Profile, ProfileKind
from call_monitor.ranking.prompt import PROMPT_VERSION, build_messages


def _profile():
    return Profile(
        id=1,
        name="Pathology AI",
        kind=ProfileKind.GROUP,
        description="Develop AI methods for digital pathology and federated learning.",
        structured={
            "areas": ["digital pathology", "federated learning"],
            "budget_min": 100_000,
            "budget_max": 5_000_000,
            "trl_min": 4,
            "trl_max": 8,
            "must_include": ["pathology"],
            "hard_exclusions": ["animal studies"],
        },
        is_active=True,
        created_by=1,
    )


def _call():
    return Call(
        id=42,
        source_id=1,
        external_id="HORIZON-X",
        url="https://x",
        title="AI for digital pathology",
        summary="EU funding for AI-based digital pathology research.",
        full_text=None,
        deadline=datetime(2026, 9, 1, tzinfo=UTC),
        budget_max=2_000_000,
        currency="EUR",
        programme="Horizon Europe",
        keywords=["health", "ai", "pathology"],
        published_at=datetime(2026, 4, 1, tzinfo=UTC),
        raw={},
    )


def test_prompt_version_is_v1():
    assert PROMPT_VERSION == "v1"


def test_messages_include_profile_in_system_and_call_in_user():
    msgs = build_messages(_profile(), _call())
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert "Pathology AI" in msgs[0]["content"]
    assert "digital pathology" in msgs[0]["content"]
    assert "animal studies" in msgs[0]["content"]
    assert "AI for digital pathology" in msgs[1]["content"]
    assert "Horizon Europe" in msgs[1]["content"]


def test_system_block_is_deterministic_per_profile():
    """For the inference engine's prefix cache to work, the system block
    MUST be byte-identical across calls for the same profile."""
    p = _profile()
    a = build_messages(p, _call())
    b = build_messages(p, _call())
    assert a[0] == b[0]


def test_summary_is_truncated_when_full_text_long():
    p = _profile()
    c = _call()
    c.full_text = "X" * 5000
    msgs = build_messages(p, c)
    # User block contains call body but not the entire 5000-char dump
    assert len(msgs[1]["content"]) < 5000
