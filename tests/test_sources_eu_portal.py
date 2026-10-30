import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from grant_lab.sources.eu_portal import EUPortalAdapter
from grant_lab.sources.types import AdapterError

# A page captured verbatim from the live SEDIA search API on 2026-09-08, with
# only the two huge HTML fields truncated. Using real bytes rather than a
# hand-written dict is the point: the old adapter read `item["title"]`, and in
# the real payload that key is present and None.
_FIXTURE = json.loads(
    Path(__file__).parent.joinpath("fixtures/eu_portal_search_page.json").read_text()
)

_SINCE = datetime(2026, 1, 1, tzinfo=UTC)

# Pinned "now" for every adapter under test. Without it the expiry filter would
# make these tests pass today and fail next year, which is a worse failure than
# a red one because it arrives without a change.
_NOW = datetime(2026, 9, 8, tzinfo=UTC)

_BRUSSELS = ZoneInfo("Europe/Brussels")


def _brussels_close(year: int, month: int, day: int) -> datetime:
    """17:00 Brussels on that day, as UTC: 15:00 in summer (CEST), 16:00 in winter."""
    return datetime(year, month, day, 17, 0, tzinfo=_BRUSSELS).astimezone(UTC)


def _meta(**overrides) -> dict:
    """One synthetic result, shaped like SEDIA: every metadata value a list."""
    metadata = {
        "identifier": ["HORIZON-HLTH-2026-DISEASE-03-15"],
        "title": ["AI for early disease detection"],
        "url": [
            "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/"
            "opportunities/topic-details/HORIZON-HLTH-2026-DISEASE-03-15"
        ],
        "status": ["31094502"],
        # Midnight UTC, exactly as the live feed encodes every deadline (the
        # captured fixture has 2/2 at T00:00). An earlier T17:00 default here
        # hid the 15-hours-early close for a month (review 2026-10-06 H3).
        "deadlineDate": ["2026-09-15T00:00:00.000+0000"],
        "startDate": ["2026-04-01T00:00:00.000+0000"],
        "descriptionByte": ["<p>Expected Outcome:</p><p>Better diagnostics.</p>"],
        "keywords": ["HORIZON-HLTH-2026-DISEASE-03-15", "health", "diagnostics"],
    }
    metadata.update(overrides)
    return {"url": None, "title": None, "metadata": metadata}


def _body(results, total=None) -> dict:
    return {
        "results": results,
        "totalResults": total if total is not None else len(results),
        "pageSize": 100,
        "pageNumber": 1,
    }


def _mock_transport(status: int = 200, body=None, raise_exc=None, record: list | None = None):
    def handler(request):
        if record is not None:
            record.append(request)
        if raise_exc is not None:
            raise raise_exc
        return httpx.Response(status, json=body if body is not None else _FIXTURE)

    return httpx.MockTransport(handler)


def _adapter(**kwargs) -> EUPortalAdapter:
    kwargs.setdefault("transport", _mock_transport())
    kwargs.setdefault("now", _NOW)
    return EUPortalAdapter(base_url="https://api.tech.ec.europa.eu", **kwargs)


# ---------------------------------------------------------------------------
# The request shape — the two things that make the live API fail
# ---------------------------------------------------------------------------


def test_query_parts_are_typed_as_json():
    """Untyped multipart parts make the live API answer HTTP 500.

    Verified against api.tech.ec.europa.eu on 2026-09-08: the same request with
    the `;type=application/json` suffix removed returns
    `{"type":"throwable","message":"An internal error occurred"}`. This gate
    exists so a "simplification" to `data=` cannot land silently green.
    """
    seen: list[httpx.Request] = []
    list(_adapter(transport=_mock_transport(record=seen)).fetch_new_calls(since=_SINCE))

    assert seen, "no request was issued"
    raw = seen[0].content
    assert seen[0].headers["content-type"].startswith("multipart/form-data")
    for part in (b'name="query"', b'name="languages"'):
        assert part in raw, f"{part!r} missing from the multipart body"
    # One typed part per field, and no more.
    assert raw.count(b"Content-Type: application/json") == 2


def test_query_filters_to_open_and_forthcoming_grant_topics():
    seen: list[httpx.Request] = []
    list(_adapter(transport=_mock_transport(record=seen)).fetch_new_calls(since=_SINCE))

    raw = seen[0].content.decode("utf-8", errors="replace")
    start = raw.index('name="query"')
    query = json.loads(
        raw[raw.index("{", start) : raw.rindex("}", start, raw.index('name="languages"')) + 1]
    )
    terms = {
        key: value for clause in query["bool"]["must"] for key, value in clause["terms"].items()
    }
    # type=1 is grant topics. type=2 (EuropeAid tenders) and type=8 (legacy
    # competitive-calls) carry no `descriptionByte` and stale deadlines.
    assert terms["type"] == ["1"]
    assert sorted(terms["status"]) == ["31094501", "31094502"]


