"""Task 4 gates: the daily tick breaks on rows examined, not rows worked.

The profile gates come from the matching-freshness plan (2026-09-06). The call
gates at the bottom are their twins from the call-embedding-freshness plan
(spec docs/superpowers/specs/2026-09-08-call-embedding-freshness.md, §5 G1-G4).
"""

from __future__ import annotations

import pytest
from sqlalchemy import event

import grant_lab.embeddings.refresh as refresh_mod
import grant_lab.embeddings.similarity as similarity_mod
import grant_lab.profiles.fingerprint as fingerprint_mod
from grant_lab.db.models import Call, Profile
from grant_lab.embeddings.refresh import (
    _refresh_missing_embeddings_batch,
    refresh_missing_embeddings_tick,
)
from grant_lab.embeddings.similarity import build_call_text
from grant_lab.profiles.fingerprint import call_embedding_fingerprint, embedding_fingerprint
from tests.freshness_helpers import FakeEmbedder, make_call, make_profile


@pytest.fixture()
def fake_embedder():
    """FakeEmbedder counts embed calls (gate 7) AND supplies session(), which
    _refresh_missing_embeddings_batch opens twice per batch."""
    return FakeEmbedder()


def test_a_changed_text_is_stale_even_with_a_current_vector_and_model(
    pg_session_factory, fake_embedder
):
    """GATE 4. Vector present, model current -- invisible to the old predicate."""
    with pg_session_factory() as s:
        p = make_profile(s, description="Liver disease")
        s.commit()
        pid = p.id
    refresh_missing_embeddings_tick(session_factory=pg_session_factory, embed_client=fake_embedder)
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding_fingerprint == embedding_fingerprint(p)
        p.description = "Cardiac imaging"
        s.commit()
        stale_fp = p.embedding_fingerprint

    refresh_missing_embeddings_tick(session_factory=pg_session_factory, embed_client=fake_embedder)
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding_fingerprint != stale_fp
        assert p.embedding_fingerprint == embedding_fingerprint(p)


def test_a_stale_profile_behind_a_full_fresh_page_is_still_reached(
    pg_session_factory, fake_embedder
):
    """GATE 5 -- the one that would have caught rev 1's mistake.

    Uses batch_size=2 rather than a realistic corpus, so the loop's exit
    condition is what is under test, not the row count.
    """
    with pg_session_factory() as s:
        for i in range(4):
            make_profile(s, description=f"Fresh {i}")
        s.commit()
    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=fake_embedder, batch_size=2
    )

    with pg_session_factory() as s:
        stale = make_profile(s, description="Originally this")
        s.commit()
        sid = stale.id
    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=fake_embedder, batch_size=2
    )
    with pg_session_factory() as s:
        assert s.get(Profile, sid).embedding is not None
        # Now make the HIGHEST id stale, behind two full pages of fresh rows.
        s.get(Profile, sid).description = "Changed to something else"
        s.commit()

    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=fake_embedder, batch_size=2
    )
    with pg_session_factory() as s:
        p = s.get(Profile, sid)
        assert p.embedding_fingerprint == embedding_fingerprint(p), (
            "the drain stopped at the first page with nothing to do and threw away its cursor"
        )


def test_a_profile_cleared_to_blank_is_invalidated_by_the_tick(pg_session_factory, fake_embedder):
    """GATE 8 -- hole 4, a defect in already-shipped code.

    refresh.py:328 excluded blank profiles from the tick, so the branch that
    wipes a cleared profile's vector could only ever fire from the request
    path -- the lossy one.
    """
    with pg_session_factory() as s:
        p = make_profile(s, description="Liver disease")
        s.commit()
        pid = p.id
    refresh_missing_embeddings_tick(session_factory=pg_session_factory, embed_client=fake_embedder)
    with pg_session_factory() as s:
        assert s.get(Profile, pid).embedding is not None
        p = s.get(Profile, pid)
        p.description = ""
        p.structured = {}
        s.commit()

    refresh_missing_embeddings_tick(session_factory=pg_session_factory, embed_client=fake_embedder)
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding is None
        assert p.embedding_model is None
        assert p.embedding_fingerprint is None


