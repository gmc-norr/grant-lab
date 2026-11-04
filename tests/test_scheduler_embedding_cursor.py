"""Task 9 gates: the scheduler carries the profile cursor across capped ticks.

Spec `docs/superpowers/specs/2026-09-06-matching-freshness-design.md`, change
5(b) second half: `_TICK_MAX_BATCHES` bounds the drain, and a tick that hits
that cap used to restart from `after_profile_id=0` tomorrow. Once the profile
scan was widened (change 5(a)), a long prefix of fresh profiles consumes the
whole allowance every day and a stale profile beyond it is never reached.

The spec asks for this to be pinned "with small `batch_size` and `max_batches`
values rather than a realistic corpus", so `tiny_tick` below drives the real
tick at batch_size=1 / max_batches=3 and the corpus is five profiles.

GATE 9c pins the regression that first fix introduced: the drain's
`scan_exhausted` is a property of BOTH halves (`examined` sums calls and
profiles), so keying the PROFILE cursor on it froze that cursor forever on any
tick held at the cap by the CALL half. Its corpus is two profiles and
always-failing calls: twenty in the consequence test, because the scheduler now
carries the CALL cursor too (G6 below), so failing calls are walked past rather
than re-read from 0 every tick, and a small corpus runs the call scan off its
table every other tick. On those ticks the joint flag is True and resets the
frozen profile cursor by accident, which hides the bug.

G6-G8 (spec `2026-09-08-call-embedding-freshness.md` §4 D9, §5) are the same
three gates for the CALL cursor. Since migration 0080 the call half examines
every call row, so a tick that restarts it at id 0 can never reach past
`batch_size * max_batches` calls; the scheduler now carries `after_call_id` and
resets it on the call half's own `call_scan_exhausted`. G8 is 9c's mirror: a
tick held at the cap by the PROFILE half must still reset the CALL cursor.
"""

from __future__ import annotations

import pytest

import grant_lab.scheduler as scheduler_mod
from grant_lab.config import Settings
from grant_lab.db.models import Call, Profile
from grant_lab.embeddings.refresh import refresh_missing_embeddings_tick
from grant_lab.profiles.fingerprint import call_embedding_fingerprint, embedding_fingerprint
from grant_lab.ranking.ollama_client import LLMError
from tests.freshness_helpers import FakeEmbedder, make_call, make_profile


class _NullAlerter:
    def record_failure(self, *a, **kw):
        pass

    def record_success(self, *a, **kw):
        pass


