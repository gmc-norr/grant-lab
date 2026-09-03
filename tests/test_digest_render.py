from datetime import UTC, datetime, timedelta

from call_manager.digest.assemble import (
    DigestCallEntry,
    DigestPayload,
    DigestProfileSection,
)
from call_manager.digest.render import build_subject, render_html, render_text


def _payload():
    now = datetime(2026, 4, 17, tzinfo=UTC)
    must = DigestCallEntry(
        call_id=1,
        title="Pathology AI",
        summary="great",
        deadline=now + timedelta(days=14),
        score=None,
        rationale=None,
        url="http://app/calls/1",
        bucket="must_include",
        also_in_profiles=[],
    )
    top = DigestCallEntry(
        call_id=2,
        title="Funding Vinnova",
        summary="apply",
        deadline=now + timedelta(days=30),
        score=82,
        rationale="strong fit",
        url="http://app/calls/2",
        bucket="top",
        also_in_profiles=["Other Group"],
    )
    border = DigestCallEntry(
        call_id=3,
        title="Borderline call",
        summary="maybe",
        deadline=None,
        score=52,
        rationale="weak fit",
        url="http://app/calls/3",
        bucket="borderline",
        also_in_profiles=[],
    )
    section = DigestProfileSection(
        profile_id=1,
        profile_name="Group A",
        entries=[must, top, border],
    )
    return DigestPayload(
        user_id=42,
        user_email="u@umu.se",
        sections=[section],
        total_calls=3,
    )


def test_subject_has_count_and_profile_names():
    subj = build_subject(_payload())
    assert "3" in subj
    assert "Group A" in subj


def test_html_renders_titles_and_links():
    html = render_html(_payload(), dashboard_url="http://app/")
    assert "<html" in html.lower()
    assert "Pathology AI" in html
    assert "Funding Vinnova" in html
    assert "Borderline call" in html
    assert "http://app/calls/1" in html
    assert "82" in html
    assert "Borderline" in html


def test_html_includes_also_in_profiles_note():
    html = render_html(_payload(), dashboard_url="http://app/")
    assert "Other Group" in html  # the also_in_profiles list


def test_text_renders_titles_and_urls():
    text = render_text(_payload(), dashboard_url="http://app/")
    assert "Pathology AI" in text
    assert "http://app/calls/2" in text
    assert "Manage subscriptions" in text  # footer


def test_html_renders_deadline_in_stockholm_timezone():
    """Call.deadline is stored UTC. Templates must render in Europe/Stockholm
    (per main.py:122-126 — 'NEVER call .strftime() on a raw datetime in templates')."""
    # 23:30 UTC on Apr 30 = 01:30 Stockholm on May 1 (CEST, +2)
    late_deadline_utc = datetime(2026, 4, 30, 23, 30, tzinfo=UTC)
    entry = DigestCallEntry(
        call_id=1,
        title="Late Deadline",
        summary=None,
        deadline=late_deadline_utc,
        score=80,
        rationale=None,
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
    # Must render Stockholm date (May 1), not UTC date (April 30)
    assert "2026-05-01" in html
    assert "2026-04-30" not in html


def test_html_link_goes_to_in_app_not_external_source():
    """Spec line 429: 'link to dashboard call detail.' Clicking a digest
    entry must land the user on /calls/<id> (where the Hide button lives),
    not the external source URL."""
    entry = DigestCallEntry(
        call_id=42,
        title="Source URL is external",
        summary=None,
        deadline=None,
        score=80,
        rationale=None,
        url="https://vinnova.se/external-call-page",  # the source URL
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
    html = render_html(payload, dashboard_url="http://app.example.com/")
    # In-app link must be present
    assert "http://app.example.com/calls/42" in html
    # External URL should NOT be the primary clickable link — it's the source URL
    # that bypasses the in-app Hide button flow. Verify we're not linking to it.
    assert 'href="https://vinnova.se/external-call-page"' not in html
    # Profile context preserved so call detail can render v2 criterion breakdown.
    assert "?profile=1" in html


def test_text_link_goes_to_in_app():
    """Plain-text digest must also link to in-app detail, not external source."""
    entry = DigestCallEntry(
        call_id=42,
        title="T",
        summary=None,
        deadline=None,
        score=80,
        rationale=None,
        url="https://vinnova.se/x",
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
    text = render_text(payload, dashboard_url="http://app.example.com/")
    assert "http://app.example.com/calls/42" in text


def test_days_until_is_deterministic_with_explicit_now():
    """`_days_until` must use the threaded-in `now`, not datetime.now(UTC),
    so 'in N days' is consistent with deadline filtering and reproducible in
    tests. With now pinned, a deadline exactly 14 days out renders 'in 14 days'
    regardless of wall-clock time."""
    now = datetime(2026, 4, 17, 12, 0, tzinfo=UTC)
    entry = DigestCallEntry(
        call_id=1,
        title="Deadline call",
        summary=None,
        deadline=now + timedelta(days=14),
        score=80,
        rationale=None,
        url="http://app/calls/1",
        bucket="top",
        also_in_profiles=[],
    )
    section = DigestProfileSection(profile_id=1, profile_name="P", entries=[entry])
    payload = DigestPayload(user_id=1, user_email="u@umu.se", sections=[section], total_calls=1)

    html = render_html(payload, dashboard_url="http://app/", now=now)
    text = render_text(payload, dashboard_url="http://app/", now=now)
    assert "in 14 days" in html
    assert "in 14 days" in text


def _payload_with_more(more_count: int):
    p = _payload()
    p.sections[0].more_count = more_count
    return p


def test_html_renders_more_breadcrumb_when_positive():
    html = render_html(_payload_with_more(12), dashboard_url="http://app/")
    assert "12 more strong matches in your dashboard" in html
    assert "http://app/?profile=1&amp;min_score=60" in html


def test_html_omits_more_breadcrumb_when_zero():
    html = render_html(_payload_with_more(0), dashboard_url="http://app/")
    assert "more strong matches" not in html


def test_text_renders_more_breadcrumb_when_positive():
    text = render_text(_payload_with_more(12), dashboard_url="http://app/")
    assert "12 more strong matches in your dashboard" in text
    assert "http://app/?profile=1&min_score=60" in text


def test_html_escapes_xss_in_title():
    """Autoescape must be active for HTML template — user-supplied content is not raw."""
    xss_entry = DigestCallEntry(
        call_id=99,
        title="<script>alert('xss')</script>",
        summary="bad",
        deadline=None,
        score=80,
        rationale=None,
        url="http://app/calls/99",
        bucket="top",
        also_in_profiles=[],
    )
    section = DigestProfileSection(
        profile_id=1,
        profile_name="Group A",
        entries=[xss_entry],
    )
    payload = DigestPayload(
        user_id=1,
        user_email="u@umu.se",
        sections=[section],
        total_calls=1,
    )
    html = render_html(payload, dashboard_url="http://app/")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
