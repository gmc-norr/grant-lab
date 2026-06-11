import pytest

from call_manager.auth.redirects import MAX_NEXT_BYTES, safe_next


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "relative/no/leading/slash",
        "//evil.com",
        "https://evil.com",
        "http://evil.com",
        "/\\evil.com",
        "/back\\slash",
        "/has\ttab",
        "/has\nnewline",
        "/has\rcarriage",
        "/has\x7fdel",
        "/has\x85nel",   # U+0085 NEXT LINE — C1 control
        "/has\x9ffoo",   # U+009F — last C1 control
        "/" + "a" * (MAX_NEXT_BYTES + 1),
    ],
)
def test_safe_next_rejects(value):
    assert safe_next(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "/",
        "/calls/5",
        "/calls/5?profile=3",
        "/calls/5?profile=3&source=x",
    ],
)
def test_safe_next_accepts_local_paths(value):
    # Returns the path unchanged (decoded form); callers must urlencode it.
    assert safe_next(value) == value


def test_safe_next_accepts_at_length_boundary():
    value = "/" + "a" * (MAX_NEXT_BYTES - 1)
    assert len(value.encode("utf-8")) == MAX_NEXT_BYTES
    assert safe_next(value) == value