def test_api_key_and_paging_go_in_the_query_string():
    seen: list[httpx.Request] = []
    list(_adapter(transport=_mock_transport(record=seen)).fetch_new_calls(since=_SINCE))

    params = seen[0].url.params
    assert params["apiKey"] == "SEDIA"
    assert params["pageSize"] == "100"
    assert params["pageNumber"] == "1"


# ---------------------------------------------------------------------------
# Normalization, against the captured live page
# ---------------------------------------------------------------------------


def test_fetch_yields_normalized_calls_from_the_live_payload():
    out = list(_adapter().fetch_new_calls(since=_SINCE))

    assert len(out) == 2
    c = out[0]
    assert c.external_id == "HORIZON-MSCA-2027-COFUND-01-01"
    assert c.title == "MSCA COFUND 2027"
    assert c.deadline == _brussels_close(2027, 4, 6)
    assert c.published_at == datetime(2026, 12, 8, tzinfo=UTC)
    assert c.programme == "Horizon Europe"
    assert c.currency == "EUR"
    assert c.url.endswith("/HORIZON-MSCA-2027-COFUND-01-01")


def test_title_is_read_from_metadata_not_the_top_level():
    """The real payload carries a top-level `title` key whose value is None.

    The pre-2026-09-08 parser read that key, so every record was skipped as
    incomplete. Assert the trap is still present in the fixture, so this test
    cannot quietly stop testing anything.
    """
    assert "title" in _FIXTURE["results"][0]
    assert _FIXTURE["results"][0]["title"] is None

    out = list(_adapter().fetch_new_calls(since=_SINCE))
    assert out[0].title == "MSCA COFUND 2027"


def test_offset_without_a_colon_parses():
    """SEDIA emits `+0000`, not `+00:00`.

    Python 3.12 parses that directly, so this is a characterization test: it
    pins the fixture's real format against a future interpreter or a rewrite
    of `_parse_iso` that stops accepting it.
    """
    assert _FIXTURE["results"][0]["metadata"]["deadlineDate"][0].endswith("+0000")
    out = list(_adapter().fetch_new_calls(since=_SINCE))
    assert out[0].deadline == _brussels_close(2027, 4, 6)


# ---------------------------------------------------------------------------
# The body: descriptionByte, no page scraping
# ---------------------------------------------------------------------------


def test_full_text_comes_from_the_search_response_with_no_extra_request():
    """The body arrives with the search hit; there is no topic-detail scrape.

    One request per page, and nothing else. A regression that reintroduced
    per-call scraping would push this count to 1 + len(results).
    """
    seen: list[httpx.Request] = []
    out = list(_adapter(transport=_mock_transport(record=seen)).fetch_new_calls(since=_SINCE))

    assert len(seen) == 1
    assert out[0].full_text is not None
    assert "Expected Outcome" in out[0].full_text


def test_bullet_list_items_survive_the_html_flattening():
    """trafilatura drops `<ul>` bullets; lxml does not.

    Measured on this same fixture text: trafilatura.extract returned 4115 of
    5058 characters, losing every list item — and list items are where the
    expected outcomes live. This gate fails if the flattener is swapped back.
    """
    item = _meta(
        descriptionByte=[
            "<p>Expected Outcome:</p><ul><li>Deeper skills and competences;</li>"
            "<li>Improved employability prospects.</li></ul>"
        ]
    )
    out = list(
        _adapter(transport=_mock_transport(body=_body([item]))).fetch_new_calls(since=_SINCE)
    )

    assert "Deeper skills and competences" in out[0].full_text
    assert "Improved employability prospects" in out[0].full_text


def test_adjacent_blocks_do_not_weld_together():
    item = _meta(descriptionByte=["<p>First sentence.</p><p>Second sentence.</p>"])
    out = list(
        _adapter(transport=_mock_transport(body=_body([item]))).fetch_new_calls(since=_SINCE)
    )

    assert "First sentence.\nSecond sentence." in out[0].full_text
    assert "sentence.Second" not in out[0].full_text


def test_unparseable_description_degrades_to_none():
    item = _meta(descriptionByte=["   "])
    out = list(
        _adapter(transport=_mock_transport(body=_body([item]))).fetch_new_calls(since=_SINCE)
    )

    assert len(out) == 1, "a bad body must not drop the call"
    assert out[0].full_text is None
    assert out[0].summary is None


