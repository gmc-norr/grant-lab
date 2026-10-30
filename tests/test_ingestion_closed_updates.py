"""A source's word that a call has closed must reach the row we already hold.

Both adapters used to DROP a closed call before the orchestrator saw it. So a
call whose deadline the source moved into the past kept our old future deadline
and stayed advertised as open on the dashboard, the digest and the public boards
(outside review 2026-10-29, F5). An adapter now passes such a call on with
`closed=True`, and the orchestrator applies it as an UPDATE ONLY:

* to a row we already hold that is still open (deadline unset or in the future);
* never as a new row (the EU feed alone carries ~590 long-closed topics);
* with no embedding and no ranking work, because nothing can be applied for.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from sqlalchemy import func, select

from grant_lab.db.models import Call, RankingJob
from grant_lab.embeddings.client import EmbedResult
from grant_lab.ingestion.orchestrator import run_ingestion
from grant_lab.sources.types import NormalizedCall
from tests.freshness_helpers import ctx_for

_FUTURE = datetime.now(UTC) + timedelta(days=120)
_PAST = datetime.now(UTC) - timedelta(days=3)


def _embed_client():
    c = MagicMock()
    c.model = "bge-m3"
    c.embed.return_value = EmbedResult(vector=[0.1] * 1024, embedded_chars=1)
    return c


class _StaticAdapter:
    slug = "vinnova"

    def __init__(self, calls):
        self._calls = list(calls)

    def fetch_new_calls(self, since):
        yield from self._calls


def _run(pg_session_factory, calls, embed_client=None):
    return run_ingestion(
        freshness=ctx_for(),
        session_factory=pg_session_factory,
        adapter=_StaticAdapter(calls),
        alerter=MagicMock(),
        embed_client=embed_client or _embed_client(),
    )


def _row(pg_session_factory, external_id):
    with pg_session_factory() as s:
        return s.execute(select(Call).where(Call.external_id == external_id)).scalar_one_or_none()


def _jobs(pg_session_factory):
    with pg_session_factory() as s:
        return s.scalar(select(func.count()).select_from(RankingJob))


def test_a_closed_update_closes_the_open_row_we_hold(pg_session_factory, profile_factory):
    """F5's reproduction: a stored future call, then the same identifier with a
    past deadline and a changed title. The row must take both."""
    profile_factory(name="P-A")
    _run(
        pg_session_factory,
        [
            NormalizedCall(
                external_id="V-1", url="https://example.org/u", title="Open", deadline=_FUTURE
            )
        ],
    )
    jobs_before = _jobs(pg_session_factory)
    embed = _embed_client()

    result = _run(
        pg_session_factory,
        [
            NormalizedCall(
                external_id="V-1",
                url="https://example.org/u",
                title="Corrected",
                deadline=_PAST,
                closed=True,
            )
        ],
        embed_client=embed,
    )

    row = _row(pg_session_factory, "V-1")
    assert (row.title, row.deadline) == ("Corrected", _PAST)
    assert (result.inserted, result.updated, result.closed) == (0, 0, 1)
    assert result.error is None
    # Nothing can be applied for any more, so no embedding and no ranking work.
    embed.embed.assert_not_called()
    assert _jobs(pg_session_factory) == jobs_before


def test_a_closed_call_we_never_stored_is_not_created(pg_session_factory):
    result = _run(
        pg_session_factory,
        [
            NormalizedCall(
                external_id="V-9",
                url="https://example.org/u",
                title="Old",
                deadline=_PAST,
                closed=True,
            )
        ],
    )

    assert _row(pg_session_factory, "V-9") is None
    assert (result.inserted, result.updated, result.closed) == (0, 0, 0)


def test_a_closed_update_to_a_row_that_is_already_closed_writes_nothing(pg_session_factory):
    """Once closed, a row has nothing left to learn from the source. Without
    this, the EU's "-CANCELLED" notice (deadline = the run's own clock) would
    move the stored deadline forward on every run."""
    _run(
        pg_session_factory,
        [
            NormalizedCall(
                external_id="V-2", url="https://example.org/u", title="Closed", deadline=_PAST
            )
        ],
    )

    result = _run(
        pg_session_factory,
        [
            NormalizedCall(
                external_id="V-2",
                url="https://example.org/u",
                title="Changed again",
                deadline=_PAST + timedelta(days=1),
                closed=True,
            )
        ],
    )

    row = _row(pg_session_factory, "V-2")
    assert (row.title, row.deadline) == ("Closed", _PAST)
    assert result.closed == 0


def test_a_closed_update_closes_a_row_with_no_deadline(pg_session_factory):
    """A row with no deadline counts as open everywhere (`deadline IS NULL OR
    deadline > now`), so the source closing it must land too."""
    _run(
        pg_session_factory,
        [NormalizedCall(external_id="V-3", url="https://example.org/u", title="Rolling")],
    )

    result = _run(
        pg_session_factory,
        [
            NormalizedCall(
                external_id="V-3",
                url="https://example.org/u",
                title="Rolling",
                deadline=_PAST,
                closed=True,
            )
        ],
    )

    assert _row(pg_session_factory, "V-3").deadline == _PAST
    assert result.closed == 1


def test_open_calls_in_the_same_run_are_still_inserted_and_ranked(
    pg_session_factory, profile_factory
):
    """The closed path must not swallow the normal one beside it."""
    profile_factory(name="P-A")
    result = _run(
        pg_session_factory,
        [
            NormalizedCall(
                external_id="V-8",
                url="https://example.org/u",
                title="Gone",
                deadline=_PAST,
                closed=True,
            ),
            NormalizedCall(
                external_id="V-4", url="https://example.org/u", title="New", deadline=_FUTURE
            ),
        ],
    )

    assert (result.inserted, result.closed) == (1, 0)
    assert _row(pg_session_factory, "V-4") is not None
    assert _jobs(pg_session_factory) == 1


# --- end to end through the real EU adapter ---------------------------------
#
# The halves above are each pinned on their own; these prove they compose. The
# EU deadlines are relative to the real clock because the orchestrator judges
# "still open" against its own run start.


def _eu_run(pg_session_factory, items):
    from grant_lab.sources.eu_portal import EUPortalAdapter
    from tests.test_sources_eu_portal import _body, _mock_transport

    adapter = EUPortalAdapter(
        base_url="https://api.tech.ec.europa.eu", transport=_mock_transport(body=_body(items))
    )
    return run_ingestion(
        freshness=ctx_for(),
        session_factory=pg_session_factory,
        adapter=adapter,
        alerter=MagicMock(),
        embed_client=_embed_client(),
    )


def _sedia_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT00:00:00.000+0000")


def test_an_eu_deadline_moved_into_the_past_closes_the_stored_call(pg_session_factory):
    from tests.test_sources_eu_portal import _meta

    _eu_run(pg_session_factory, [_meta(deadlineDate=[_sedia_date(_FUTURE)])])
    result = _eu_run(
        pg_session_factory,
        [_meta(deadlineDate=[_sedia_date(_PAST)], title=["Renamed by the portal"])],
    )

    row = _row(pg_session_factory, "HORIZON-HLTH-2026-DISEASE-03-15")
    assert row.title == "Renamed by the portal"
    assert row.deadline < datetime.now(UTC)
    assert result.closed == 1


def test_an_eu_cancelled_copy_closes_the_stored_original(pg_session_factory):
    """The Startup Europe case: the portal serves the original as open AND a
    "-CANCELLED" copy beside it. The original must end up closed and named as
    cancelled, and the copy must not become a row of its own."""
    from tests.test_sources_eu_portal import _meta

    original = "HORIZON-EIE-2027-01-CONNECT-01"
    _eu_run(
        pg_session_factory,
        [
            _meta(
                identifier=[original], title=["Startup Europe"], deadlineDate=[_sedia_date(_FUTURE)]
            )
        ],
    )
    result = _eu_run(
        pg_session_factory,
        [
            _meta(
                identifier=[original], title=["Startup Europe"], deadlineDate=[_sedia_date(_FUTURE)]
            ),
            _meta(
                identifier=[f"{original}-CANCELLED"],
                title=["Startup Europe - CANCELLED"],
                deadlineDate=[_sedia_date(_FUTURE)],
            ),
        ],
    )

    row = _row(pg_session_factory, original)
    assert row.title == "Startup Europe - CANCELLED"
    assert row.deadline <= datetime.now(UTC)
    assert _row(pg_session_factory, f"{original}-CANCELLED") is None
    assert result.closed == 1

    # And the next night's identical feed leaves it closed, not reopened.
    _eu_run(
        pg_session_factory,
        [
            _meta(
                identifier=[original], title=["Startup Europe"], deadlineDate=[_sedia_date(_FUTURE)]
            ),
            _meta(
                identifier=[f"{original}-CANCELLED"],
                title=["Startup Europe - CANCELLED"],
                deadlineDate=[_sedia_date(_FUTURE)],
            ),
        ],
    )
    assert _row(pg_session_factory, original).deadline <= datetime.now(UTC)