@pytest.fixture()
def _settings_env(monkeypatch):
    """The env vars Settings() requires. The scheduler is built but never
    started, so no job fires except the one this test calls by hand."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


@pytest.fixture()
def _tick_cursors(monkeypatch):
    """Drive the scheduler's tick at batch_size=1, max_batches=3, recording
    BOTH cursors the scheduler hands it. `tiny_tick` and
    `tiny_tick_call_cursors` are the two views onto one recording; they must
    share it, because only one wrapper can be patched in.

    `after_call_id` is recorded as the value the real function will USE: a
    scheduler that omits the argument gets the default 0, which is exactly the
    restart-from-zero behaviour the call-cursor gates exist to catch.
    """
    seen: dict[str, list[int]] = {"after_profile_id": [], "after_call_id": []}

    def _small(**kwargs):
        seen["after_profile_id"].append(kwargs["after_profile_id"])
        seen["after_call_id"].append(kwargs.get("after_call_id", 0))
        kwargs["batch_size"] = 1
        kwargs["max_batches"] = 3
        return refresh_missing_embeddings_tick(**kwargs)

    monkeypatch.setattr(scheduler_mod, "refresh_missing_embeddings_tick", _small)
    return seen


@pytest.fixture()
def tiny_tick(_tick_cursors):
    """Drive the scheduler's tick at batch_size=1, max_batches=3.

    The scheduler hardcodes batch_size=200 and takes the default
    max_batches=200, so reaching the cap for real would need a 40,000-row
    corpus. Only those two size knobs are overridden — the REAL
    `refresh_missing_embeddings_tick` still runs, against the real database,
    so the cursor argument the scheduler passes (or fails to pass) is what
    these tests measure.

    Yields the list of `after_profile_id` values the scheduler actually
    passed, in order, one per tick — the evidence a frozen cursor shows up in
    directly rather than only through its consequences.
    """
    return _tick_cursors["after_profile_id"]


@pytest.fixture()
def tiny_tick_call_cursors(_tick_cursors):
    """`tiny_tick`'s twin for the CALL cursor: the `after_call_id` values the
    scheduler passed (or, when it passed none, the 0 the tick defaulted to),
    in order, one per tick."""
    return _tick_cursors["after_call_id"]


@pytest.fixture()
def fake_embedder():
    """One embedder shared by the seeding call and the scheduler's own job, so
    `embedding_model` matches across both and nothing is stale by accident."""
    return FakeEmbedder()


@pytest.fixture()
def embeddings_tick(pg_session_factory, fake_embedder, _settings_env, tiny_tick):
    """The scheduler's own `embeddings:refresh-missing` job function.

    Taken off the built scheduler rather than reimplemented, so the closure
    under test is the production one (the pattern tests/test_scheduler.py
    uses for boards:auto-publish).
    """

    settings = Settings()
    sched = scheduler_mod.build_scheduler(
        session_factory=pg_session_factory,
        settings=settings,
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=fake_embedder,
        start=False,
    )
    job = sched.get_job("embeddings:refresh-missing")
    assert job is not None
    try:
        yield job.func
    finally:
        sched.shutdown(wait=False)


def _seed_five_fresh_profiles(session_factory, embedder) -> list[int]:
    """Five profiles, all embedded and fingerprinted against their own text."""
    with session_factory() as s:
        ids = [make_profile(s, description=f"Fresh {i}").id for i in range(5)]
        s.commit()
    refresh_missing_embeddings_tick(session_factory=session_factory, embed_client=embedder)
    return ids


def _go_stale(session_factory, profile_id: int) -> None:
    with session_factory() as s:
        s.get(Profile, profile_id).description = "Changed to something else"
        s.commit()


def _is_current(session_factory, profile_id: int) -> bool:
    with session_factory() as s:
        p = s.get(Profile, profile_id)
        return p.embedding_fingerprint == embedding_fingerprint(p)


def test_a_capped_tick_resumes_where_it_stopped_instead_of_restarting_at_zero(
    pg_session_factory, fake_embedder, embeddings_tick
):
    """GATE 9a — the trap the spec names.

    Five fresh profiles, a cap of three batches of one row. The first tick
    examines ids[0..2] and stops on the cap having done no work. If the
    scheduler does not carry the cursor, the second tick restarts at
    after_profile_id=0 and halts at the same wall, and the stale ids[4] is
    never reached — today, or on any later day.
    """
    ids = _seed_five_fresh_profiles(pg_session_factory, fake_embedder)
    _go_stale(pg_session_factory, ids[4])

    embeddings_tick()  # examines ids[0], ids[1], ids[2] -> hits max_batches
    embeddings_tick()  # must resume after ids[2] and reach ids[4]

    assert _is_current(pg_session_factory, ids[4]), (
        "the capped tick restarted at after_profile_id=0 and stalled at the same wall"
    )


def test_an_exhausted_scan_resets_the_cursor_so_a_row_going_stale_behind_it_is_revisited(
    pg_session_factory, fake_embedder, embeddings_tick
):
    """GATE 9b — the failure mode the fix itself could introduce.

    A carried cursor that never goes back to 0 is a worse bug than the one it
    fixes: every profile behind the high-water mark becomes permanently
    unreachable. Once a scan is exhausted the cursor must reset.
    """
    ids = _seed_five_fresh_profiles(pg_session_factory, fake_embedder)

    embeddings_tick()  # capped, stops after ids[2]
    embeddings_tick()  # ids[3], ids[4], then an empty page -> scan EXHAUSTED

    _go_stale(pg_session_factory, ids[0])
    embeddings_tick()  # only reaches ids[0] if the exhausted scan reset the cursor

    assert _is_current(pg_session_factory, ids[0]), (
        "an exhausted scan left the cursor at the high-water mark, so a profile "
        "that goes stale behind it is never revisited"
    )


# --- GATE 9c: a tick held at the cap by the CALL half ------------------------

_UNEMBEDDABLE = "UNEMBEDDABLE-CALL"


class _CallsFailEmbedder(FakeEmbedder):
    """Embeds profiles, refuses calls.

    A call that fails to embed records nothing (`refresh_call_embedding`
    returns False without writing), so it stays stale. The drain breaks only on
    a batch that EXAMINES nothing, so while calls remain ahead of the call
    cursor the call half returns a row every batch and holds the drain at
    `max_batches` -- the condition this gate needs: capped for a reason that
    has nothing to do with the profile half.

    Not forever, though. The scheduler carries the call cursor across ticks,
    so these calls are walked past a batch at a time and the call scan resets
    once it runs off the end of the table. A gate that needs every tick capped
    must seed more calls than its ticks examine.
    """

    def embed(self, text, _client=None):
        if _UNEMBEDDABLE in text:
            self.calls += 1
            raise LLMError("ollama refused this call")
        return super().embed(text, _client=_client)


@pytest.fixture()
def fake_embedder_calls_fail():
    return _CallsFailEmbedder()


@pytest.fixture()
def embeddings_tick_calls_fail(
    pg_session_factory, fake_embedder_calls_fail, _settings_env, tiny_tick
):
    """`embeddings_tick`, but wired to the embedder that refuses calls."""

    settings = Settings()
    sched = scheduler_mod.build_scheduler(
        session_factory=pg_session_factory,
        settings=settings,
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=fake_embedder_calls_fail,
        start=False,
    )
    job = sched.get_job("embeddings:refresh-missing")
    assert job is not None
    try:
        yield job.func
    finally:
        sched.shutdown(wait=False)


def test_a_tick_capped_by_the_call_half_still_resets_the_profile_cursor(
    pg_session_factory,
    fake_embedder_calls_fail,
    embeddings_tick_calls_fail,
    tiny_tick,
    tiny_tick_call_cursors,
):
    """GATE 9c — the regression the joint flag introduced.

    Corpus: two profiles and twenty calls that always fail to embed. At
    batch_size=1 / max_batches=3 the profile half runs off the end of its own
    table on the third batch of the FIRST tick, but the call half still
    returns a row on every batch of all six ticks, so `examined` is never 0
    and every tick stops on the cap.

    The twenty is load-bearing: six ticks of three one-row batches examine 18
    calls, and two more keep the call scan unfinished after the last tick. The
    scheduler carries the CALL cursor (G6), so failing calls are walked past
    rather than re-read from 0 every tick. With the original five, the second
    tick ran the call scan off its table, the joint flag went True and reset
    the frozen profile cursor by accident on every other tick, and the
    joint-flag mutant passed this test (measured on bf4f7194).

    Keyed on the JOINT `scan_exhausted`, the scheduler therefore reads "rows
    still ahead" and carries `after_profile_id=2` into every later tick — the
    measured sequence was [0, 2, 2, 2, 2, 2] — and a profile that goes stale
    behind that frozen cursor is never repaired. Before the carry existed,
    every tick restarted at 0 and DID repair it, so this is a regression.

    The profile cursor must reset on the PROFILE half's own exhaustion.
    """
    with pg_session_factory() as s:
        profile_ids = [make_profile(s, description=f"Fresh {i}").id for i in range(2)]
        s.commit()
    # Seed the profiles current BEFORE the calls exist, so the corpus starts
    # with the profile half genuinely clean and only the calls are unworkable.
    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=fake_embedder_calls_fail
    )
    assert all(_is_current(pg_session_factory, pid) for pid in profile_ids)

    with pg_session_factory() as s:
        for i in range(20):
            make_call(s, title=f"{_UNEMBEDDABLE} {i}")
        s.commit()

    embeddings_tick_calls_fail()  # capped by the calls; profile half exhausted on batch 3
    embeddings_tick_calls_fail()  # capped by the calls again

    _go_stale(pg_session_factory, profile_ids[0])

    for _ in range(4):
        embeddings_tick_calls_fail()

    # The premise, checked rather than assumed: the CALL half held every tick
    # at the cap, so its cursor only ever advances -- it must never fall back
    # to 0 after the first tick. This has already been lost silently once
    # (the corpus was five calls before the twenty above); if it regresses
    # again, the call scan runs off its own table mid-run, the joint
    # `scan_exhausted` goes True on that tick, and the frozen profile cursor
    # gets reset by the SAME accident this gate exists to catch -- which
    # would make the assertion below pass for the wrong reason.
    assert 0 not in tiny_tick_call_cursors[1:], (
        "the call half did not hold every tick at the cap -- the call cursor "
        f"returned to 0 after the first tick; values passed: {tiny_tick_call_cursors}"
    )
    assert _is_current(pg_session_factory, profile_ids[0]), (
        "the call half held the tick at max_batches, so the joint scan_exhausted "
        "stayed False and the profile cursor froze past the last profile; the "
        f"after_profile_id values actually passed were {tiny_tick}"
    )


def test_the_call_half_being_capped_does_not_freeze_the_profile_cursor_sequence(
    pg_session_factory, fake_embedder_calls_fail, embeddings_tick_calls_fail, tiny_tick
):
    """GATE 9c, stated on the cursor itself rather than on its consequence.

    Two profiles and five always-failing calls, three ticks. Every tick's
    profile half runs off the end of the two-row table, so every tick must be
    handed `after_profile_id=0`, and a frozen cursor shows up as any nonzero
    entry. Five calls suffice for this form, unlike the consequence test: the
    call half holds the FIRST tick at the cap, so the joint-flag mutant hands
    the second tick `after_profile_id=2`. Measured [0, 2, 0] now that the call
    cursor is carried (the second tick runs the call scan off its table and
    resets the cursor by accident); [0, 2, 2] before it was.
    """
    with pg_session_factory() as s:
        for i in range(2):
            make_profile(s, description=f"Fresh {i}")
        for i in range(5):
            make_call(s, title=f"{_UNEMBEDDABLE} {i}")
        s.commit()

    for _ in range(3):
        embeddings_tick_calls_fail()

    assert tiny_tick == [0, 0, 0], (
        "a tick capped by the CALL half must still reset the profile cursor once "
        "the profile half has run off the end of its own table"
    )


# --- G6-G8: the CALL cursor -----------------------------------------------------


def _seed_five_fresh_calls(session_factory, embedder) -> list[int]:
    """Five calls, all embedded and fingerprinted against their own text, and
    no profiles, so every batch's `examined` is the call half alone."""
    with session_factory() as s:
        ids = [make_call(s, title=f"Fresh call {i}").id for i in range(5)]
        s.commit()
    refresh_missing_embeddings_tick(session_factory=session_factory, embed_client=embedder)
    return ids