def test_an_already_blank_and_already_cleared_profile_is_not_rescanned_forever(
    pg_session_factory, fake_embedder
):
    """The other half of gate 8: including blank rows must not break termination.

    The mechanism that can actually break is NOT "a blank row gets re-embedded"
    -- it cannot, because such a row is never appended to `profile_ids`. It is
    "a blank row is re-EXAMINED every iteration because the cursor never moves
    past it". So this asserts the cursor, and then proves at the tick level
    that a stale row sitting BEHIND a blank one is still reached.
    """
    with pg_session_factory() as s:
        p = make_profile(s, description="")
        p.structured = {}
        s.commit()
        blank_id = p.id
    summary = _refresh_missing_embeddings_batch(
        session_factory=pg_session_factory,
        embed_client=fake_embedder,
        batch_size=200,
        force=False,
        after_call_id=0,
        after_profile_id=0,
    )
    assert summary["profiles_embedded"] == 0
    assert summary["profiles_failed"] == 0
    # It WAS examined -- that is what keeps the drain going ...
    assert summary["profiles_seen"] == 1
    # ... and the cursor moved past it, which is what stops the drain from
    # re-reading it on every iteration until max_batches.
    assert summary["max_profile_id"] == blank_id

    # Tick-level proof, with batch_size=1 so the blank row owns page one: a
    # stale row behind it must still be repaired. With a stuck cursor every
    # page re-reads the blank row and this profile is never reached.
    with pg_session_factory() as s:
        behind = make_profile(s, description="Behind the blank row")
        s.commit()
        behind_id = behind.id
    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=fake_embedder, batch_size=1
    )
    with pg_session_factory() as s:
        p = s.get(Profile, behind_id)
        assert p.embedding is not None
        assert p.embedding_fingerprint == embedding_fingerprint(p)


def test_force_mode_ignores_the_fingerprint(pg_session_factory, fake_embedder):
    """GATE 7. force=True must re-embed every embeddable row regardless."""
    with pg_session_factory() as s:
        make_profile(s, description="Liver disease")
        s.commit()
    refresh_missing_embeddings_tick(session_factory=pg_session_factory, embed_client=fake_embedder)
    fake_embedder.calls = 0

    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=fake_embedder, force=True
    )
    assert fake_embedder.calls > 0, "force mode skipped a fingerprint-fresh row"


def test_the_staleness_decision_does_not_load_the_vector(pg_session_factory, fake_embedder):
    """GATE 6. Parsing 1024-dim vectors is the measured dominant cost of a
    profile load; loading them to decide whether to replace them is a
    regression. Assert on the emitted SQL of the CANDIDATE SELECT.
    """
    import re

    statements: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    with pg_session_factory() as session:
        engine = session.bind
        event.listen(engine, "before_cursor_execute", _record)
        try:
            make_profile(session, description="Liver disease")
            session.commit()
            statements.clear()
            _refresh_missing_embeddings_batch(
                session_factory=pg_session_factory,
                embed_client=fake_embedder,
                batch_size=200,
                force=False,
                after_call_id=0,
                after_profile_id=0,
            )
        finally:
            event.remove(engine, "before_cursor_execute", _record)

    # Narrow to the paged CANDIDATE scan. A filter of
    # `"FROM profiles" in s and "SELECT" in s` also catches the
    # `session.get(Profile, pid)` that refresh_profile_embedding must issue
    # for the row it is about to embed -- which loads the vector by necessity.
    candidate = [s for s in statements if "ORDER BY profiles.id" in s and "LIMIT" in s]
    assert len(candidate) == 1, f"expected exactly one candidate scan, got {len(candidate)}"
    sql = candidate[0]

    # Look ONLY at the result columns. `re.split` on a FROM word boundary, not
    # `sql.split(" FROM ")`: the compiled SQL puts a newline before FROM, so
    # the space-delimited form silently returns the WHOLE statement and every
    # position-sensitive assertion below then reads the WHERE clause too.
    select_list = re.split(r"\bFROM\b", sql, maxsplit=1)[0]

    # The vector must not be a result column in ANY shape the ORM emits it:
    #   `profiles.embedding,`        mid-list
    #   `profiles.embedding`         last, no trailing comma
    #   `profiles.embedding AS ...`  aliased (what session.get emits)
    # The two lookaheads exclude the three things that legitimately start with
    # the same prefix: profiles.embedding_model, profiles.embedding_fingerprint,
    # and `profiles.embedding IS NULL AS embedding_is_null` -- the computed
    # flag, which is exactly what this gate wants to see instead.
    leaked = re.findall(r"profiles\.embedding(?!_)(?!\s+IS\s+NULL)", select_list)
    assert not leaked, f"the candidate SELECT loaded the 1024-dim vector:\n{sql}"
    assert "embedding_is_null" in select_list, (
        f"no computed NULL flag in the candidate scan:\n{sql}"
    )


