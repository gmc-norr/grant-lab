import pytest
from fastapi import FastAPI, HTTPException, Request

from grant_lab.web.client_ip import MAX_EMAIL_LEN, client_ip, email_fingerprint


def _req(*, headers=None, client=("203.0.113.5", 1234), trusted_proxy=False, hops=1):
    app = FastAPI()
    app.state.settings = type(
        "S", (), {"trusted_proxy": trusted_proxy, "trusted_proxy_hops": hops}
    )()
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "headers": raw, "app": app, "client": client}
    return Request(scope)


def test_max_email_len_is_320():
    assert MAX_EMAIL_LEN == 320


def test_email_fingerprint_is_stable_12_hex():
    fp = email_fingerprint("a@umu.se")
    assert len(fp) == 12 and fp == email_fingerprint("a@umu.se")


def test_direct_client_host():
    assert client_ip(_req()) == "203.0.113.5"


def test_absent_client_400():
    with pytest.raises(HTTPException) as e:
        client_ip(_req(client=None))
    assert e.value.status_code == 400


def test_trusted_proxy_takes_nth_from_right():
    r = _req(headers={"X-Forwarded-For": "1.1.1.1, 9.9.9.9"}, trusted_proxy=True, hops=1)
    assert client_ip(r) == "9.9.9.9"


def test_trusted_proxy_too_few_hops_400():
    r = _req(headers={"X-Forwarded-For": "1.1.1.1"}, trusted_proxy=True, hops=2)
    with pytest.raises(HTTPException) as e:
        client_ip(r)
    assert e.value.status_code == 400
