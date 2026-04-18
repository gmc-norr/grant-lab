from __future__ import annotations

import math
from types import SimpleNamespace

from call_monitor.embeddings.similarity import (
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


def test_build_profile_text_uses_description_only():
    profile = SimpleNamespace(
        description="Pathology research focus.",
        structured={"areas": ["pathology"], "must_include": ["xyz"]},
    )
    assert build_profile_text(profile) == "Pathology research focus."


def test_build_profile_text_empty_description_returns_empty_string():
    profile = SimpleNamespace(description="", structured={})
    assert build_profile_text(profile) == ""
