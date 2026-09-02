"""Tests for cumulative redirect budget (timeout + bytes) in fetcher.

Defect 2: _fetch_url_bytes used to reset the byte accumulator (`total = 0`)
on every redirect hop and use a fresh `timeout_s` per hop.  The fix must:
 - Compute a single `deadline_time` BEFORE the redirect loop.
 - Carry `total` across hops (cumulative byte accumulator).
 - If remaining time <= 0 before a hop, raise FetchError("budget exhausted").
"""

import httpx
import pytest

from call_manager.proposal_assist.references.fetcher import (
    FetchError,
    _fetch_url_bytes,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_transport_with_responses(responses):
    """Return a MockTransport that replays `responses` in order.

    Each element in `responses` is an httpx.Response (or a callable that
    receives the request and returns one).  After the list is exhausted
    any further request returns a 200 with empty body.
    """
    calls = [0]

    def handler(request):
        idx = calls[0]
        calls[0] += 1
        if idx < len(responses):
            r = responses[idx]
            return r(request) if callable(r) else r
        return httpx.Response(200, content=b"ok")

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Cumulative byte accumulator tests
# ---------------------------------------------------------------------------


def test_cumulative_bytes_across_redirect_hops(monkeypatch):
    """Byte cap must be cumulative across all redirect hops.

    Strategy: bypass SSRF validation so we can use a MockTransport, then
    verify that bytes returned on the *first* hop count toward the cap on
    the *second* hop.

    We set max_bytes=10 and have the first hop return a redirect (no body
    bytes, but the old code reset total=0 on each hop).  The second hop
    returns 11 bytes.  In the NEW (correct) code: total starts at 0,
    redirect hop adds 0 bytes, content hop adds 11 bytes → 11 > 10 →
    FetchError("exceeds").

    This is primarily testing the accounting logic; network I/O is mocked.
    """
    # We monkeypatch _resolve_and_validate and _pinned_transport so the
    # MockTransport is used instead of real sockets.
    import call_manager.proposal_assist.references.fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "_resolve_and_validate", lambda host, port: "1.2.3.4")

    call_count = [0]

    def fake_pinned_transport(pin_map):
        call_count[0] += 1
        if call_count[0] == 1:
            # First hop: redirect to a different hostname (avoids loop detection).
            return _make_transport_with_responses(
                [
                    httpx.Response(302, headers={"location": "https://redirect-target.com/final"}),
                ]
            )
        else:
            # Second hop: content — 11 bytes, which exceeds max_bytes=10.
            return _make_transport_with_responses(
                [
                    httpx.Response(200, content=b"x" * 11),
                ]
            )

    monkeypatch.setattr(fetcher_mod, "_pinned_transport", fake_pinned_transport)

    with pytest.raises(FetchError, match="exceeds"):
        _fetch_url_bytes("https://example.com/start", max_bytes=10)


def test_redirect_hop_bytes_successful_under_cap(monkeypatch):
    """Verify the successful (non-bomb) redirect path still works with the
    new cumulative total: a redirect to a different host followed by a small
    response that stays under max_bytes succeeds normally."""
    import call_manager.proposal_assist.references.fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "_resolve_and_validate", lambda host, port: "1.2.3.4")

    call_idx = [0]

    def fake_pinned_transport(pin_map):
        idx = call_idx[0]
        call_idx[0] += 1
        if idx == 0:
            # First hop: a redirect to a different host.
            return _make_transport_with_responses(
                [
                    httpx.Response(302, headers={"location": "https://final-host.com/doc"}),
                ]
            )
        else:
            # Second hop: 6 bytes (well under max_bytes=10).
            return _make_transport_with_responses(
                [
                    httpx.Response(200, content=b"x" * 6),
                ]
            )

    monkeypatch.setattr(fetcher_mod, "_pinned_transport", fake_pinned_transport)

    # With max_bytes=10: 0 bytes (redirect) + 6 bytes (content) = 6 <= 10 → OK.
    result, _ = _fetch_url_bytes("https://example.com/start", max_bytes=10)
    assert result == b"x" * 6


# ---------------------------------------------------------------------------
# Deadline exhaustion tests
# ---------------------------------------------------------------------------


def test_budget_exhausted_before_hop_raises(monkeypatch):
    """If the wall-clock deadline is already past before a hop begins,
    _fetch_url_bytes must raise FetchError('budget exhausted') without
    attempting the network call.

    We achieve a zero remaining budget by monkeypatching time.monotonic
    so it always returns a time far in the future relative to the deadline.
    """
    import call_manager.proposal_assist.references.fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "_resolve_and_validate", lambda host, port: "1.2.3.4")

    call_count = [0]

    def fake_pinned_transport(pin_map):
        # First hop: redirect to a different host so we re-enter the loop.
        call_count[0] += 1
        if call_count[0] == 1:
            return _make_transport_with_responses(
                [
                    httpx.Response(302, headers={"location": "https://redirect-hop2.com/page"}),
                ]
            )
        # If we get here, the budget check failed to raise — return something
        # so the test can detect the failure.
        return _make_transport_with_responses([httpx.Response(200, content=b"oops")])

    monkeypatch.setattr(fetcher_mod, "_pinned_transport", fake_pinned_transport)

    # Monkeypatch time.monotonic so after the first call (which sets
    # deadline_time) all subsequent calls return a value far in the future,
    # making remaining_s <= 0 for the second hop.
    import time as time_mod

    real_monotonic = time_mod.monotonic
    calls = [0]

    def fake_monotonic():
        calls[0] += 1
        if calls[0] == 1:
            # First call: used to compute deadline_time = t0 + timeout_s.
            # Return a normal value.
            return real_monotonic()
        # All subsequent calls: return far-future so remaining_s = deadline - now < 0.
        return real_monotonic() + 1_000_000

    # Patch `time.monotonic` in the fetcher module's namespace.
    # The fetcher must `import time` and call `time.monotonic()` for this to work.
    monkeypatch.setattr(fetcher_mod.time, "monotonic", fake_monotonic)

    with pytest.raises(FetchError, match="budget exhausted"):
        _fetch_url_bytes("https://example.com/start", timeout_s=60.0)


