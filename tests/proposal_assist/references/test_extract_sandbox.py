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


# ---------------------------------------------------------------------------
# Review 2026-09-02 §2.5(d) / Appendix A4 F6 — the sandbox bounds memory too
# ---------------------------------------------------------------------------
#
# A 1.99 MB PDF whose one page is zlib(2 GiB of spaces) made the child allocate
# 4.04 GB and "finish" inside the 30 s CPU budget. The worker now caps its own
# address space (RLIMIT_AS) before extracting; a MemoryError is reported like any
# other extraction failure. Measured 2026-09-02 on this box: a spawned child sits
# at 48 MB of address space after importing the extractor and extracting a 1 MB
# text file, and allocates 3 GiB without complaint when unlimited.


class _ListQueue:
    def __init__(self):
        self.items: list = []

    def put(self, item):
        self.items.append(item)


def test_worker_applies_the_memory_limit_before_extracting(monkeypatch):
    import resource

    from call_manager.proposal_assist.references import extract_sandbox as sb

    calls: list = []
    monkeypatch.setattr(sb.resource, "setrlimit", lambda res, lim: calls.append((res, lim)))
    q = _ListQueue()
    sb._worker("text/markdown", b"# T\n\nbody", q)
    limit = sb._MAX_ADDRESS_SPACE_BYTES
    assert calls == [(resource.RLIMIT_AS, (limit, limit))]
    assert limit >= 1024**3, "must leave real extraction (pdfminer, openpyxl) room to run"
    assert q.items and q.items[0][0] == "ok"


def test_memory_error_inside_the_child_is_reported_as_an_extraction_failure(monkeypatch):
    from call_manager.proposal_assist.references import extract_sandbox as sb

    monkeypatch.setattr(sb.resource, "setrlimit", lambda res, lim: None)

    def _boom(**kwargs):
        raise MemoryError

    monkeypatch.setattr(sb, "extract_text", _boom)
    q = _ListQueue()
    sb._worker("text/plain", b"x", q)
    status, payload = q.items[0]
    assert status == "err"
    assert "MemoryError" in payload and "memory budget" in payload


def _allocate_under_the_limit(q) -> None:
    # Module-level so the spawn context can import it by qualified name.
    from call_manager.proposal_assist.references import extract_sandbox as sb

    sb._apply_memory_limit()
    try:
        bytearray(3 * 1024**3)
        q.put("ok")
    except MemoryError:
        q.put("MemoryError")


def test_the_limit_bites_in_a_real_spawned_child():
    """The syscall, not a mock: under the worker's own limit a 3 GiB allocation
    fails in a real spawned child of this interpreter."""
    from call_manager.proposal_assist.references.extract_sandbox import _CTX

    q = _CTX.Queue()
    proc = _CTX.Process(target=_allocate_under_the_limit, args=(q,), daemon=True)
    proc.start()
    try:
        assert q.get(timeout=60) == "MemoryError"
    finally:
        proc.join(10)