# --- The call half: spec 2026-09-08 §5, G1-G4 (plan gates 5-8) ---------------


class _RecordingEmbedder(FakeEmbedder):
    """FakeEmbedder that also records the TEXT of every embed.

    The gates below count embed calls, never vectors: FakeEmbedder returns a
    constant [0.1] * 1024, so a vector comparison fails identically on correct
    and broken code (spec §1). The bare count cannot say WHICH call re-embedded,
    though, and gates 5 and 6 need exactly that. Every call seeded here has a
    distinct title, so the number of times one call's composed text appears in
    `texts` is that call's embed count.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.texts: list[str] = []

    def embed(self, text, _client=None):
        self.texts.append(text)
        return super().embed(text, _client=_client)

    def reset(self) -> None:
        self.calls = 0
        self.texts.clear()


@pytest.fixture()
def recording_embedder():
    return _RecordingEmbedder()


def _embeds_of_current_text(session_factory, embedder, call_id: int) -> int:
    """How many times the embedder was handed this call's CURRENT composed text."""
    with session_factory() as s:
        return embedder.texts.count(build_call_text(s.get(Call, call_id)))


def _two_calls_embedded_then_one_changed(session_factory, embedder) -> tuple[int, int]:
    """Two calls, embedded and fingerprinted by a first tick; then one call's
    summary changes. Returns (changed_id, untouched_id) with the embedder reset,
    so what it records afterwards is the second tick alone."""
    with session_factory() as s:
        changed_id = make_call(s, title="Liver disease programme").id
        untouched_id = make_call(s, title="Cardiac imaging programme").id
        s.commit()
    refresh_missing_embeddings_tick(session_factory=session_factory, embed_client=embedder)
    assert embedder.calls == 2, "setup: the first tick must embed both calls"

    with session_factory() as s:
        c = s.get(Call, changed_id)
        stamped = c.embedding_fingerprint
        c.summary = "Rewritten upstream: now about something else entirely."
        s.commit()
        # Vector present, model current, text new: the row the old
        # NULL-or-mismatched-model predicate could never see.
        assert c.embedding is not None
        assert c.embedding_model == embedder.model
        assert stamped != call_embedding_fingerprint(c)
    embedder.reset()
    return changed_id, untouched_id