def test_conditions_boilerplate_is_not_appended_to_the_body():
    """`topicConditions` repeats near-verbatim across calls.

    Folding it into full_text would push thousands of identical characters into
    every call's embedding and make the corpus less separable.
    """
    marker = "General conditions"
    assert marker in _FIXTURE["results"][0]["metadata"]["topicConditions"][0]

    out = list(_adapter().fetch_new_calls(since=_SINCE))
    assert marker not in (out[0].full_text or "")


# ---------------------------------------------------------------------------
# Derived fields
# ---------------------------------------------------------------------------


def test_summary_is_the_opening_of_the_body_and_marks_its_cut():
    long_body = "<p>" + ("Advanced diagnostics research. " * 40) + "</p>"
    item = _meta(descriptionByte=[long_body])
    out = list(
        _adapter(transport=_mock_transport(body=_body([item]))).fetch_new_calls(since=_SINCE)
    )

    summary = out[0].summary
    assert summary.startswith("Advanced diagnostics research.")
    assert summary.endswith("…")
    assert len(summary) <= 501  # 500 chars plus the ellipsis
    assert "\n" not in summary


def test_short_body_becomes_the_summary_verbatim():
    item = _meta(descriptionByte=["<p>Better diagnostics for rare disease.</p>"])
    out = list(
        _adapter(transport=_mock_transport(body=_body([item]))).fetch_new_calls(since=_SINCE)
    )

    assert out[0].summary == "Better diagnostics for rare disease."


def _actions(record_index: int) -> list[dict]:
    raw = _FIXTURE["results"][record_index]["metadata"]["budgetOverview"][0]
    return [
        action for actions in json.loads(raw)["budgetTopicActionMap"].values() for action in actions
    ]


def test_budget_uses_per_grant_contribution_not_the_yearly_envelope():
    """`Call.budget_min/max` are matched against a research profile's own
    budget range — the size of project a group wants to run — so they must be
    the per-grant contribution bounds, not the call's yearly envelope.

    The second fixture record discriminates them: its own action contributes
    8 000 000 per grant while its yearly envelopes run to 24 000 000 and sum to
    far more. Reading the envelope shows up here as a wrong number.
    """
    envelopes = {
        int(amount)
        for action in _actions(1)
        for amount in (action.get("budgetYearMap") or {}).values()
    }
    assert max(envelopes) == 24_000_000, "fixture no longer discriminates the two fields"

    out = list(_adapter().fetch_new_calls(since=_SINCE))
    assert out[1].budget_max == 8_000_000
    assert out[1].budget_max != max(envelopes)
    assert out[1].budget_max != sum(envelopes)


def test_budget_ignores_the_sibling_topics_sharing_the_parent_call():
    """`budgetTopicActionMap` holds every topic of the PARENT CALL.

    Summing the whole map gave HORIZON-CL5-2027-03-D5-05 the range
    2 000 000 - 13 500 000, which belongs to the parent call, not the topic.
    The topic's own action is one of eleven, so this record can tell a
    correctly-scoped read from a whole-map read.
    """
    actions = _actions(1)
    identifier = _FIXTURE["results"][1]["metadata"]["identifier"][0]
    mine = [a for a in actions if str(a["action"]).split(" - ")[0].strip() == identifier]
    assert len(actions) == 11 and len(mine) == 1, "fixture no longer has sibling topics"

    whole_map_min = min(a["minContribution"] for a in actions if a["minContribution"])
    whole_map_max = max(a["maxContribution"] for a in actions if a["maxContribution"])
    assert (whole_map_min, whole_map_max) == (2_000_000, 13_500_000)

    out = list(_adapter().fetch_new_calls(since=_SINCE))
    assert (out[1].budget_min, out[1].budget_max) == (8_000_000, 8_000_000)


def test_zero_contribution_means_unstated_not_zero_euros():
    """The first record's only action states 0/0 — "not published", not free."""
    assert [(a["minContribution"], a["maxContribution"]) for a in _actions(0)] == [(0, 0)]

    out = list(_adapter().fetch_new_calls(since=_SINCE))
    assert out[0].budget_min is None
    assert out[0].budget_max is None


def test_malformed_budget_json_degrades_to_none():
    item = _meta(budgetOverview=["{not json"])
    out = list(
        _adapter(transport=_mock_transport(body=_body([item]))).fetch_new_calls(since=_SINCE)
    )

    assert len(out) == 1
    assert out[0].budget_min is None and out[0].budget_max is None


def test_keywords_drop_the_identifiers_and_keep_subject_terms():
    out = list(
        _adapter(transport=_mock_transport(body=_body([_meta()]))).fetch_new_calls(since=_SINCE)
    )
    assert out[0].keywords == ["health", "diagnostics"]


def test_keywords_are_empty_when_the_payload_lists_only_identifiers():
    # Both captured records carry exactly their own and their parent call's id.
    out = list(_adapter().fetch_new_calls(since=_SINCE))
    assert out[0].keywords == []


