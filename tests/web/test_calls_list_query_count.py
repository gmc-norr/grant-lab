"""Gates for the fields the match card reads off `GET /api/calls` (Task 4, spec §5).

The card needs the call's own columns, the per-profile criterion chips, the
applicant-note count and the escalation flag. The chips are the dangerous part:
the obvious implementation reads `call.rankings[i].criteria` inside the response
comprehension, which is one lazy SELECT per row -- invisible in a one-row fixture
and 50 round trips on a full page.

So the load-bearing gate here is a STATEMENT COUNT compared across two page
sizes, not a ceiling: `assert N10 < 60` passes a per-row loop on a small page,
which is the "weaker property than the one claimed" hollow-gate shape.

Every request that measures the criteria path sends `?profile=`. The batched
query is guarded by `if profile_id is not None`, so a request without one skips
the block in BOTH runs and the comparison would hold over code it never ran.
`test_the_gate_would_be_vacuous_without_a_profile` pins that precondition.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from grant_lab.db.models import (
    Call,
    CallDecision,
    CallRanking,
    CallRankingCriterion,
    Profile,
    ProfileKind,
    Source,
    User,
)
from grant_lab.main import create_app


@pytest.fixture()
def make_app(monkeypatch, pg_url):
    """`create_app()` builds its OWN Engine, separate from `pg_session_factory`'s.

    That separation is what makes the statement counter below honest -- seeding
    never lands in the count -- but nothing disposes those engines, and a pooled
    psycopg connection collected by the GC raises ResourceWarning from
    `__del__`. `pyproject.toml` sets `filterwarnings = ["error::ResourceWarning"]`,
    so leaving them to the collector puts an unraisable warning of the exact
    class the suite treats as fatal on whichever test happens to trigger a GC.
    Dispose them here instead.
    """
    engines = []

    def _make():
        monkeypatch.setenv("DATABASE_URL", pg_url)
        monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
        monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
        monkeypatch.setenv("APP_BASE_URL", "http://testserver")
        app = create_app()
        engines.append(app.state.engine)
        return app

    yield _make
    for engine in engines:
        engine.dispose()


def _client(app, uid):
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c


def _seed_user_and_profiles(pg_session_factory, *, n_profiles: int = 1):
    with pg_session_factory() as s:
        u = User(status="active", email="carder@umu.se")
        s.add(u)
        s.flush()
        pids = []
        for i in range(n_profiles):
            p = Profile(
                name=f"P{i + 1}",
                kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
                level="group",
                description="d",
                structured={},
                created_by=u.id,
            )
            s.add(p)
            s.flush()
            pids.append(p.id)
        s.commit()
        return u.id, pids


def _add_call(
    pg_session_factory,
    *,
    tag: str,
    profile_id: int | None = None,
    applicant_notes: list[dict] | None = None,
    escalated: bool = False,
    criteria: tuple[str, ...] = (),
    programme: str | None = None,
    budget_min: int | None = None,
    budget_max: int | None = None,
    currency: str | None = None,
    summary: str | None = None,
    keywords: tuple[str, ...] = (),
) -> int:
    """One call, optionally ranked for `profile_id` with `criteria` chips.

    `profile_id=None` seeds an UNRANKED call -- the `(Call, None)` tuple the
    dashboard's outer join produces for a newly ingested call, which is the
    default state of the list and not an edge case.
    """
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id=f"CARD-{tag}",
            url=f"https://example.test/card/{tag}",
            title=f"Card call {tag}",
            published_at=datetime(2026, 4, 1, tzinfo=UTC),
            deadline=None,
            programme=programme,
            budget_min=budget_min,
            budget_max=budget_max,
            currency=currency,
            summary=summary,
            keywords=list(keywords),
        )
        s.add(c)
        s.flush()
        if profile_id is not None:
            s.add(
                CallRanking(
                    call_id=c.id,
                    profile_id=profile_id,
                    aggregate_score=70,
                    model="test-model",
                    prompt_version="v1",
                    applicant_notes=applicant_notes,
                    escalated=escalated,
                )
            )
            for name in criteria:
                s.add(
                    CallRankingCriterion(
                        call_id=c.id,
                        profile_id=profile_id,
                        criterion=name,
                        rating="pass",
                        rationale=f"{name} because",
                        quoted_evidence=f"quote for {name}",
                    )
                )
        s.commit()
        return c.id


def _decide(pg_session_factory, *, call_id: int, user_id: int, profile_id: int, state: str):
    """One decision row, WITH a note.

    Without the note the saved/hidden gates below would compare two runs over
    rows that have nothing to project -- the column would be selected and come
    back NULL every time, which is the same statement count a real N+1 on an
    empty column would also produce. Seeding a note is what makes the measured
    query the one the user actually gets.
    """
    with pg_session_factory() as s:
        s.add(
            CallDecision(
                user_id=user_id,
                profile_id=profile_id,
                call_id=call_id,
                state=state,
                note=f"reason for {call_id}",
                decided_by=user_id,
            )
        )
        s.commit()


@contextmanager
def _statement_counter(app):
    """Count every cursor execute on the engine the app actually uses.

    The seeding fixtures run on `pg_session_factory`'s engine (a different
    Engine on the same database), so only request traffic lands in this box.
    """
    engine = app.state.engine
    box = {"n": 0}

    def _on_exec(conn, cursor, statement, parameters, context, executemany):
        box["n"] += 1

    event.listen(engine, "before_cursor_execute", _on_exec)
    try:
        yield box
    finally:
        event.remove(engine, "before_cursor_execute", _on_exec)


def _measure(client, app, url: str) -> tuple[int, dict]:
    with _statement_counter(app) as box:
        r = client.get(url)
    assert r.status_code == 200, r.text
    return box["n"], r.json()


def _by_title(body) -> dict[str, dict]:
    return {i["title"]: i for i in body["items"]}


# ---------------------------------------------------------------------------
# The no-N+1 gate. Equality against the one-call run, not a ceiling.
# ---------------------------------------------------------------------------
def test_the_list_costs_the_same_number_of_statements_at_1_and_10_calls(
    make_app, pg_session_factory
):
    uid, (pid,) = _seed_user_and_profiles(pg_session_factory)
    app = make_app()
    c = _client(app, uid)
    url = f"/api/calls?profile={pid}"

    # The first call on a fresh Engine pays for dialect initialization and pool
    # warm-up, which are cursor executes too. Burn them before measuring, or N1
    # carries a constant N10 does not and the equality below is not the property
    # under test.
    _add_call(pg_session_factory, tag="unranked", profile_id=None)
    c.get(url)

    n1, body1 = _measure(c, app, url)
    assert len(body1["items"]) == 1

    for i in range(9):
        _add_call(
            pg_session_factory,
            tag=f"r{i}",
            profile_id=pid,
            criteria=("topic_fit", "budget", "eligibility"),
        )
    n10, body10 = _measure(c, app, url)
    assert len(body10["items"]) == 10

    # The criteria path really ran -- otherwise this compares two runs of code
    # that never touched a criterion row.
    ranked = _by_title(body10)["Card call r0"]
    assert [x["criterion"] for x in sorted(ranked["criteria"], key=lambda x: x["criterion"])] == [
        "budget",
        "eligibility",
        "topic_fit",
    ]

    assert n10 == n1, f"page cost scales with row count: N1={n1} N10={n10}"


def _decision_view_costs_the_same_at_1_and_10(make_app, pg_session_factory, *, state: str):
    """The 1-vs-10 body of the test above, over a DECISION view.

    Identical shape on purpose: same warm-up burn, same equality (not a
    ceiling), same proof that the criteria path really ran. The only changes
    are the `view` parameter and a decision row -- with a note -- for every
    seeded call, without which the view returns zero rows and the comparison
    measures an empty page.
    """
    uid, (pid,) = _seed_user_and_profiles(pg_session_factory)
    app = make_app()
    c = _client(app, uid)
    url = f"/api/calls?profile={pid}&view={state}"

    # The first call on a fresh Engine pays for dialect initialization and pool
    # warm-up, which are cursor executes too. Burn them before measuring, or N1
    # carries a constant N10 does not and the equality below is not the property
    # under test.
    first = _add_call(pg_session_factory, tag="unranked", profile_id=None)
    _decide(pg_session_factory, call_id=first, user_id=uid, profile_id=pid, state=state)
    c.get(url)

    n1, body1 = _measure(c, app, url)
    assert len(body1["items"]) == 1
    # The note is really on the wire: a view returning rows with a NULL third
    # column would satisfy the count equality while projecting nothing.
    assert body1["items"][0]["decision_note"] == f"reason for {first}"

    for i in range(9):
        cid = _add_call(
            pg_session_factory,
            tag=f"r{i}",
            profile_id=pid,
            criteria=("topic_fit", "budget", "eligibility"),
        )
        _decide(pg_session_factory, call_id=cid, user_id=uid, profile_id=pid, state=state)
    n10, body10 = _measure(c, app, url)
    assert len(body10["items"]) == 10

    # The criteria path really ran -- otherwise this compares two runs of code
    # that never touched a criterion row.
    ranked = _by_title(body10)["Card call r0"]
    assert [x["criterion"] for x in sorted(ranked["criteria"], key=lambda x: x["criterion"])] == [
        "budget",
        "eligibility",
        "topic_fit",
    ]
    assert all(i["decision_note"] for i in body10["items"])

    assert n10 == n1, f"page cost scales with row count: N1={n1} N10={n10}"


def test_the_hidden_view_costs_the_same_number_of_statements_at_1_and_10_calls(
    make_app, pg_session_factory
):
    """G8. Per-row growth is what an N+1 looks like; comparing `hidden` against
    `inbox` compares two DIFFERENT queries and would miss it entirely."""
    _decision_view_costs_the_same_at_1_and_10(make_app, pg_session_factory, state="hidden")


def test_the_saved_view_costs_the_same_number_of_statements_at_1_and_10_calls(
    make_app, pg_session_factory
):
    """G8, the other decision view. `saved` and `hidden` take the same branch in
    build_dashboard_view, but the branch is keyed on `view` and only a test per
    view proves that neither name fell out of it."""
    _decision_view_costs_the_same_at_1_and_10(make_app, pg_session_factory, state="saved")


def test_the_gate_would_be_vacuous_without_a_profile(make_app, pg_session_factory):
    """Without `?profile=`, the criteria block is skipped -- so a count
    comparison over that URL is a comparison of code that never runs.

    This is not a second no-N+1 gate; it is the reason the gate above sends a
    profile. It asserts the SKIP positively (no chips at all), so the
    precondition is a fact in the suite rather than a comment.
    """
    uid, (pid,) = _seed_user_and_profiles(pg_session_factory)
    app = make_app()
    c = _client(app, uid)
    _add_call(
        pg_session_factory,
        tag="np",
        profile_id=pid,
        criteria=("topic_fit", "budget"),
    )
    c.get("/api/calls")
    _, body = _measure(c, app, "/api/calls")
    assert len(body["items"]) == 1
    assert body["items"][0]["criteria"] == []
    # ...and the very same row DOES have chips once a profile is selected.
    _, ranked = _measure(c, app, f"/api/calls?profile={pid}")
    assert len(ranked["items"][0]["criteria"]) == 2


@contextmanager
def _criteria_rowcounts(app):
    """Record `cursor.rowcount` for every statement that reads the criteria table.

    The statement-count gate above is blind to an unbounded read: dropping
    `CallRankingCriterion.call_id.in_(call_ids)` is still exactly ONE statement,
    `criteria_by_call` is keyed by `call_id` so every off-page row is silently
    discarded on the way in, and the response body is byte-identical. The only
    observable difference is how many rows crossed the wire -- so that is what
    this measures (F-12 #3).
    """
    engine = app.state.engine
    rows: list[int] = []

    def _on_exec(conn, cursor, statement, parameters, context, executemany):
        if "call_ranking_criteria" in statement:
            rows.append(cursor.rowcount)

    event.listen(engine, "after_cursor_execute", _on_exec)
    try:
        yield rows
    finally:
        event.remove(engine, "after_cursor_execute", _on_exec)


def test_the_criteria_query_reads_only_the_page(make_app, pg_session_factory):
    """Ten ranked calls, one criterion each, a two-row page: two criterion rows.

    Deliberately NOT a statement count -- see `_criteria_rowcounts`. On this
    fixture an unbounded query returns 10 rows for a page of 2; on the live
    table it would be the profile's entire criteria set on every keystroke.
    """
    uid, (pid,) = _seed_user_and_profiles(pg_session_factory)
    for i in range(10):
        _add_call(pg_session_factory, tag=f"page{i}", profile_id=pid, criteria=("budget",))
    app = make_app()
    c = _client(app, uid)
    url = f"/api/calls?profile={pid}&limit=2"
    c.get(url)  # burn dialect init / pool warm-up, same reason as the count gate

    with _criteria_rowcounts(app) as rows:
        r = c.get(url)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 2
    assert body["pagination"]["total"] == 10
    # The chips really arrived -- otherwise `rows == [2]` could be satisfied by
    # a query that read two rows and threw them away.
    assert [x["criterion"] for x in body["items"][0]["criteria"]] == ["budget"]
    assert rows == [2], (
        f"the criteria statement returned {rows} row(s) for a 2-row page over 10 ranked calls"
    )


# ---------------------------------------------------------------------------
# Chips are per-profile. Two profiles rank the SAME call differently.
# ---------------------------------------------------------------------------
def test_chips_come_from_the_selected_profile_only(make_app, pg_session_factory):
    uid, (p1, p2) = _seed_user_and_profiles(pg_session_factory, n_profiles=2)
    cid = _add_call(
        pg_session_factory,
        tag="shared",
        profile_id=p1,
        criteria=("topic_fit", "budget"),
    )
    # P2 ranks the SAME call with a different criterion set and different text.
    with pg_session_factory() as s:
        s.add(
            CallRanking(
                call_id=cid,
                profile_id=p2,
                aggregate_score=10,
                model="test-model",
                prompt_version="v1",
            )
        )
        s.add(
            CallRankingCriterion(
                call_id=cid,
                profile_id=p2,
                criterion="trl",
                rating="fail",
                rationale="p2 only",
                quoted_evidence="p2 evidence",
            )
        )
        s.commit()

    app = make_app()
    c = _client(app, uid)

    _, b1 = _measure(c, app, f"/api/calls?profile={p1}")
    chips1 = b1["items"][0]["criteria"]
    assert sorted(x["criterion"] for x in chips1) == ["budget", "topic_fit"]
    assert all(x["rationale"] != "p2 only" for x in chips1)

    _, b2 = _measure(c, app, f"/api/calls?profile={p2}")
    chips2 = b2["items"][0]["criteria"]
    assert [x["criterion"] for x in chips2] == ["trl"]
    assert chips2[0]["rationale"] == "p2 only"
    assert chips2[0]["quoted_evidence"] == "p2 evidence"


def test_chip_labels_and_flags_match_the_detail_page(make_app, pg_session_factory):
    """One source of truth for label / is_topic_fit / is_reason_code.

    Compared field-by-field against `GET /api/calls/{id}`'s criteria, so a
    second copy of the derivation in the list drifts loudly instead of quietly
    calling "Budget" something else on the card than on the page.
    """
    uid, (pid,) = _seed_user_and_profiles(pg_session_factory)
    cid = _add_call(
        pg_session_factory,
        tag="labels",
        profile_id=pid,
        criteria=(
            "topic_fit",
            "budget",
            "eligibility",
            "timeline",
            "trl",
            "exclusions",
            # `embedding` is seeded so ALL FOUR members of _REASON_CODE_CRITERIA
            # are pinned below. Measured: without this row, dropping "embedding"
            # from the tuple left every test in this file green.
            "embedding",
        ),
    )
    app = make_app()
    c = _client(app, uid)

    _, body = _measure(c, app, f"/api/calls?profile={pid}")
    listed = {x["criterion"]: x for x in body["items"][0]["criteria"]}

    detail = c.get(f"/api/calls/{cid}?profile={pid}")
    assert detail.status_code == 200, detail.text
    detailed = {x["criterion"]: x for x in detail.json()["criteria"]}

    assert (
        set(listed)
        == set(detailed)
        == {
            "topic_fit",
            "budget",
            "eligibility",
            "timeline",
            "trl",
            "exclusions",
            "embedding",
        }
    )
    for name, d in detailed.items():
        shared = {k: v for k, v in d.items() if k != "detail"}
        assert listed[name] == shared, name
    # And the derivations are not all-False/all-True constants.
    assert listed["topic_fit"]["is_topic_fit"] is True
    assert listed["budget"]["is_topic_fit"] is False
    assert listed["budget"]["is_reason_code"] is True
    assert listed["eligibility"]["is_reason_code"] is False
    assert listed["budget"]["label"] == "Budget"
    assert listed["trl"]["label"] == "Technology Readiness Level"
    # _REASON_CODE_CRITERIA membership, pinned member-by-member (not just the
    # two above) so dropping any one of them from the tuple fails loudly.
    assert listed["timeline"]["is_reason_code"] is True
    assert listed["trl"]["is_reason_code"] is True
    assert listed["embedding"]["is_reason_code"] is True
    assert listed["embedding"]["label"] == "Topic similarity"
    assert listed["topic_fit"]["is_reason_code"] is False
    assert listed["eligibility"]["is_reason_code"] is False
    assert listed["exclusions"]["is_reason_code"] is False


def _force_seq_scan(engine):
    """Defeat `call_ranking_criteria`'s covering PK index for this engine.

    The PK is `(call_id, profile_id, criterion)`. Both criteria queries filter
    by equality on the first two columns, which lets Postgres answer with a
    plain Index Scan on that PK -- and an Index Scan on a 3-row table happens
    to hand rows back pre-sorted by `criterion`, the trailing key column, even
    with NO `ORDER BY` in the query. That masks a missing `ORDER BY` on a
    table this small (measured: EXPLAIN shows `Index Scan using
    pk_call_ranking_criteria`, output already alphabetical, before this
    function is used). Disabling index scans forces a Seq Scan, whose output
    follows heap (insertion) order instead, so a missing `ORDER BY` is
    actually observable and the mutation test below means something.
    """

    def _on_checkout(dbapi_conn, connection_record, connection_proxy):
        cur = dbapi_conn.cursor()
        cur.execute("SET enable_indexscan = off")
        cur.execute("SET enable_bitmapscan = off")
        cur.execute("SET enable_indexonlyscan = off")
        cur.close()

    event.listen(engine, "checkout", _on_checkout)


def test_criteria_are_ordered_identically_on_list_and_detail(make_app, pg_session_factory):
    """Both surfaces must render chips in the SAME, deterministic order.

    Criteria are inserted out of alphabetical order (`trl`, `budget`,
    `topic_fit`) and index scans are disabled for the app's engine
    (`_force_seq_scan`) so a missing `ORDER BY` would surface heap
    (insertion) order -- `trl, budget, topic_fit` -- instead of being hidden
    by the PK index's incidental sort. Both endpoints add
    `.order_by(CallRankingCriterion.criterion)` (see `_criterion_common_fields`),
    so both must come back alphabetical.
    """
    uid, (pid,) = _seed_user_and_profiles(pg_session_factory)
    cid = _add_call(
        pg_session_factory,
        tag="order",
        profile_id=pid,
        criteria=("trl", "budget", "topic_fit"),
    )
    app = make_app()
    _force_seq_scan(app.state.engine)
    c = _client(app, uid)

    _, body = _measure(c, app, f"/api/calls?profile={pid}")
    listed_order = [x["criterion"] for x in body["items"][0]["criteria"]]

    detail = c.get(f"/api/calls/{cid}?profile={pid}")
    assert detail.status_code == 200, detail.text
    detailed_order = [x["criterion"] for x in detail.json()["criteria"]]

    expected = ["budget", "topic_fit", "trl"]
    assert listed_order == expected
    assert detailed_order == expected


# ---------------------------------------------------------------------------
# applicant_note_count / escalated
# ---------------------------------------------------------------------------
def test_a_ranking_with_null_applicant_notes_counts_zero(make_app, pg_session_factory):
    """`applicant_notes` is nullable and the LOCAL Gemma path always leaves it
    NULL -- the common row, not the edge case. `len(None)` is a 500."""
    uid, (pid,) = _seed_user_and_profiles(pg_session_factory)
    _add_call(pg_session_factory, tag="nullnotes", profile_id=pid, applicant_notes=None)
    app = make_app()
    c = _client(app, uid)
    _, body = _measure(c, app, f"/api/calls?profile={pid}")
    assert body["items"][0]["applicant_note_count"] == 0


def test_notes_are_counted_when_present(make_app, pg_session_factory):
    uid, (pid,) = _seed_user_and_profiles(pg_session_factory)
    _add_call(
        pg_session_factory,
        tag="notes",
        profile_id=pid,
        applicant_notes=[
            {"category": "budget", "text": "check the cap"},
            {"category": "ip", "text": "background IP"},
        ],
    )
    app = make_app()
    c = _client(app, uid)
    _, body = _measure(c, app, f"/api/calls?profile={pid}")
    assert body["items"][0]["applicant_note_count"] == 2


def test_an_unranked_call_reports_zero_notes_and_not_escalated(make_app, pg_session_factory):
    """`view.calls` is `list[tuple[Call, CallRanking | None, str | None]]`: the
    ranked query is an OUTER join, so a call with no ranking row for the
    selected profile still appears with `ranking=None`. (The third slot is the
    decision note, non-None only on the saved/hidden views.)"""
    uid, (pid,) = _seed_user_and_profiles(pg_session_factory)
    _add_call(pg_session_factory, tag="noranking", profile_id=None)
    app = make_app()
    c = _client(app, uid)
    _, body = _measure(c, app, f"/api/calls?profile={pid}")
    item = body["items"][0]
    assert item["aggregate_score"] is None
    assert item["applicant_note_count"] == 0
    assert item["escalated"] is False
    assert item["criteria"] == []


def test_escalated_is_its_own_column_not_a_function_of_the_note_count(make_app, pg_session_factory):
    """Escalation can succeed and still leave zero notes (analysis failed or was
    capped after the remote rank replaced the score), so the card's "N things to
    check" and "this was escalated" are different facts."""
    uid, (pid,) = _seed_user_and_profiles(pg_session_factory)
    _add_call(
        pg_session_factory,
        tag="esc",
        profile_id=pid,
        escalated=True,
        applicant_notes=None,
    )
    app = make_app()
    c = _client(app, uid)
    _, body = _measure(c, app, f"/api/calls?profile={pid}")
    item = body["items"][0]
    assert item["applicant_note_count"] == 0
    assert item["escalated"] is True


def test_not_escalated_when_the_ranking_says_so(make_app, pg_session_factory):
    uid, (pid,) = _seed_user_and_profiles(pg_session_factory)
    _add_call(
        pg_session_factory,
        tag="noesc",
        profile_id=pid,
        escalated=False,
        applicant_notes=[{"category": "budget", "text": "one"}],
    )
    app = make_app()
    c = _client(app, uid)
    _, body = _measure(c, app, f"/api/calls?profile={pid}")
    item = body["items"][0]
    assert item["applicant_note_count"] == 1
    assert item["escalated"] is False


# ---------------------------------------------------------------------------
# The plain columns the card renders.
# ---------------------------------------------------------------------------
def test_the_list_carries_the_columns_the_card_renders(make_app, pg_session_factory):
    uid, (pid,) = _seed_user_and_profiles(pg_session_factory)
    _add_call(
        pg_session_factory,
        tag="cols",
        profile_id=pid,
        programme="Horizon Europe",
        budget_min=1_000_000,
        budget_max=5_000_000,
        currency="SEK",
        summary="A short summary.",
        keywords=("oncology", "imaging"),
    )
    app = make_app()
    c = _client(app, uid)
    _, body = _measure(c, app, f"/api/calls?profile={pid}")
    item = body["items"][0]
    assert item["programme"] == "Horizon Europe"
    assert item["budget_min"] == 1_000_000
    assert item["budget_max"] == 5_000_000
    assert item["currency"] == "SEK"
    assert item["url"] == "https://example.test/card/cols"
    assert item["summary"] == "A short summary."
    assert item["keywords"] == ["oncology", "imaging"]


def test_nullable_columns_serialize_as_null_not_missing(make_app, pg_session_factory):
    uid, (pid,) = _seed_user_and_profiles(pg_session_factory)
    _add_call(pg_session_factory, tag="bare", profile_id=pid)
    app = make_app()
    c = _client(app, uid)
    _, body = _measure(c, app, f"/api/calls?profile={pid}")
    item = body["items"][0]
    for key in ("programme", "budget_min", "budget_max", "currency", "summary"):
        assert key in item, key
        assert item[key] is None, key
    assert item["keywords"] == []
