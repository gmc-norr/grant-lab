"""Spec D2: what the call contributed to a ranking. Gates G3, G4, G5, G15 (unit level)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from grant_lab.profiles.fingerprint import (
    FINGERPRINT_VERSION,
    RANKED_RAW_KEYS,
    call_rank_fingerprint,
)
from grant_lab.ranking.rank_pair import rank_pair
from tests.freshness_helpers import ollama_clean

_DEADLINE = datetime(2099, 3, 1, 12, 0, tzinfo=UTC)


def _call(**over):
    base = dict(
        url="https://example.org/c/1",
        title="AI in pathology",
        summary="Funding for AI diagnostics.",
        full_text="Body text.",
        deadline=_DEADLINE,
        budget_min=100_000,
        budget_max=500_000,
        currency="EUR",
        programme="Digital Europe",
        keywords=["ai", "health"],
        published_at=datetime(2026, 9, 1, tzinfo=UTC),
        raw={"trl_min": 3, "trl_max": 6, "metadata": {"fetched": "2026-09-24"}},
        embedding_fingerprint="v2:" + "a" * 64,
        embedding_model="bge-m3",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_it_carries_the_version_tag():
    assert call_rank_fingerprint(_call()).startswith(f"{FINGERPRINT_VERSION}:")


def test_ranked_raw_keys_are_the_two_trl_bounds():
    assert RANKED_RAW_KEYS == ("trl_min", "trl_max")


@pytest.mark.parametrize(
    "field, value",
    [
        ("url", "https://example.org/c/2"),
        ("title", "Something else"),
        ("summary", "Changed summary."),
        ("full_text", "Changed body."),
        ("deadline", _DEADLINE + timedelta(days=30)),
        ("budget_min", 1),
        ("budget_max", 2),
        ("currency", "SEK"),
        ("programme", "Horizon Europe"),
        ("keywords", ["ai"]),
        ("published_at", datetime(2026, 9, 2, tzinfo=UTC)),
        ("embedding_fingerprint", "v2:" + "b" * 64),  # G15, unit level
        ("embedding_model", "mxbai-embed-large"),
    ],
)
def test_every_ranked_field_changes_it(field, value):
    assert call_rank_fingerprint(_call(**{field: value})) != call_rank_fingerprint(_call())


def test_a_raw_change_outside_the_ranked_keys_does_not_change_it():
    """G3, unit level. Raw churns between fetches of an identical call."""
    churned = _call(raw={"trl_min": 3, "trl_max": 6, "metadata": {"fetched": "2026-09-25"}})
    assert call_rank_fingerprint(churned) == call_rank_fingerprint(_call())


def test_a_trl_change_does_change_it():
    """G4, unit level. Without this, 'ignore raw entirely' passes G3."""
    moved = _call(raw={"trl_min": 4, "trl_max": 6, "metadata": {"fetched": "2026-09-24"}})
    assert call_rank_fingerprint(moved) != call_rank_fingerprint(_call())


def test_the_same_instant_in_another_zone_is_the_same_fingerprint():
    """The DB hands back timestamptz in the connection's zone; a Python-built
    Call holds UTC. Equal instants must hash equally, or every pair a test
    builds in Python looks stale to a row read back from Postgres."""
    cet = timezone(timedelta(hours=1))
    shifted = _call(deadline=_DEADLINE.astimezone(cet))
    assert call_rank_fingerprint(shifted) == call_rank_fingerprint(_call())


class _Recording(dict):
    """A dict that remembers every key it was asked for."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.read: set[str] = set()

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.read.add(key)
        return super().__getitem__(key)


def test_ranking_reads_only_the_ranked_raw_keys():
    """G5. Keys read from call.structured during a real rank_pair run must be a
    subset of RANKED_RAW_KEYS. A key read and not hashed is a ranking input the
    fingerprint cannot see."""
    rec = _Recording({"trl_min": 3, "trl_max": 6, "metadata": {"x": 1}})
    call = _call(structured=rec, raw=rec)
    profile = SimpleNamespace(
        name="P",
        kind="organizational_unit",
        description="AI in pathology",
        structured={"areas": ["AI"], "trl_min": 2, "trl_max": 7},
        budget_currency=None,
        embedding=None,
        embedding_model=None,
    )
    rank_pair(
        client=ollama_clean(),
        profile=profile,
        call=call,
        now=datetime(2026, 9, 24, tzinfo=UTC),
        embedding_threshold=0.0,
    )
    assert rec.read, "ranking read nothing from call.structured -- the gate is hollow"
    assert rec.read <= set(RANKED_RAW_KEYS), rec.read - set(RANKED_RAW_KEYS)
