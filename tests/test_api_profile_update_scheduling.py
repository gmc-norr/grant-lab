"""Task 8 gates: hole 1, the scheduling guard."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sqltext

from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.main import create_app

_BODY = {
    "description": "Imaging research lab.", "areas": "oncology\nimaging",
    "budget_min": "1000", "budget_max": "2000", "budget_currency": "SEK",
    "trl_min": "3", "trl_max": "7", "must_include": "pediatric", "hard_exclusions": "",
    "level": "group", "career_stage": "", "applicant_types": "", "role": "",
    "years_since_phd": "", "org_type": "university_hospital", "capabilities": "biobank",
    "patient_populations": "", "scale": "", "funding_instruments": "project_grant",
    "funder_exclusions": "", "country": "SE", "host_institution": "UmU",
}


@pytest.fixture()
def app(monkeypatch, pg_url):
    # Every env var set INLINE via monkeypatch. Never read the real .env.
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


@pytest.fixture()
def owner_profile(app, pg_session_factory, profile_factory):
    """(client, profile_id), already normalized by one real PUT, so a second
    identical PUT is a genuine no-op rather than a first normalization."""
    pid, uid = profile_factory(name="Lab", description="Imaging research lab.")
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with (
        patch("grant_lab.web.api.profiles.post_edit.refresh_and_backfill_in_background"),
        client,
    ):
        r = client.put(f"/api/profiles/{pid}", json=_BODY)
    assert r.status_code == 200, r.text
    return client, pid


def test_an_areas_only_edit_schedules_the_refresh(owner_profile):
    """GATE 2. build_profile_text composes description OR areas OR org fields,
    so a profile with an empty description and non-empty areas has real
    embedding text -- but the guard read `body.description`, so editing one
    scheduled NOTHING and it stayed stale forever."""
    client, pid = owner_profile
    with (
        patch(
            "grant_lab.web.api.profiles.post_edit.refresh_and_backfill_in_background"
        ) as task,
        client,
    ):
        r = client.put(
            f"/api/profiles/{pid}",
            json={**_BODY, "description": "", "areas": "liver cancer\ntransplant rejection"},
        )
    assert r.status_code == 200, r.text
    assert task.called, "an areas-only edit scheduled no refresh and no backfill"


def test_an_identical_put_schedules_nothing(owner_profile, pg_session_factory):
    """GATE 3 -- the load-bearing one for the canonicalizer's numeric rule.

    Today an unchanged PUT enqueues 90 days of ranking jobs whenever the
    description happens to be non-empty. The endpoint writes budget_min with
    bounded_int_or_400 (an INT); a float 1000.0 in the row is what the
    canonicalizer has to absorb.
    """
    client, pid = owner_profile
    # Seeded with RAW SQL, not through the ORM. `p.structured = {**p.structured,
    # "budget_min": 1000.0}` is a SILENT NO-OP: Python's `==` on dicts holds
    # `{"budget_min": 1000} == {"budget_min": 1000.0}`, so SQLAlchemy's change
    # detection sees no difference and emits no UPDATE at all. The gate then
    # passes for the exact reason its own docstring warns about -- the value
    # was already normalized -- and passes with the canonicalizer DELETED.
    with pg_session_factory() as s:
        s.execute(
            sqltext(
                "UPDATE profiles SET structured = jsonb_set(structured, '{budget_min}', "
                "'1000.0'::jsonb) WHERE id = :i"
            ),
            {"i": pid},
        )
        s.commit()
        raw = s.execute(
            sqltext("SELECT structured->>'budget_min' FROM profiles WHERE id=:i"), {"i": pid}
        ).scalar_one()
    assert raw == "1000.0", f"seed did not land: {raw!r}"

    with (
        patch(
            "grant_lab.web.api.profiles.post_edit.refresh_and_backfill_in_background"
        ) as task,
        client,
    ):
        r = client.put(f"/api/profiles/{pid}", json=_BODY)
    assert r.status_code == 200, r.text
    assert not task.called, "an unchanged PUT enqueued a 90-day backfill"


def test_a_real_change_still_schedules(owner_profile):
    """Discriminating control: without this, 'never schedule' passes gate 3."""
    client, pid = owner_profile
    with (
        patch(
            "grant_lab.web.api.profiles.post_edit.refresh_and_backfill_in_background"
        ) as task,
        client,
    ):
        r = client.put(
            f"/api/profiles/{pid}",
            json={**_BODY, "description": "Something genuinely different"},
        )
    assert r.status_code == 200, r.text
    assert task.called