def test_programme_falls_back_to_the_raw_prefix_when_unmapped():
    item = _meta(identifier=["NEWPROG-2030-TOPIC-01"])
    out = list(
        _adapter(transport=_mock_transport(body=_body([item]))).fetch_new_calls(since=_SINCE)
    )

    assert out[0].programme == "NEWPROG"


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [("LIFE-2027-X", "LIFE Programme"), ("DIGITAL-2027-X", "Digital Europe Programme")],
)
def test_programme_maps_known_prefixes(identifier, expected):
    item = _meta(identifier=[identifier])
    out = list(
        _adapter(transport=_mock_transport(body=_body([item]))).fetch_new_calls(since=_SINCE)
    )

    assert out[0].programme == expected


# ---------------------------------------------------------------------------
# Skips and failures
# ---------------------------------------------------------------------------


def test_fetch_skips_records_missing_required_fields():
    body = _body([_meta(identifier=[]), _meta(title=[]), _meta()])
    out = list(_adapter(transport=_mock_transport(body=body)).fetch_new_calls(since=_SINCE))

    assert len(out) == 1


def test_fetch_skips_a_record_with_no_metadata_object():
    body = _body([{"url": "https://ec.europa.eu/x", "title": "no metadata here"}, _meta()])
    out = list(_adapter(transport=_mock_transport(body=body)).fetch_new_calls(since=_SINCE))

    assert len(out) == 1
    assert out[0].external_id == "HORIZON-HLTH-2026-DISEASE-03-15"


def test_fetch_skips_records_with_unsafe_url_scheme():
    body = _body([_meta(url=["javascript:alert(1)"]), _meta()])
    out = list(_adapter(transport=_mock_transport(body=body)).fetch_new_calls(since=_SINCE))

    assert len(out) == 1
    assert out[0].url.startswith("https://")


_CANCELLED_COPY = _meta(
    identifier=["HORIZON-NEB-2027-01-REGEN-03-CANCELLED"],
    title=["New European Bauhaus regeneration - CANCELLED"],
)
_ITS_ORIGINAL = _meta(identifier=["HORIZON-NEB-2027-01-REGEN-03"])


@pytest.mark.parametrize(
    "items",
    [[_CANCELLED_COPY, _ITS_ORIGINAL], [_ITS_ORIGINAL, _CANCELLED_COPY], [_CANCELLED_COPY]],
    ids=["copy_first", "original_first", "original_gone_from_the_feed"],
)
def test_a_cancelled_copy_closes_its_original_instead_of_listing_it_twice(items):
    """A withdrawn topic is republished with CANCELLED appended, and SEDIA keeps
    serving it as open NEXT TO its original, which is also open.

    `HORIZON-NEB-2027-01-REGEN-03-CANCELLED` was a live open row on 2026-10-26
    alongside `HORIZON-NEB-2027-01-REGEN-03` (review 2026-10-26 M-core-1), so
    the researcher saw one call twice and the ranker spent on a call nobody can
    apply to. Skipping the copy fixed the double listing but left the original
    open. The copy is the portal's own statement that the original is withdrawn,
    so it now CLOSES the original: one call, under the original identifier,
    closed as of this run, wherever the copy sits in the feed and even after the
    original itself has left it (outside review 2026-10-29, F5). Its own
    deadline is the original's old one, so it is not used.
    """
    out = list(
        _adapter(transport=_mock_transport(body=_body(list(items)))).fetch_new_calls(since=_SINCE)
    )

    assert [(c.external_id, c.closed, c.deadline) for c in out] == [
        ("HORIZON-NEB-2027-01-REGEN-03", True, _NOW)
    ]
    assert out[0].title == "New European Bauhaus regeneration - CANCELLED"


def test_a_cancelled_topic_is_skipped_on_its_title():
    """The other live victim carried the marker in the TITLE as well:
    `HORIZON-EIE-2027-01-CONNECT-01-CANCELLED` / "Startup Europe - CANCELLED".

    The identifier here is given WITHOUT the suffix so this exercises the title
    leg on its own — otherwise the identifier leg would carry the test and the
    title match could rot unnoticed.
    """
    body = _body(
        [
            _meta(
                identifier=["HORIZON-EIE-2027-01-CONNECT-01"],
                title=["Startup Europe - CANCELLED"],
            ),
            _meta(),
        ]
    )
    out = list(_adapter(transport=_mock_transport(body=body)).fetch_new_calls(since=_SINCE))

    assert [c.external_id for c in out] == ["HORIZON-HLTH-2026-DISEASE-03-15"]