def _call_goes_stale(session_factory, call_id: int) -> None:
    """A re-ingest rewrote the text under a current vector — the state migration
    0080 made the call half able to see."""
    with session_factory() as s:
        s.get(Call, call_id).summary = "Rewritten by a later ingest"
        s.commit()


def _call_is_current(session_factory, call_id: int) -> bool:
    with session_factory() as s:
        c = s.get(Call, call_id)
        return c.embedding_fingerprint == call_embedding_fingerprint(c)


def test_a_capped_tick_resumes_the_call_cursor_instead_of_restarting_at_zero(
    pg_session_factory, fake_embedder, embeddings_tick, tiny_tick_call_cursors
):
    """G6 (plan Gate 9) — the call-side twin of GATE 9a.

    Five fresh calls, a cap of three batches of one row. The first tick
    examines ids[0..2] and stops on the cap having done no work. If the
    scheduler does not carry the call cursor, the second tick restarts at
    after_call_id=0 and halts at the same wall, and the stale ids[4] is never
    reached. At production settings the wall is row 40,001 of the calls table.
    """
    ids = _seed_five_fresh_calls(pg_session_factory, fake_embedder)
    _call_goes_stale(pg_session_factory, ids[4])

    embeddings_tick()  # examines ids[0], ids[1], ids[2] -> hits max_batches
    embeddings_tick()  # must resume after ids[2] and reach ids[4]

    assert tiny_tick_call_cursors == [0, ids[2]], (
        "the second tick must resume the call scan after the last call the capped "
        f"tick examined (ids[2]={ids[2]}); after_call_id values passed: "
        f"{tiny_tick_call_cursors}"
    )
    assert _call_is_current(pg_session_factory, ids[4]), (
        "the capped tick restarted at after_call_id=0 and stalled at the same wall"
    )


