from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from call_manager.ranking.prompt import build_messages


def _make_call():
    return SimpleNamespace(
        title="A relevant call",
        summary="Brief abstract.",
        full_text=None,
        deadline=datetime.now(UTC) + timedelta(days=30),
        budget_min=None,
        budget_max=None,
        currency=None,
        programme=None,
    )


def test_pre_migration_profile_ranks_without_error():
    """A profile with only the OLD structured keys (no new namespaces) must
    still build a ranking prompt without KeyError."""

    class _Legacy:
        name = "Legacy Group"
        kind = "organizational_unit"
        description = "d"
        budget_currency = None
        structured = {"areas": ["x"], "hard_exclusions": [], "budget_min": None}

    msgs = build_messages(profile=_Legacy(), call=_make_call())
    assert msgs and any("Legacy Group" in m["content"] for m in msgs)


def test_embedding_text_includes_org_capabilities():
    """Phase 5 (Task 28): org capabilities + patient_populations now feed the
    embed text so structured org context shapes retrieval. Task 31 NULLs
    existing vectors to force a re-embed under this richer text."""
    from call_manager.embeddings.similarity import build_profile_text

    class _P:
        description = "Hepatology unit"
        structured = {
            "areas": ["liver"],
            "org": {"capabilities": ["biobank"]},
        }

    text = build_profile_text(_P())
    assert "Hepatology unit" in text and "liver" in text
    assert "biobank" in text  # org capabilities now enter the embed text
