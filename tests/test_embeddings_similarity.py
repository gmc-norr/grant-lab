from __future__ import annotations

import math
from types import SimpleNamespace

from call_manager.embeddings.similarity import (
    build_call_text,
    build_profile_text,
    cosine,
)


def test_cosine_orthogonal_vectors_zero():
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert cosine(a, b) == 0.0


def test_cosine_identical_vectors_one():
    a = [0.5, 0.5, 0.7]
    assert math.isclose(cosine(a, a), 1.0, abs_tol=1e-9)


def test_cosine_anti_parallel_minus_one():
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert math.isclose(cosine(a, b), -1.0, abs_tol=1e-9)


def test_cosine_returns_none_when_either_input_is_none():
    assert cosine(None, [1.0, 0.0]) is None
    assert cosine([1.0, 0.0], None) is None


def test_cosine_returns_none_when_either_vector_is_zero():
    """Zero vector → undefined cosine. Return None, don't divide-by-zero."""
    assert cosine([0.0, 0.0, 0.0], [1.0, 0.0, 0.0]) is None
    assert cosine([1.0, 0.0, 0.0], [0.0, 0.0, 0.0]) is None


def test_cosine_returns_none_when_dimensions_differ():
    """Mismatched dims is a programming error; surface as None rather
    than silently returning a meaningless number."""
    assert cosine([1.0, 0.0], [1.0, 0.0, 0.0]) is None


def test_cosine_returns_none_on_nan_component():
    """A NaN component (e.g. a poisoned stored vector) yields a NaN result;
    treat as UNKNOWN (None) rather than letting NaN propagate into ranking
    comparisons where every comparison would silently be False."""
    assert cosine([float("nan"), 1.0], [1.0, 0.0]) is None


def test_cosine_returns_none_on_inf_component():
    """Inf components overflow na*nb to inf and yield a non-finite result —
    also collapse to None."""
    assert cosine([float("inf"), 1.0], [1.0, 0.0]) is None


def test_build_call_text_concatenates_title_summary_truncated_full_text():
    call = SimpleNamespace(
        title="A Title",
        summary="A summary.",
        full_text="x" * 9000,  # > 8000 char cap
    )
    text = build_call_text(call)
    assert text.startswith("A Title")
    assert "A summary." in text
    # full_text truncated to 8000 chars; total length should be ~8020 (with separators)
    assert "x" * 8000 in text
    assert "x" * 8001 not in text


def test_build_call_text_handles_null_summary_and_full_text():
    call = SimpleNamespace(title="Just a title", summary=None, full_text=None)
    text = build_call_text(call)
    assert text.strip() == "Just a title"


def test_build_profile_text_includes_description_and_areas():
    """Codex #2: profile.areas is the structured signal of what the group
    does. Embedding description alone misses that signal when descriptions
    are terse — include areas so semantic similarity reflects both the
    prose summary and the tagged topic surface."""
    profile = SimpleNamespace(
        description="Pathology research focus.",
        structured={
            "areas": ["digital pathology", "federated learning"],
            "must_include": ["xyz"],
            "hard_exclusions": ["animal studies"],
            "budget_min": 100,
            "budget_max": 500,
            "trl_min": 3,
            "trl_max": 7,
        },
    )
    text = build_profile_text(profile)
    assert "Pathology research focus." in text
    assert "digital pathology" in text
    assert "federated learning" in text
    # must_include, hard_exclusions, budget, TRL are keyword/numeric —
    # not semantic; including them would dilute the signal.
    assert "xyz" not in text
    assert "animal studies" not in text
    assert "100" not in text


def test_build_profile_text_works_with_only_areas():
    """A profile with empty description but populated areas still has
    signal — embed it rather than treating it as unembeddable."""
    profile = SimpleNamespace(
        description="",
        structured={"areas": ["clinical AI"]},
    )
    text = build_profile_text(profile)
    assert "clinical AI" in text


def test_build_profile_text_works_with_only_description():
    """Backwards-compat: profiles that have no `areas` still get their
    description embedded exactly as before (modulo trailing whitespace)."""
    profile = SimpleNamespace(
        description="Pathology research focus.",
        structured={},
    )
    assert build_profile_text(profile).strip() == "Pathology research focus."


def test_build_profile_text_empty_profile_returns_empty_string():
    profile = SimpleNamespace(description="", structured={})
    assert build_profile_text(profile) == ""


def test_build_profile_text_tolerates_none_structured():
    """Profiles before structured was populated may have NULL `structured`
    from older rows — treat as empty rubric."""
    profile = SimpleNamespace(description="X", structured=None)
    assert build_profile_text(profile).strip() == "X"