def test_the_two_cancelled_legs_log_at_different_levels(caplog):
    """A title-only match is the only CANCELLED decision that can be WRONG, so
    it has to be findable in the log without reading the code.

    Review 2026-10-26's reviewer found the title leg skips a legitimate title
    whose last word is a shouted uppercase CANCELLED after a separator. The
    identifier leg cannot be wrong, and it is the leg that catches both live
    victims. So: title-only -> WARNING, identifier -> INFO, and `matched_on`
    says which fired.
    """
    body = _body(
        [
            # identifier leg: unambiguous
            _meta(identifier=["HORIZON-NEB-2027-01-REGEN-03-CANCELLED"]),
            # title leg alone: the identifier is clean
            _meta(
                identifier=["HORIZON-EIE-2027-01-CONNECT-01"],
                title=["Startup Europe - CANCELLED"],
            ),
        ]
    )
    with caplog.at_level(logging.INFO, logger="grant_lab.sources.eu_portal"):
        out = list(_adapter(transport=_mock_transport(body=body)).fetch_new_calls(since=_SINCE))

    # The identifier leg now CLOSES its original (F5); the title leg cannot
    # name an original, so it still only skips.
    assert [(c.external_id, c.closed) for c in out] == [("HORIZON-NEB-2027-01-REGEN-03", True)]
    events = {
        (r.__dict__.get("event"), r.__dict__.get("matched_on")): r.levelno
        for r in caplog.records
        if r.__dict__.get("event") in ("skip_cancelled", "close_cancelled")
    }
    assert events == {
        ("close_cancelled", "identifier"): logging.INFO,
        ("skip_cancelled", "title"): logging.WARNING,
    }, events


@pytest.mark.parametrize(
    "title",
    [
        # The word is ordinary subject matter. Upper case but not at the end:
        "Reducing CANCELLED surgery through smarter scheduling",
        # At the end but lower case, which is how prose writes it:
        "Why elective treatment gets cancelled",
        # Ends in those letters but is a different word:
        "Keeping appointments UNCANCELLED",
    ],
)
def test_a_call_whose_title_merely_mentions_cancellation_is_kept(title):
    """The skip must not eat a legitimate call. It matches only the uppercase
    token CANCELLED, only at the END, and only after a separator."""
    body = _body([_meta(title=[title])])
    out = list(_adapter(transport=_mock_transport(body=body)).fetch_new_calls(since=_SINCE))

    assert [c.title for c in out] == [title]


def test_a_non_dict_result_element_is_skipped_with_a_warning_and_the_run_continues(caplog):
    """One junk element used to abort the whole run.

    `_normalize` called `item.get("metadata")` with no `isinstance` guard, so a
    string, a null or a number in `results` raised `AttributeError`, the
    orchestrator's broad `except` turned it into a failed source, and every
    good hit on the page was discarded (review 2026-10-26, Low / A3).
    """
    body = _body(["just a string", None, 42, _meta()], total=4)
    with caplog.at_level(logging.WARNING, logger="grant_lab.sources.eu_portal"):
        out = list(_adapter(transport=_mock_transport(body=body)).fetch_new_calls(since=_SINCE))

    # The good hit still arrives: "nothing raised" alone would also be true of
    # a run that returned nothing.
    assert [c.external_id for c in out] == ["HORIZON-HLTH-2026-DISEASE-03-15"]
    junk = [r for r in caplog.records if r.__dict__.get("event") == "skip_non_dict_result"]
    assert len(junk) == 3, [r.getMessage() for r in caplog.records]
    assert {r.levelno for r in junk} == {logging.WARNING}


def test_http_error_raises_adapter_error():
    adapter = _adapter(transport=_mock_transport(status=500, body={"error": "boom"}))
    with pytest.raises(AdapterError):
        list(adapter.fetch_new_calls(since=_SINCE))


def test_network_failure_raises_adapter_error():
    adapter = _adapter(transport=_mock_transport(raise_exc=httpx.ConnectError("no route")))
    with pytest.raises(AdapterError):
        list(adapter.fetch_new_calls(since=_SINCE))


def test_non_json_body_raises_adapter_error():
    def handler(request):
        return httpx.Response(200, text="<html>gateway</html>")

    adapter = _adapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterError):
        list(adapter.fetch_new_calls(since=_SINCE))


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def _paging_transport(pages: list[list[dict]], total: int, record: list | None = None):
    def handler(request):
        if record is not None:
            record.append(request)
        page_number = int(request.url.params["pageNumber"])
        results = pages[page_number - 1] if page_number <= len(pages) else []
        return httpx.Response(200, json={"results": results, "totalResults": total})

    return httpx.MockTransport(handler)


