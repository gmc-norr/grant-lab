"""extract_text_bounded() runs untrusted file extraction in a killable
subprocess and raises ExtractError when it exceeds a wall-clock budget.

Testing technique: the worker runs in a ``spawn``-ed child that re-imports the
module fresh, so a parent-side monkeypatch never reaches it. We therefore drive
*real* work through the real ``extract_text`` — a normal doc, a deliberately
large doc, and a deliberately huge doc — and assert on the end-to-end result.
"""

from __future__ import annotations

import time

import pytest

from call_manager.proposal_assist.references.extract import ExtractError
from call_manager.proposal_assist.references.extract_sandbox import extract_text_bounded


def test_returns_sections_for_normal_input():
    sections = extract_text_bounded(
        mime="text/markdown", data=b"# Title\n\nSome body text.", timeout_seconds=30.0
    )
    assert sections
    assert any("body text" in text for _, text in sections)


def test_large_payload_is_not_a_false_timeout():
    """A >64 KiB extraction result must come back intact, not be misread as a
    timeout. A multiprocessing.Queue feeder thread blocks once the pickled
    payload exceeds the OS pipe buffer (~64 KiB), so the implementation must
    drain the queue while waiting rather than join()-then-get()."""
    data = b"# Title\n\n" + (b"paragraph of text. " * 8) * 1000
    sections = extract_text_bounded(mime="text/markdown", data=data, timeout_seconds=30.0)
    joined = "".join(text for _, text in sections)
    assert len(joined.encode()) > 64 * 1024


def test_times_out_and_kills_the_child():
    """A genuinely slow parse (~millions of markdown lines) with a tiny budget
    must terminate the child and raise ExtractError promptly — not run to the
    parse's natural completion."""
    data = ("\n".join(f"# H{i}\nbody {i}" for i in range(2_000_000))).encode()

    start = time.monotonic()
    with pytest.raises(ExtractError):
        extract_text_bounded(mime="text/markdown", data=data, timeout_seconds=0.5)
    elapsed = time.monotonic() - start
    # Prompt kill: the 0.5s budget plus terminate/join + spawn slack, well under
    # the multi-second parse it would otherwise take.
    assert elapsed < 20.0


def test_child_extraction_error_surfaces_as_extract_error():
    """When extract_text raises inside the child (here: an unsupported MIME),
    the parent re-raises it as ExtractError rather than hanging or crashing."""
    with pytest.raises(ExtractError):
        extract_text_bounded(
            mime="application/x-not-a-real-mime", data=b"garbage", timeout_seconds=30.0
        )