def test_an_exhausted_call_scan_resets_the_call_cursor_so_a_call_going_stale_behind_it_is_revisited(
    pg_session_factory, fake_embedder, embeddings_tick, tiny_tick_call_cursors
):
    """G7 (plan Gate 10) — the call-side twin of GATE 9b.

    A carried cursor that never goes back to 0 makes every call behind the
    high-water mark permanently unreachable, which is worse than the wall it
    removes. Once the call scan runs off the end of its table, the cursor must
    reset.
    """
    ids = _seed_five_fresh_calls(pg_session_factory, fake_embedder)

    embeddings_tick()  # capped, stops after ids[2]
    embeddings_tick()  # ids[3], ids[4], then an empty page -> call scan EXHAUSTED

    _call_goes_stale(pg_session_factory, ids[0])
    embeddings_tick()  # only reaches ids[0] if the exhausted scan reset the cursor

    assert tiny_tick_call_cursors[2] == 0, (
        "an exhausted call scan must hand the next tick after_call_id=0; values "
        f"passed: {tiny_tick_call_cursors}"
    )
    assert _call_is_current(pg_session_factory, ids[0]), (
        "an exhausted call scan left the cursor at the high-water mark, so a call "
        "that goes stale behind it is never revisited"
    )


_UNEMBEDDABLE_PROFILE = "UNEMBEDDABLE-PROFILE"