def test_a_changed_call_re_embeds_on_the_next_tick(pg_session_factory, recording_embedder):
    """Plan GATE 5 (spec G1) -- the reproduction of spec §1.

    On the unfixed code the second tick embeds nothing: the changed call's
    vector is present and its model current, so the SQL-narrowed call half
    never returns it.
    """
    changed_id, _ = _two_calls_embedded_then_one_changed(pg_session_factory, recording_embedder)

    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=recording_embedder
    )

    n = _embeds_of_current_text(pg_session_factory, recording_embedder, changed_id)
    assert n == 1, (
        f"the changed call must re-embed exactly once on the next tick; it re-embedded {n} "
        f"time(s), and the tick embedded {recording_embedder.calls} call(s) in total"
    )
    with pg_session_factory() as s:
        c = s.get(Call, changed_id)
        assert c.embedding_fingerprint == call_embedding_fingerprint(c)


def test_an_unchanged_call_does_not_re_embed(pg_session_factory, recording_embedder):
    """Plan GATE 6 (spec G2) -- the other failure direction.

    Gate 5 alone passes a tick that re-embeds the world. Same corpus and same
    change as gate 5; this asserts on the call that was NOT changed. It passes
    on the unfixed code too (nothing re-embeds there) -- it is gate 5's partner,
    not a reproduction, and it is the gate a "fix" that marks every row stale
    must fail.
    """
    _, untouched_id = _two_calls_embedded_then_one_changed(pg_session_factory, recording_embedder)

    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=recording_embedder
    )

    n = _embeds_of_current_text(pg_session_factory, recording_embedder, untouched_id)
    assert n == 0, (
        f"a call whose text, model and vector are all current re-embedded {n} time(s); "
        f"the tick embedded {recording_embedder.calls} call(s) in total"
    )


def test_a_stale_call_behind_a_full_fresh_page_is_still_reached(
    pg_session_factory, recording_embedder
):
    """Plan GATE 7 (spec G3) -- the call-side twin of
    test_a_stale_profile_behind_a_full_fresh_page_is_still_reached, and the
    gate the drain's termination argument turns on.

    batch_size=2 and five calls, the stale one LAST: behind two full pages of
    fresh calls. There are no profiles, so profiles_seen is 0 on every page and
    the drain survives page one only if a page of fresh calls counts as
    EXAMINED. A drain that breaks on calls worked (calls_embedded +
    calls_failed) stops there and never reaches the stale call. A corpus that
    fits in one page would pass on that broken drain, which is why the page is
    this small.
    """
    with pg_session_factory() as s:
        ids = [make_call(s, title=f"Fresh call {i}").id for i in range(5)]
        s.commit()
    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=recording_embedder, batch_size=2
    )
    assert recording_embedder.calls == 5, "setup: the first tick must embed all five calls"

    stale_id = max(ids)
    with pg_session_factory() as s:
        c = s.get(Call, stale_id)
        c.summary = "Changed to something else."
        s.commit()
        assert c.embedding_fingerprint != call_embedding_fingerprint(c)
    recording_embedder.reset()

    summary = refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=recording_embedder, batch_size=2
    )

    n = _embeds_of_current_text(pg_session_factory, recording_embedder, stale_id)
    assert n == 1, (
        "the drain never got past the first page of fresh calls to the stale call "
        f"behind it (re-embedded {n} time(s))"
    )
    with pg_session_factory() as s:
        c = s.get(Call, stale_id)
        assert c.embedding_fingerprint == call_embedding_fingerprint(c)
    # Rows EXAMINED, not rows worked: the drain looked at all five, worked one.
    assert summary["calls_seen"] == 5


