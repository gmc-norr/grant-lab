"""Tests for reconcile_workspace_citekeys (citation provenance Slice 2).

Spec contract: the git ledger (citations.json) is canonical for citekeys; the
references.citekey column is a best-effort DB cache. A best-effort cache write
can fail (or never run), leaving references.citekey stale/NULL. Reconciliation
repairs that drift forward-only, idempotently, and never destructively, and it
must respect the partial unique index on (workspace_id, citekey).
"""

import json
import threading
import time
import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from call_manager.db.models import Reference, Workspace
from call_manager.proposal_assist.citations.ledger import dump_ledger, empty_ledger
from call_manager.proposal_assist.drafting.locking import commit_mutex_key
from call_manager.proposal_assist.reconcile import (
    CITATIONS_FILENAME,
    reconcile_workspace_citekeys,
)
from tests.proposal_assist.conftest import make_workspace


def _seed_ref(pg_session_factory, workspace_id, *, citekey=None) -> uuid.UUID:
    with pg_session_factory() as s:
        ref = Reference(
            workspace_id=workspace_id,
            kind="paper",
            source_type="uploaded",
            filename="seed.pdf",
            storage_path="seed/seed.pdf",
            mime_type="application/pdf",
            citekey=citekey,
            metadata_status="approved",
        )
        s.add(ref)
        s.flush()
        rid = ref.id
        s.commit()
    return rid


def _make_ws(pg_session_factory) -> uuid.UUID:
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        return ws.id


def _write_ledger(repo_dir, rows: list[dict]) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    ledger = {**empty_ledger(), "evidence": rows}
    (repo_dir / CITATIONS_FILENAME).write_text(dump_ledger(ledger))


def _row(*, ev_id, citekey, reference_id) -> dict:
    return {"id": ev_id, "citekey": citekey, "reference_id": reference_id}


def test_backfills_null_db_citekey_from_ledger(pg_session_factory, tmp_path):
    wsid = _make_ws(pg_session_factory)
    rid = _seed_ref(pg_session_factory, wsid, citekey=None)
    repo_dir = tmp_path / "repo"
    _write_ledger(repo_dir, [_row(ev_id="ev-aaaaaa", citekey="smith2021", reference_id=str(rid))])

    n = reconcile_workspace_citekeys(pg_session_factory, wsid, repo_dir)
    assert n == 1
    with pg_session_factory() as s:
        assert s.get(Reference, rid).citekey == "smith2021"

    # Idempotent: a second run repairs nothing.
    n2 = reconcile_workspace_citekeys(pg_session_factory, wsid, repo_dir)
    assert n2 == 0


def test_noop_when_db_matches_ledger(pg_session_factory, tmp_path):
    wsid = _make_ws(pg_session_factory)
    rid = _seed_ref(pg_session_factory, wsid, citekey="smith2021")
    repo_dir = tmp_path / "repo"
    _write_ledger(repo_dir, [_row(ev_id="ev-aaaaaa", citekey="smith2021", reference_id=str(rid))])

    n = reconcile_workspace_citekeys(pg_session_factory, wsid, repo_dir)
    assert n == 0
    with pg_session_factory() as s:
        assert s.get(Reference, rid).citekey == "smith2021"


def test_skips_when_another_ref_holds_target_citekey(pg_session_factory, tmp_path):
    """A genuine conflict (the unique index would reject) must be skipped, not
    crash; resolution is left to a human / Slice 3."""
    wsid = _make_ws(pg_session_factory)
    rid1 = _seed_ref(pg_session_factory, wsid, citekey=None)
    # A DIFFERENT reference already owns the target citekey in the DB.
    rid2 = _seed_ref(pg_session_factory, wsid, citekey="smith2021")
    repo_dir = tmp_path / "repo"
    _write_ledger(repo_dir, [_row(ev_id="ev-aaaaaa", citekey="smith2021", reference_id=str(rid1))])

    n = reconcile_workspace_citekeys(pg_session_factory, wsid, repo_dir)
    assert n == 0
    with pg_session_factory() as s:
        assert s.get(Reference, rid1).citekey is None  # untouched
        assert s.get(Reference, rid2).citekey == "smith2021"  # untouched


def test_skips_reference_with_conflicting_ledger_citekeys(pg_session_factory, tmp_path):
    """If a reference_id maps to two DISTINCT citekeys in the ledger, that's
    corruption — skip that reference_id (don't guess)."""
    wsid = _make_ws(pg_session_factory)
    rid = _seed_ref(pg_session_factory, wsid, citekey=None)
    repo_dir = tmp_path / "repo"
    _write_ledger(
        repo_dir,
        [
            _row(ev_id="ev-aaaaaa", citekey="smith2021", reference_id=str(rid)),
            _row(ev_id="ev-bbbbbb", citekey="jones2020", reference_id=str(rid)),
        ],
    )

    n = reconcile_workspace_citekeys(pg_session_factory, wsid, repo_dir)
    assert n == 0
    with pg_session_factory() as s:
        assert s.get(Reference, rid).citekey is None


def test_multiple_rows_same_citekey_repairs_once(pg_session_factory, tmp_path):
    wsid = _make_ws(pg_session_factory)
    rid = _seed_ref(pg_session_factory, wsid, citekey=None)
    repo_dir = tmp_path / "repo"
    _write_ledger(
        repo_dir,
        [
            _row(ev_id="ev-aaaaaa", citekey="smith2021", reference_id=str(rid)),
            _row(ev_id="ev-bbbbbb", citekey="smith2021", reference_id=str(rid)),
        ],
    )

    n = reconcile_workspace_citekeys(pg_session_factory, wsid, repo_dir)
    assert n == 1
    with pg_session_factory() as s:
        assert s.get(Reference, rid).citekey == "smith2021"