class _ProfilesFailEmbedder(FakeEmbedder):
    """Embeds calls, refuses profiles — `_CallsFailEmbedder` the other way round.

    A profile that fails to embed records nothing, so it stays stale. What
    holds the drain at `max_batches` here is simply that the profile half
    returns a row on every batch (the drain breaks only on a batch that
    EXAMINES nothing); the profile cursor walks forward across ticks, so the
    corpus needs more profiles than the ticks under test examine.
    """

    def embed(self, text, _client=None):
        if _UNEMBEDDABLE_PROFILE in text:
            self.calls += 1
            raise LLMError("ollama refused this profile")
        return super().embed(text, _client=_client)


@pytest.fixture()
def fake_embedder_profiles_fail():
    return _ProfilesFailEmbedder()


@pytest.fixture()
def embeddings_tick_profiles_fail(
    pg_session_factory, fake_embedder_profiles_fail, _settings_env, tiny_tick
):
    """`embeddings_tick`, but wired to the embedder that refuses profiles."""

    settings = Settings()
    sched = scheduler_mod.build_scheduler(
        session_factory=pg_session_factory,
        settings=settings,
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=fake_embedder_profiles_fail,
        start=False,
    )
    job = sched.get_job("embeddings:refresh-missing")
    assert job is not None
    try:
        yield job.func
    finally:
        sched.shutdown(wait=False)


def test_a_tick_capped_by_the_profile_half_still_resets_the_call_cursor(
    pg_session_factory,
    fake_embedder_profiles_fail,
    embeddings_tick_profiles_fail,
    tiny_tick,
    tiny_tick_call_cursors,
):
    """G8 (plan Gate 11) — GATE 9c's mirror, the same bug the other way round.

    Corpus: two fresh calls and ten always-failing profiles. At batch_size=1 /
    max_batches=3 the call half runs off the end of its two-row table on the
    third batch of EVERY tick, while the profile half returns a row on every
    batch of all three ticks (3 ticks x 3 batches = 9 examined, one to spare),
    so `examined` is never 0 and every tick stops on the cap.

    Keyed on the JOINT `scan_exhausted`, the scheduler would read "rows still
    ahead" and carry `after_call_id=ids[1]` into every later tick — [0, ids[1],
    ids[1]] — and a call that goes stale behind that frozen cursor would never
    be repaired while the profile half stays capped. The call cursor must reset
    on the CALL half's own exhaustion.
    """
    with pg_session_factory() as s:
        call_ids = [make_call(s, title=f"Fresh call {i}").id for i in range(2)]
        s.commit()
    # Seed the calls current BEFORE the profiles exist, so the corpus starts
    # with the call half genuinely clean and only the profiles are unworkable.
    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=fake_embedder_profiles_fail
    )
    assert all(_call_is_current(pg_session_factory, cid) for cid in call_ids)

    with pg_session_factory() as s:
        profile_ids = [
            make_profile(s, description=f"{_UNEMBEDDABLE_PROFILE} {i}").id for i in range(10)
        ]
        s.commit()

    embeddings_tick_profiles_fail()  # capped by the profiles; call half exhausted on batch 3
    _call_goes_stale(pg_session_factory, call_ids[0])  # behind the call high-water mark
    embeddings_tick_profiles_fail()  # capped by the profiles again
    embeddings_tick_profiles_fail()

    # The premise, checked rather than assumed: the PROFILE half held every
    # tick at the cap, walking its own cursor forward three rows a tick. If
    # this fails, the corpus no longer exercises the bug and the assertions
    # below would pass vacuously.
    assert tiny_tick == [0, profile_ids[2], profile_ids[5]], (
        f"the profile half did not hold every tick at the cap; after_profile_id "
        f"values passed: {tiny_tick}"
    )
    assert tiny_tick_call_cursors == [0, 0, 0], (
        "a tick capped by the PROFILE half must still reset the call cursor once "
        "the call half has run off the end of its own table; after_call_id values "
        f"passed: {tiny_tick_call_cursors}"
    )
    assert _call_is_current(pg_session_factory, call_ids[0]), (
        "the profile half held the tick at max_batches, so the call cursor froze "
        "past the last call and a call that went stale behind it was never repaired"
    )
