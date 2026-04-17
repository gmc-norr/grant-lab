from call_monitor.auth.rate_limit import SlidingWindowLimiter


def test_allows_under_limit():
    rl = SlidingWindowLimiter(max_requests=3, window_seconds=60)
    for _ in range(3):
        assert rl.allow("alice@ki.se") is True


def test_blocks_over_limit():
    rl = SlidingWindowLimiter(max_requests=3, window_seconds=60)
    for _ in range(3):
        rl.allow("alice@ki.se")
    assert rl.allow("alice@ki.se") is False


def test_separate_keys_are_independent():
    rl = SlidingWindowLimiter(max_requests=1, window_seconds=60)
    assert rl.allow("a@ki.se") is True
    assert rl.allow("b@ki.se") is True
    assert rl.allow("a@ki.se") is False


def test_window_expires(monkeypatch):
    clock = {"t": 1000.0}

    def fake_time():
        return clock["t"]

    rl = SlidingWindowLimiter(max_requests=2, window_seconds=10, clock=fake_time)
    assert rl.allow("x") is True
    assert rl.allow("x") is True
    assert rl.allow("x") is False

    clock["t"] += 11  # past the window
    assert rl.allow("x") is True