def test_fetch_paginates_until_totalresults_is_reached():
    full_page = [_meta(identifier=[f"HORIZON-P1-{i}"]) for i in range(100)]
    second = [_meta(identifier=[f"HORIZON-P2-{i}"]) for i in range(5)]
    seen: list[httpx.Request] = []
    adapter = _adapter(transport=_paging_transport([full_page, second], total=105, record=seen))

    out = list(adapter.fetch_new_calls(since=_SINCE))

    assert len(out) == 105
    assert [int(r.url.params["pageNumber"]) for r in seen] == [1, 2]


def test_fetch_stops_on_a_short_page_even_if_total_disagrees():
    """A server that overstates totalResults must not make us spin."""
    seen: list[httpx.Request] = []
    adapter = _adapter(transport=_paging_transport([[_meta()]], total=9999, record=seen))

    out = list(adapter.fetch_new_calls(since=_SINCE))

    assert len(out) == 1
    assert len(seen) == 1


def test_fetch_stops_on_an_empty_page():
    full_page = [_meta(identifier=[f"HORIZON-P1-{i}"]) for i in range(100)]
    seen: list[httpx.Request] = []
    adapter = _adapter(transport=_paging_transport([full_page, []], total=9999, record=seen))

    out = list(adapter.fetch_new_calls(since=_SINCE))

    assert len(out) == 100
    assert [int(r.url.params["pageNumber"]) for r in seen] == [1, 2]


def _overlapping_pages() -> tuple[list[list[dict]], int]:
    """The A3 probe: page 2 repeats the last five hits of page 1.

    SEDIA's search API is paged with no stable sort, so a hit can move between
    pages between two requests of the same run. Live on 2026-10-26 one run
    logged `inserted 0, updated 419` while only 375 distinct rows carried that
    day's `fetched_at`: 44 identifiers arrived twice and 44 were never seen.

    Here 100 + 50 hits with a 5-hit overlap are 150 yields over 145 distinct
    identifiers, and the feed claims 150.
    """
    page1 = [_meta(identifier=[f"HORIZON-SEDIA-{i:03d}"]) for i in range(100)]
    page2 = [_meta(identifier=[f"HORIZON-SEDIA-{i:03d}"]) for i in range(95, 145)]
    return [page1, page2], 150


def test_paged_results_are_deduplicated_by_identifier():
    """Yielding the same call twice re-embeds it and re-upserts it for nothing.

    Dedup does NOT recover the hits that fell between the shifting pages — see
    `test_a_run_that_saw_less_than_the_feed_claims_warns_with_both_numbers`,
    which is what makes that loss visible.
    """
    pages, total = _overlapping_pages()
    out = list(
        _adapter(transport=_paging_transport(pages, total=total)).fetch_new_calls(since=_SINCE)
    )

    ids = [c.external_id for c in out]
    assert len(ids) == 145, f"{len(ids)} yields over {len(set(ids))} distinct identifiers"
    assert len(set(ids)) == len(ids)


def test_a_skipped_first_copy_does_not_suppress_a_good_second_copy():
    """The dedup key is what was YIELDED, not what was seen.

    Keyed on "seen", a first copy that `_normalize` drops — expired round,
    unsafe URL, missing field — would claim the identifier and silence the good
    copy behind it, turning a skip into a lost call. Keyed on "yielded", the
    second copy still gets its chance.
    """
    # A full page, so paging really continues to page 2; the last hit on page 1
    # is a closed-round copy of the identifier page 2 then repeats as open.
    page1 = [_meta(identifier=[f"HORIZON-FILL-{i:03d}"]) for i in range(99)]
    page1.append(_meta(deadlineDate=["2026-03-01T00:00:00.000+0000"]))
    page2 = [_meta()]
    out = list(
        _adapter(transport=_paging_transport([page1, page2], total=101)).fetch_new_calls(
            since=_SINCE
        )
    )

    survivors = [c for c in out if c.external_id == "HORIZON-HLTH-2026-DISEASE-03-15"]
    assert len(survivors) == 1, [c.external_id for c in out[-3:]]
    assert survivors[0].deadline == _brussels_close(2026, 9, 15)
    assert len(out) == 100


def test_a_run_that_saw_less_than_the_feed_claims_warns_with_both_numbers(caplog):
    """Dedup makes the output honest; this warning is what makes the residual
    loss visible. Without it, a run that silently missed 44 of 419 calls looks
    exactly like a complete one."""
    pages, total = _overlapping_pages()
    adapter = _adapter(transport=_paging_transport(pages, total=total))

    with caplog.at_level(logging.WARNING, logger="grant_lab.sources.eu_portal"):
        out = list(adapter.fetch_new_calls(since=_SINCE))

    assert len(out) == 145
    records = [r for r in caplog.records if r.__dict__.get("event") == "paging_incomplete"]
    assert records, [r.getMessage() for r in caplog.records]
    assert records[0].levelno == logging.WARNING
    # Both numbers, so an operator can see the size of the gap. The JSON
    # formatter surfaces every `extra` key as a top-level log field.
    assert records[0].__dict__["distinct"] == 145
    assert records[0].__dict__["total_results"] == 150


