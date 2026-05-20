import pytest

from call_manager.proposal_assist.hashing import source_text_hash


def test_source_text_hash_is_64_lowercase_hex():
    h = source_text_hash("hello world")
    assert len(h) == 64
    assert h == h.lower()
    int(h, 16)  # raises ValueError if not valid hex


def test_source_text_hash_stable():
    assert source_text_hash("hello") == source_text_hash("hello")


def test_source_text_hash_differs_on_content():
    assert source_text_hash("a") != source_text_hash("b")


def test_source_text_hash_treats_empty_string():
    h = source_text_hash("")
    # sha256("") is well-known
    assert h == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_source_text_hash_rejects_non_str():
    with pytest.raises(TypeError):
        source_text_hash(b"bytes are not allowed")
