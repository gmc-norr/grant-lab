from datetime import UTC, datetime, timedelta

from grant_lab.digest.assemble import (
    DigestCallEntry,
    DigestPayload,
    DigestProfileSection,
)
from grant_lab.digest.render import render_html, render_text


def test_digest_html_shows_v2_one_line_summary_in_rationale_slot():
    """A v2 ranking entry's rationale is the criterion summary; templates
    render it next to the score without needing a v2-specific branch."""
    now = datetime(2026, 4, 17, tzinfo=UTC)
    entry = DigestCallEntry(
        call_id=1,
        title="V2 Call",
        summary="x",
        deadline=now + timedelta(days=10),
        score=80,
        rationale="Topic 4/5 · Eligibility: pass · Exclusions: pass",
        url="http://app/calls/1",
        bucket="top",
        also_in_profiles=[],
    )
    section = DigestProfileSection(profile_id=1, profile_name="P", entries=[entry])
    payload = DigestPayload(
        user_id=1,
        user_email="u@umu.se",
        sections=[section],
        total_calls=1,
    )
    html = render_html(payload, dashboard_url="http://app/")
    assert "Topic 4/5" in html
    assert "Eligibility: pass" in html


def test_digest_text_shows_v2_one_line_summary():
    entry = DigestCallEntry(
        call_id=1,
        title="V2 Call",
        summary="x",
        deadline=None,
        score=80,
        rationale="Topic 5/5 · Eligibility: pass · Exclusions: pass",
        url="http://app/calls/1",
        bucket="top",
        also_in_profiles=[],
    )
    section = DigestProfileSection(profile_id=1, profile_name="P", entries=[entry])
    payload = DigestPayload(
        user_id=1,
        user_email="u@umu.se",
        sections=[section],
        total_calls=1,
    )
    text = render_text(payload, dashboard_url="http://app/")
    assert "Topic 5/5" in text