def test_a_complete_run_does_not_warn(caplog):
    """The warning must be a signal, not a fixture of every run: the 03:00 tick
    would otherwise cry wolf nightly and nobody would read it."""
    page1 = [_meta(identifier=[f"HORIZON-P1-{i}"]) for i in range(100)]
    page2 = [_meta(identifier=[f"HORIZON-P2-{i}"]) for i in range(5)]
    adapter = _adapter(transport=_paging_transport([page1, page2], total=105))

    with caplog.at_level(logging.WARNING, logger="grant_lab.sources.eu_portal"):
        out = list(adapter.fetch_new_calls(since=_SINCE))

    assert len(out) == 105
    complaints = [r for r in caplog.records if r.__dict__.get("event") == "paging_incomplete"]
    assert complaints == [], [r.__dict__ for r in complaints]


def test_needs_body_is_accepted_and_costs_no_extra_request():
    """The protocol still passes `needs_body`; this adapter has no second
    request to skip, so it must neither error nor change the request count."""
    seen: list[httpx.Request] = []
    adapter = _adapter(transport=_mock_transport(record=seen))

    out = list(adapter.fetch_new_calls(since=_SINCE, needs_body=lambda _id: False))

    assert len(out) == 2
    assert len(seen) == 1
    assert out[0].full_text is not None


# ---------------------------------------------------------------------------
# Deadlines and expiry
# ---------------------------------------------------------------------------


def test_a_call_whose_every_round_has_closed_is_passed_on_as_closed():
    """590 of 1214 live topics are in this state despite the API reporting them
    open or forthcoming, so the deadline is the only trustworthy signal.

    They used to be dropped here, which also dropped the one fact we need from
    them: when a call we already hold as open has moved its deadline into the
    past (outside review 2026-10-29, F5). It is now passed on with closed=True
    and its last round's real deadline; the orchestrator only ever uses that to
    close a row it holds, never to create one."""
    body = _body(
        [
            _meta(identifier=["HORIZON-OLD-1"], deadlineDate=["2026-03-01T00:00:00.000+0000"]),
            _meta(),
        ]
    )
    out = list(_adapter(transport=_mock_transport(body=body)).fetch_new_calls(since=_SINCE))

    assert [(c.external_id, c.closed) for c in out] == [
        ("HORIZON-HLTH-2026-DISEASE-03-15", False),
        ("HORIZON-OLD-1", True),
    ]
    assert out[0].deadline == _brussels_close(2026, 9, 15)
    assert out[1].deadline == _brussels_close(2026, 3, 1)


def test_an_open_copy_wins_over_a_closed_copy_of_the_same_call():
    """One identifier can arrive twice across shifting pages: once as a closed
    old round, once open. Whichever comes first, the open copy is the call, and
    the closed one must not close it."""
    closed_copy = _meta(deadlineDate=["2026-03-01T00:00:00.000+0000"])
    for items in ([closed_copy, _meta()], [_meta(), closed_copy]):
        out = list(
            _adapter(transport=_mock_transport(body=_body(items))).fetch_new_calls(since=_SINCE)
        )
        assert [(c.external_id, c.closed) for c in out] == [
            ("HORIZON-HLTH-2026-DISEASE-03-15", False)
        ], items


def test_a_call_with_a_later_open_round_survives_a_closed_early_round():
    """The bug this catches: reading only entry [0] reports the FIRST round.

    PERI-2026-ANTI-COUNTERFEIT is live with rounds 2026-04-21 and 2026-09-15;
    on entry [0] alone it looks expired and would be thrown away.
    """
    item = _meta(deadlineDate=["2026-04-21T00:00:00.000+0000", "2026-09-15T00:00:00.000+0000"])
    out = list(
        _adapter(transport=_mock_transport(body=_body([item]))).fetch_new_calls(since=_SINCE)
    )

    assert len(out) == 1, "a call with an open later round must not be dropped"
    assert out[0].deadline == _brussels_close(2026, 9, 15)


def test_the_earliest_open_round_is_the_one_reported():
    """Not the last, and not the first — the one an applicant is working to."""
    item = _meta(
        deadlineDate=[
            "2026-01-10T00:00:00.000+0000",
            "2026-11-30T00:00:00.000+0000",
            "2027-11-30T00:00:00.000+0000",
        ]
    )
    out = list(
        _adapter(transport=_mock_transport(body=_body([item]))).fetch_new_calls(since=_SINCE)
    )

    assert out[0].deadline == _brussels_close(2026, 11, 30)