# ---------------------------------------------------------------------------
# Review 2026-09-02 §2.5(b)(c) / Appendix A4 F2, F3 — bounds INSIDE one hop
# ---------------------------------------------------------------------------


def _counting_body(piece: bytes, pieces: int):
    """A streamed response body of `pieces` × `piece` that records how many
    pieces the fetcher actually pulled off the wire."""
    pulled = [0]

    def gen():
        for _ in range(pieces):
            pulled[0] += 1
            yield piece

    return gen(), pulled


def test_error_status_reads_at_most_a_snippet_of_the_body(monkeypatch):
    """F2: `response.read()` on a 4xx/5xx materialised the whole body — 200 MB
    served with `max_bytes=1 MiB` was read in full (peak RSS 626 MB). The error
    path must pull one small piece for the message snippet and stop."""
    import call_manager.proposal_assist.references.fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "_resolve_and_validate", lambda host, port: "1.2.3.4")
    body, pulled = _counting_body(b"x" * 65536, 80)  # 5 MiB, served under a 1 MiB cap
    monkeypatch.setattr(
        fetcher_mod,
        "_pinned_transport",
        lambda pin_map: httpx.MockTransport(lambda req: httpx.Response(500, content=body)),
    )
    with pytest.raises(FetchError, match="HTTP 500"):
        _fetch_url_bytes("https://example.com/err", max_bytes=1024 * 1024, timeout_s=5.0)
    assert pulled[0] <= 1, f"error body: pulled {pulled[0]} of 80 pieces — read whole"


def test_deadline_is_enforced_inside_the_body_read(monkeypatch):
    """F3: `timeout_s` was checked only BETWEEN redirect hops and passed to httpx
    as a per-read timeout, so a slow drip that delivers a byte every 0.4 s never
    tripped it (12 bytes came back after 4.5 s under `timeout_s=1.0`; the only
    bound was max_bytes — 50 MB at one byte per 59 s is 93 years, holding an
    ingest slot throughout). The deadline must be re-checked between pieces."""
    import types

    import call_manager.proposal_assist.references.fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "_resolve_and_validate", lambda host, port: "1.2.3.4")
    clock = [1000.0]
    pulled = [0]

    def drip():
        for _ in range(100):
            clock[0] += 0.4  # each piece costs 0.4 s of wall time
            pulled[0] += 1
            yield b"x"

    monkeypatch.setattr(
        fetcher_mod,
        "_pinned_transport",
        lambda pin_map: httpx.MockTransport(lambda req: httpx.Response(200, content=drip())),
    )
    # Only the fetcher's own clock is faked; httpx keeps the real one.
    monkeypatch.setattr(fetcher_mod, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))

    with pytest.raises(FetchError, match="budget exhausted"):
        _fetch_url_bytes("https://example.com/drip", max_bytes=10_000, timeout_s=1.0)
    assert pulled[0] < 100, "the drip ran to completion — the deadline was not checked in-loop"
    assert pulled[0] < 10_000, "must be the deadline that fired, not max_bytes"


def test_per_read_timeout_is_capped_even_with_a_long_budget(monkeypatch):
    """F3, second half: the per-read timeout handed to httpx is
    `min(remaining, _READ_TIMEOUT_S)`, so a single silent read cannot consume
    the whole budget before the in-loop check gets a chance to run."""
    import call_manager.proposal_assist.references.fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "_resolve_and_validate", lambda host, port: "1.2.3.4")
    monkeypatch.setattr(
        fetcher_mod,
        "_pinned_transport",
        lambda pin_map: httpx.MockTransport(lambda req: httpx.Response(200, content=b"ok")),
    )
    seen: list[object] = []
    real_client = httpx.Client

    class RecordingClient(real_client):
        def __init__(self, *args, timeout=None, **kwargs):
            seen.append(timeout)
            super().__init__(*args, timeout=timeout, **kwargs)

    monkeypatch.setattr(fetcher_mod.httpx, "Client", RecordingClient)
    body, _ = _fetch_url_bytes("https://example.com/ok", max_bytes=1024, timeout_s=600.0)
    assert body == b"ok"
    assert seen and seen[0] <= fetcher_mod._READ_TIMEOUT_S < 600.0
