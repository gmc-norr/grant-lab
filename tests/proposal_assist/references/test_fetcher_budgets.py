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
            return _make_transport_with_responses([
                httpx.Response(302, headers={"location": "https://redirect-target.com/final"}),
            ])
        else:
            # Second hop: content — 11 bytes, which exceeds max_bytes=10.
            return _make_transport_with_responses([
                httpx.Response(200, content=b"x" * 11),
            ])

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
            return _make_transport_with_responses([
                httpx.Response(302, headers={"location": "https://final-host.com/doc"}),
            ])
        else:
            # Second hop: 6 bytes (well under max_bytes=10).
            return _make_transport_with_responses([
                httpx.Response(200, content=b"x" * 6),
            ])

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
            return _make_transport_with_responses([
                httpx.Response(302, headers={"location": "https://redirect-hop2.com/page"}),
            ])
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