def test_a_call_with_no_deadline_at_all_is_kept():
    """ "No deadline stated" is not "expired"; dropping these would lose calls
    the ranker can still judge, and `_timeline` already passes them."""
    out = list(
        _adapter(transport=_mock_transport(body=_body([_meta(deadlineDate=[])]))).fetch_new_calls(
            since=_SINCE
        )
    )

    assert len(out) == 1
    assert out[0].deadline is None


def test_expiry_is_judged_against_the_injected_clock():
    """Same payload, two instants, opposite outcomes — so the filter is really
    reading the clock and not some property of the fixture."""
    body = _body([_meta(deadlineDate=["2026-09-15T00:00:00.000+0000"])])

    before = list(
        _adapter(
            transport=_mock_transport(body=body), now=datetime(2026, 9, 8, tzinfo=UTC)
        ).fetch_new_calls(since=_SINCE)
    )
    after = list(
        _adapter(
            transport=_mock_transport(body=body), now=datetime(2026, 9, 20, tzinfo=UTC)
        ).fetch_new_calls(since=_SINCE)
    )

    # Expired is now passed on as CLOSED rather than dropped (F5), so the two
    # instants differ in the flag, not in whether a call comes back at all.
    assert [c.closed for c in before] == [False]
    assert [c.closed for c in after] == [True]


# ---------------------------------------------------------------------------
# The deadline's time of day (review 2026-10-06 H3)
# ---------------------------------------------------------------------------


def test_the_live_feeds_midnight_deadline_is_read_as_the_1700_brussels_close():
    """SEDIA encodes a deadline DATE as midnight UTC; EU calls close at 17:00
    Brussels on that date. Stored unchanged, every EU call left the feed at
    02:00 Stockholm on its last day — 670 live rows on 2026-10-06."""
    raw = [r["metadata"]["deadlineDate"][0] for r in _FIXTURE["results"]]
    assert raw and all("T00:00:00.000+0000" in v for v in raw), raw

    out = list(_adapter().fetch_new_calls(since=_SINCE))

    # 2027-04-06 and -14 are CEST, so 17:00 Brussels is 15:00 UTC.
    assert [c.deadline for c in out] == [
        datetime(2027, 4, 6, 15, 0, tzinfo=UTC),
        datetime(2027, 4, 14, 15, 0, tzinfo=UTC),
    ]


def test_a_winter_deadline_closes_at_1600_utc():
    """CET, not CEST: a fixed +15 h bump would be an hour late in winter."""
    item = _meta(deadlineDate=["2026-11-30T00:00:00.000+0000"])
    out = list(
        _adapter(transport=_mock_transport(body=_body([item]))).fetch_new_calls(since=_SINCE)
    )

    assert out[0].deadline == datetime(2026, 11, 30, 16, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("now", "kept"),
    [
        # 10:00 UTC on the deadline day: the old code had dropped it 10 h ago.
        (datetime(2026, 9, 15, 10, 0, tzinfo=UTC), True),
        (datetime(2026, 9, 15, 14, 59, 59, tzinfo=UTC), True),
        # 17:00 Brussels (CEST) itself: `deadline <= now` closes it.
        (datetime(2026, 9, 15, 15, 0, tzinfo=UTC), False),
    ],
)
def test_a_call_stays_open_until_1700_brussels_on_its_deadline_day(now, kept):
    body = _body([_meta(deadlineDate=["2026-09-15T00:00:00.000+0000"])])
    out = list(
        _adapter(transport=_mock_transport(body=body), now=now).fetch_new_calls(since=_SINCE)
    )

    # "Kept" means passed on as an OPEN call; past 17:00 it comes back CLOSED (F5).
    assert [c.closed for c in out] == [not kept]


def test_a_deadline_that_states_a_time_is_left_alone():
    """Only an exact midnight is a bare date. 17:00 UTC is a real instant (one
    such live row exists) and must not be moved."""
    item = _meta(deadlineDate=["2026-09-15T17:00:00.000+0000"])
    out = list(
        _adapter(transport=_mock_transport(body=_body([item]))).fetch_new_calls(since=_SINCE)
    )

    assert out[0].deadline == datetime(2026, 9, 15, 17, 0, tzinfo=UTC)


def test_the_round_picker_sees_the_bumped_deadline():
    """A round closing today must still be the one reported this morning — the
    bump has to happen before `_pick_deadline`, not after it."""
    item = _meta(deadlineDate=["2026-09-15T00:00:00.000+0000", "2026-11-30T00:00:00.000+0000"])
    out = list(
        _adapter(
            transport=_mock_transport(body=_body([item])),
            now=datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
        ).fetch_new_calls(since=_SINCE)
    )

    assert out[0].deadline == _brussels_close(2026, 9, 15)