def test_editing_build_call_text_invalidates_every_call_fingerprint(
    pg_session_factory, recording_embedder, monkeypatch
):
    """Plan GATE 8 (spec G4) -- route 3, and the reason D2 hashes OUTPUT.

    Editing the composer (or CALL_TEXT_FULL_TEXT_LIMIT) changes what every
    vector describes while changing no row at all. A hash over the columns
    would still match here; only a hash over build_call_text's output moves.

    `from x import y` binds a separate name in each importing module, so the
    edit is applied at EVERY lookup site the tick reaches:
    profiles.fingerprint (the staleness decision and the stamp) and
    embeddings.refresh (the text actually embedded), plus the defining module.
    Both live sites are checked below, one before the tick and one after.
    """
    with pg_session_factory() as s:
        ids = [make_call(s, title=f"Composer call {i}").id for i in range(3)]
        s.commit()
    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=recording_embedder
    )
    assert recording_embedder.calls == 3, "setup: the first tick must embed all three calls"

    original = similarity_mod.build_call_text

    def edited(call) -> str:
        return "EDITED COMPOSER\n\n" + original(call)

    for mod in (similarity_mod, fingerprint_mod, refresh_mod):
        monkeypatch.setattr(mod, "build_call_text", edited)

    # Live at the fingerprint's lookup site: every stored fingerprint now
    # mismatches, though no column of any call changed.
    with pg_session_factory() as s:
        for cid in ids:
            c = s.get(Call, cid)
            assert c.embedding_fingerprint != call_embedding_fingerprint(c)
    recording_embedder.reset()

    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=recording_embedder
    )

    with pg_session_factory() as s:
        expected = sorted(edited(s.get(Call, cid)) for cid in ids)
    # Live at refresh's lookup site too: what was embedded is the EDITED text.
    assert sorted(recording_embedder.texts) == expected, (
        f"editing build_call_text must re-embed every call with the new text; the tick "
        f"embedded {recording_embedder.calls} of {len(ids)}"
    )
    with pg_session_factory() as s:
        for cid in ids:
            c = s.get(Call, cid)
            assert c.embedding_fingerprint == call_embedding_fingerprint(c)


def test_the_call_staleness_decision_does_not_load_the_vector(pg_session_factory, fake_embedder):
    """Spec D5 -- the call-side twin of
    test_the_staleness_decision_does_not_load_the_vector.

    The widened call scan examines every call past the cursor, so loading the
    1024-dim vector there would parse one per row examined, just to decide
    whether to replace it. Assert on the emitted SQL of the CANDIDATE SELECT;
    see the profile twin for why the select list is split off with a regex.
    """
    import re

    statements: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    with pg_session_factory() as session:
        engine = session.bind
        event.listen(engine, "before_cursor_execute", _record)
        try:
            make_call(session)
            session.commit()
            statements.clear()
            _refresh_missing_embeddings_batch(
                session_factory=pg_session_factory,
                embed_client=fake_embedder,
                batch_size=200,
                force=False,
                after_call_id=0,
                after_profile_id=0,
            )
        finally:
            event.remove(engine, "before_cursor_execute", _record)

    # The paged candidate scan only -- NOT the `session.get(Call, cid)` that
    # refresh_call_embedding issues for the row it embeds, which loads the
    # vector by necessity.
    candidate = [s for s in statements if "ORDER BY calls.id" in s and "LIMIT" in s]
    assert len(candidate) == 1, f"expected exactly one candidate scan, got {len(candidate)}"
    sql = candidate[0]
    select_list = re.split(r"\bFROM\b", sql, maxsplit=1)[0]

    leaked = re.findall(r"calls\.embedding(?!_)(?!\s+IS\s+NULL)", select_list)
    assert not leaked, f"the candidate SELECT loaded the 1024-dim vector:\n{sql}"
    assert "embedding_is_null" in select_list, (
        f"no computed NULL flag in the candidate scan:\n{sql}"
    )
    # The staleness decision needs the composer's input UNCUT: build_call_text
    # owns the one cut (see the full_text comment in
    # _refresh_missing_embeddings_batch). Any SQL function wrapping the column
    # -- left(), substr(), substring() -- is a second copy of the limit.
    assert re.search(r"\bcalls\.full_text\b", select_list), (
        f"the scan cannot compose the call text:\n{sql}"
    )
    assert not re.search(r"\(\s*calls\.full_text\b", select_list), (
        f"full_text is cut in SQL as well as in build_call_text:\n{sql}"
    )
