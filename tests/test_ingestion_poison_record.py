"""One upstream field-type change was a permanent, silent ingestion outage.

`upsert.py`'s bounded-column guards call `len()` on values the adapters pass
through from upstream JSON with no `str()` and no type check. Measured on this
codebase before the fix:

    currency=0     -> TypeError: object of type 'int' has no len()
    external_id=12345 -> TypeError: object of type 'int' has no len()
    url=9          -> AttributeError: 'int' object has no attribute 'lower'
    programme={..} -> PASSES the length guard (len of a 1-key dict is 1) and
                      reaches a VARCHAR(200)
    budget_min, deadline, keywords, raw, summary, title -> all reach the DB
                      layer with a type their column cannot take

`orchestrator.py:189-196` advances `sources.last_fetched_at` INSIDE the same
transaction as the upsert loop, so any escape rolls it back. The next tick
re-fetches from the same `since`, hits the same item, fails identically —
forever, discarding every other call in the batch on every tick. That is exactly
the poison record the guards' own comment claims to prevent.

The rule these tests pin: `upsert_call` must never raise on a well-formed
`NormalizedCall` whatever the field TYPES are. It either persists a sanitized
row or skips the record — the two outcomes the module already had.
"""

import pytest
from sqlalchemy import select

from grant_lab.db.models import Call, Source
from grant_lab.ingestion.upsert import upsert_call
from grant_lab.sources.types import NormalizedCall


def _sid(s):
    return s.execute(select(Source.id).where(Source.slug == "vinnova")).scalar_one()


def _call(**over):
    kw = {"external_id": "P-1", "url": "https://example.org/u", "title": "T"}
    kw.update(over)
    return NormalizedCall(**kw)


# Identity columns: a non-string cannot be sanitized without changing WHICH call
# this is, so the record is rejected outright — the same policy the module
# already applies to an over-length external_id and an unsafe URL.
@pytest.mark.parametrize(
    ("field", "value"),
    [("external_id", 12345), ("external_id", None), ("url", 9), ("url", {"a": 1})],
)
def test_a_bad_identity_field_skips_the_record_instead_of_raising(field, value, pg_session_factory):
    with pg_session_factory() as s:
        assert upsert_call(s, source_id=_sid(s), call=_call(**{field: value})) is False
        s.commit()
        assert s.execute(select(Call).where(Call.source_id == _sid(s))).first() is None


# Metadata columns: nullable, non-identity. Coerced so the CALL STILL INGESTS —
# the policy the existing currency/programme guards already state.
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("currency", 0),
        ("currency", {"a": 1}),
        ("programme", {"a": 1}),
        ("summary", 5),
        ("full_text", 5),
        ("title", 7),
        ("raw", "notadict"),
        ("budget_min", "abc"),
        ("budget_max", "abc"),
        # Past PostgreSQL BIGINT (outside review 2026-10-29, F6): a plain Python int
        # reached SQL and NumericValueOutOfRange aborted the whole transaction.
        ("budget_max", 10**30),
        ("budget_min", -(10**30)),
        ("budget_max", 2**63),
        ("budget_max", "1" + "0" * 30),
        ("deadline", "2026-01-01"),
        ("published_at", "2026-01-01"),
    ],
)
def test_a_bad_metadata_field_still_ingests_the_call(field, value, pg_session_factory):
    with pg_session_factory() as s:
        sid = _sid(s)
        assert upsert_call(s, source_id=sid, call=_call(**{field: value})) is True
        s.commit()
        row = s.execute(select(Call).where(Call.external_id == "P-1")).scalar_one()
        # Whatever landed must be a type the column can hold — proven by the
        # commit above succeeding, and re-read here so a silently-NULLed
        # identity field cannot pass.
        assert row.external_id == "P-1"
        assert row.url == "https://example.org/u"


def test_a_poison_record_does_not_take_the_batch_with_it(pg_session_factory):
    # The harm was never one lost call — it was the OTHER calls in the batch,
    # discarded on every tick because the shared transaction rolled back.
    with pg_session_factory() as s:
        sid = _sid(s)
        good_a = _call(external_id="P-GOOD-A")
        poison = _call(external_id="P-2", currency=0)
        good_b = _call(external_id="P-GOOD-B")
        for c in (good_a, poison, good_b):
            upsert_call(s, source_id=sid, call=c)
        s.commit()
        ids = set(
            s.execute(select(Call.external_id).where(Call.source_id == sid)).scalars().all()
        )
    assert {"P-GOOD-A", "P-GOOD-B"} <= ids


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # A bare string is ITERABLE, so `list(value)` would silently persist
        # ["a", ",", "b"] — a valid ARRAY(Text) that no assertion about
        # "the call still ingested" can catch. It is not a keyword list; drop it.
        ("a,b", []),
        (["ok", 2, None], ["ok", "2", ""]),  # ARRAY(Text) needs every ELEMENT typed
        (None, []),
        ({"a": 1}, []),
    ],
)
def test_keywords_are_a_list_of_strings_or_nothing(value, expected, pg_session_factory):
    with pg_session_factory() as s:
        sid = _sid(s)
        assert upsert_call(s, source_id=sid, call=_call(keywords=value)) is True
        s.commit()
        row = s.execute(select(Call).where(Call.external_id == "P-1")).scalar_one()
        assert row.keywords == expected


def test_raw_is_a_dict_or_nothing(pg_session_factory):
    # JSONB accepts a bare string, so this one persists happily and wrong:
    # `call.structured` (models.py) hands `raw` to the criteria module as a dict.
    with pg_session_factory() as s:
        sid = _sid(s)
        assert upsert_call(s, source_id=sid, call=_call(raw="notadict")) is True
        s.commit()
        assert s.execute(select(Call).where(Call.external_id == "P-1")).scalar_one().raw == {}


def test_a_budget_at_the_bigint_limits_is_kept_as_is(pg_session_factory):
    """The range check must not eat the largest values the column CAN hold."""
    with pg_session_factory() as s:
        sid = _sid(s)
        upsert_call(s, source_id=sid, call=_call(budget_min=-(2**63), budget_max=2**63 - 1))
        s.commit()
        row = s.execute(select(Call).where(Call.external_id == "P-1")).scalar_one()
    assert (row.budget_min, row.budget_max) == (-(2**63), 2**63 - 1)


def test_an_out_of_range_budget_does_not_take_the_batch_with_it(pg_session_factory):
    """F6's reproduction: one valid call, then one with budget_max=10**30. The
    valid call was rolled back with it, every run, forever."""
    with pg_session_factory() as s:
        sid = _sid(s)
        upsert_call(s, source_id=sid, call=_call(external_id="P-GOOD"))
        upsert_call(s, source_id=sid, call=_call(external_id="P-HUGE", budget_max=10**30))
        s.commit()
        rows = dict(
            s.execute(select(Call.external_id, Call.budget_max).where(Call.source_id == sid)).all()
        )
    assert rows == {"P-GOOD": None, "P-HUGE": None}