def test_missing_citations_json_returns_zero(pg_session_factory, tmp_path):
    wsid = _make_ws(pg_session_factory)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    n = reconcile_workspace_citekeys(pg_session_factory, wsid, repo_dir)
    assert n == 0


def test_corrupt_citations_json_returns_zero(pg_session_factory, tmp_path):
    wsid = _make_ws(pg_session_factory)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / CITATIONS_FILENAME).write_text("{ not valid json")
    n = reconcile_workspace_citekeys(pg_session_factory, wsid, repo_dir)
    assert n == 0


def test_skips_missing_reference(pg_session_factory, tmp_path):
    """Ledger references an id not in the DB (e.g. hard-deleted) → skip, no crash."""
    wsid = _make_ws(pg_session_factory)
    repo_dir = tmp_path / "repo"
    ghost = str(uuid.uuid4())
    _write_ledger(repo_dir, [_row(ev_id="ev-aaaaaa", citekey="smith2021", reference_id=ghost)])
    n = reconcile_workspace_citekeys(pg_session_factory, wsid, repo_dir)
    assert n == 0


def test_rows_without_reference_id_or_citekey_ignored(pg_session_factory, tmp_path):
    wsid = _make_ws(pg_session_factory)
    rid = _seed_ref(pg_session_factory, wsid, citekey=None)
    repo_dir = tmp_path / "repo"
    rows = [
        {"id": "ev-aaaaaa"},  # no citekey/reference_id
        {"id": "ev-bbbbbb", "citekey": "x2021"},  # no reference_id
        _row(ev_id="ev-cccccc", citekey="smith2021", reference_id=str(rid)),
    ]
    # write directly (bypass _row helper validation, mirror a hand-edited ledger)
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / CITATIONS_FILENAME).write_text(
        json.dumps({"version": 2, "evidence": rows}, indent=2) + "\n"
    )
    n = reconcile_workspace_citekeys(pg_session_factory, wsid, repo_dir)
    assert n == 1
    with pg_session_factory() as s:
        assert s.get(Reference, rid).citekey == "smith2021"


def test_reconcile_citekeys_repairs_nothing_on_a_soft_deleted_workspace(
    pg_session_factory, tmp_path
):
    """references.citekey is user-visible. A dead workspace must not be mutated."""
    wsid = _make_ws(pg_session_factory)
    ref_id = _seed_ref(pg_session_factory, wsid, citekey=None)
    repo_dir = tmp_path / "repo"
    _write_ledger(
        repo_dir, [_row(ev_id="ev-aaaaaa", citekey="smith2021", reference_id=str(ref_id))]
    )

    with pg_session_factory() as s:
        ws = s.get(Workspace, wsid)
        ws.soft_deleted_at = datetime.now(UTC)
        s.commit()

    repaired = reconcile_workspace_citekeys(pg_session_factory, wsid, repo_dir)

    assert repaired == 0
    with pg_session_factory() as s:
        assert s.get(Reference, ref_id).citekey is None, (
            "a dead workspace's user-visible column was mutated"
        )


def test_reconcile_citekeys_waits_for_the_commit_mutex_key(pg_session_factory, tmp_path):
    """The lease gate. Structure and reasoning are identical to
    test_reconcile_git_drift_waits_for_the_commit_mutex_key in
    tests/proposal_assist/test_reconcile.py — read that test's docstring; the
    observation must be made WHILE the holder is in force.
    """
    wsid = _make_ws(pg_session_factory)
    ref_id = _seed_ref(pg_session_factory, wsid, citekey=None)
    repo_dir = tmp_path / "repo"
    _write_ledger(
        repo_dir, [_row(ev_id="ev-aaaaaa", citekey="smith2021", reference_id=str(ref_id))]
    )

    done = threading.Event()

    def _reconcile():
        try:
            reconcile_workspace_citekeys(pg_session_factory, wsid, repo_dir)
        finally:
            done.set()

    holder = pg_session_factory()
    observed: list[str | None] = []
    t = threading.Thread(target=_reconcile)
    try:
        holder.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": commit_mutex_key(wsid)}
        )
        t.start()
        time.sleep(1.5)
        with pg_session_factory() as probe:
            observed.append(probe.get(Reference, ref_id).citekey)
    finally:
        holder.rollback()
        holder.close()
        assert done.wait(timeout=30), "reconcile never finished"
        t.join(timeout=5)

    assert observed == [None], (
        f"while the commit-mutex key was held exclusively the citekey was already "
        f"{observed!r} — reconcile did not take the shared lease"
    )
    with pg_session_factory() as s:
        assert s.get(Reference, ref_id).citekey == "smith2021"


def test_reconcile_citekeys_still_repairs_a_live_workspace(pg_session_factory, tmp_path):
    """Regression guard: the fence must not break the case reconcile exists for."""
    wsid = _make_ws(pg_session_factory)
    ref_id = _seed_ref(pg_session_factory, wsid, citekey=None)
    repo_dir = tmp_path / "repo"
    _write_ledger(
        repo_dir, [_row(ev_id="ev-aaaaaa", citekey="smith2021", reference_id=str(ref_id))]
    )

    repaired = reconcile_workspace_citekeys(pg_session_factory, wsid, repo_dir)
    assert repaired == 1
    with pg_session_factory() as s:
        assert s.get(Reference, ref_id).citekey == "smith2021"
