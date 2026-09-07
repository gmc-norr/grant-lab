# Decision Notes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a researcher write a free-text reason on a call they hid or saved, and put those reasons in front of whoever labels the golden set.

**Architecture:** `call_decisions.note` already exists (migration 0071) and nothing has ever written it, so there is **no migration**. A new `PUT /api/calls/{id}/note` writes it. `GET /api/calls` reads it back on the `saved`/`hidden` views only, off a `CallDecision` join `build_dashboard_view` already makes for its `ORDER BY`. The golden-set miner grows a `user_notes` list, unattributed. The frontend gains an ignorable strip in the triage feed, note display in the Saved/Hidden rows and on the call page, and — because the deployment is `match` only — the golden-set admin page has to be mounted in that edition first.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, PostgreSQL 16, pytest + testcontainers. React 19 + Vite, TanStack Query, `openapi-fetch`, Mantine, vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-07-decision-notes-design.md` (rev 3, commit `0ea0e3cc`). Task numbers below cite its decisions (D1–D10) and gates (§7 G1–G13).

## Global Constraints

- **No migration.** `call_decisions.note` is `Text`, nullable, created by `alembic/versions/0071_call_decisions.py`. Do not write one. If you believe you need one, you have misread the spec.
- **The deployment is the `match` edition only.** `full` is the next version. Anything gated full-only is not in production.
- **Baseline: `5484` tests collected** on master `7afd8c23`, measured 2026-09-07 with `uv run pytest --collect-only -q`. Carry DELTAS against this. Re-measure before asserting any total.
- **`1 skipped` stays exactly 1.** That skip is a real-SDK test gated on `ANTHROPIC_API_KEY`. `0 skipped` means a live paid API call ran. **Never export `ANTHROPIC_API_KEY`.**
- **Never read, write, copy or back up `/home/parlar_ai/dev/grant-lab/.env`.** It holds a live API key. Nothing in this plan needs it. Set env vars inline in the one command that needs them.
- **`docs/superpowers/specs/` is byte-identical at every commit.** The spec is INPUT. If the spec is wrong, that is a note in the commit message, not an edit.
- **Fast local run:** `uv run pytest -n 16 --dist loadfile` (~1m). Never `-n auto` — `psutil` is absent here and xdist resolves it to 72 workers.
- **Judge on FAILED, never ERROR.** A broken import reddens a whole suite as collection ERRORs with zero FAILED, which reads like a passing suite.
- **No new npm packages.** `minimumReleaseAge: 10080` (seven days) in `frontend/pnpm-workspace.yaml` blocks `pnpm add` even for old versions. There is no toast library and you are not adding one.
- **`pnpm run build` is the only typechecker.** vitest does not typecheck. Every task touching TypeScript ends with `pnpm run build`.
- **Naming, verbatim (D6):** response field `decision_note`; request body field `note`; golden-set field `user_notes`. Never `note` on `CallOut` (it already carries `applicant_note_count`, a different thing), never `reason` on the candidate (that already means the mining reason).
- **`max_length=1000`** on the request model (D8). Whitespace-only becomes `NULL`, never `""`.
- **Regenerate `docs/ARCHITECTURE.md`** in the same commit as any structural move: `uv run python scripts/gen_architecture.py`. Do not hand-edit it.

## File Structure

| File | Change | Responsibility |
| --- | --- | --- |
| `src/grant_lab/web/api/calls.py` | modify | the note endpoint, `CallOut.decision_note`, `note=None` in both upserts |
| `src/grant_lab/web/dashboard.py` | modify | select the note in the `decision_ordered` branch; widen the row tuple |
| `src/grant_lab/ranking/golden_set.py` | modify | `CandidateRow.user_notes`, mined from hidden decisions only |
| `src/grant_lab/web/api/admin_golden_set.py` | modify | `CandidateOut.user_notes` — the serialization boundary |
| `src/grant_lab/web/api/__init__.py` | modify | move `admin_golden_set.router` into `_ALWAYS` |
| `tests/architecture/test_edition_surface.py` | modify | drop the golden-set prefix; re-measure its inline comment |
| `tests/architecture/test_match_gates_are_wired.py` | modify | the scenario manifest string |
| `frontend/src/router.simple.tsx` | modify | mount `admin/golden-set` |
| `frontend/src/routes/AdminNav.tsx` | modify | drop it from `MATCH_EXCLUDED` |
| `frontend/src/api/calls.ts` | modify | `useSetDecisionNote` |
| `frontend/src/routes/DecisionNoteStrip.tsx` | **create** | the strip: one job, D10's contract |
| `frontend/src/routes/TriageFeed.tsx` | modify | own the strip's state, render it |
| `frontend/src/routes/TriageCard.tsx` | modify | show/edit the note in Saved and Hidden |
| `frontend/src/routes/CallDetail.tsx` | modify | note beside the existing hide/undo |
| `frontend/src/routes/AdminGoldenSet.tsx` | modify | render `user_notes` under the reason |
| `frontend/e2e/match.spec.ts` | modify | the admin subnav scenario |
| `tests/test_api_calls_note.py` | **create** | G2–G5 |
| `tests/web/test_calls_decision_note_projection.py` | **create** | G1, G8, G9 |
| `tests/test_golden_set_notes.py` | **create** | G7, G12 |
| `frontend/src/routes/DecisionNoteStrip.test.tsx` | **create** | G10, G11 |

---

### Task 1: The note endpoint

**Files:**
- Modify: `src/grant_lab/web/api/calls.py`
- Test: `tests/test_api_calls_note.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `PUT /api/calls/{call_id}/note?profile_id=N`, body `{"note": str | null}`, response `{"decision_note": str | null}`. Later tasks read `decision_note` off `CallOut`, which is a different object — do not conflate them.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_calls_note.py`. Copy the `_seed_call`, `_setup` and `_login` helpers from `tests/test_api_calls_hide.py` verbatim — that file is the house pattern for this endpoint family, and a divergent fixture is how two tests of the same endpoint stop testing the same thing.

```python
"""Gates for PUT /api/calls/{id}/note (spec 2026-09-07 decision notes, §7 G2-G5).

The note hangs off a decision row that must already exist. Every gate here
therefore creates the decision first -- a fixture that skips that step tests
the 404 arm and nothing else.
"""

from __future__ import annotations

from sqlalchemy import select

from grant_lab.db.models import CallDecision

# _seed_call, _setup and _login are copied from tests/test_api_calls_hide.py.
# Keep them identical; if that file's fixture changes, change this one too.
from tests.test_api_calls_hide import _login, _seed_call, _setup


def test_note_round_trips_on_a_hidden_call(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        r = client.put(
            f"/api/calls/{cid}/note",
            params={"profile_id": pid},
            json={"note": "wrong field - this is materials science"},
        )
    assert r.status_code == 200
    assert r.json() == {"decision_note": "wrong field - this is materials science"}
    with pg_session_factory() as s:
        row = s.execute(
            select(CallDecision).where(
                CallDecision.user_id == uid, CallDecision.call_id == cid
            )
        ).scalar_one()
    assert row.note == "wrong field - this is materials science"


def test_note_404s_when_there_is_no_decision(monkeypatch, pg_url, pg_session_factory):
    """G2. The endpoint must not INVENT a decision to hang the note on: `state`
    is NOT NULL and constrained to saved|hidden, so it would have to guess."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    with client:
        r = client.put(
            f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": "hi"}
        )
    assert r.status_code == 404
    with pg_session_factory() as s:
        rows = s.execute(select(CallDecision)).scalars().all()
    # The row count is the gate. A 404 that still wrote is a 404 that lied.
    assert rows == []


def test_a_note_write_does_not_move_decided_at(monkeypatch, pg_url, pg_session_factory):
    """G4. Both existing decision endpoints DO set decided_at=func.now() in
    their upsert, so copying one of them is the natural mistake -- and the
    Saved/Hidden lists are ordered by decided_at, so it would reorder the list
    under a user as they annotate it."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        with pg_session_factory() as s:
            before = s.execute(
                select(CallDecision.decided_at).where(CallDecision.user_id == uid)
            ).scalar_one()
        client.put(
            f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": "later"}
        )
    with pg_session_factory() as s:
        after = s.execute(
            select(CallDecision.decided_at).where(CallDecision.user_id == uid)
        ).scalar_one()
    assert after == before


def test_whitespace_only_becomes_null_not_empty_string(
    monkeypatch, pg_url, pg_session_factory
):
    """D8. Otherwise "has a note" has two answers and the golden-set list
    renders blank entries that consumed a slot in the cap."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        r = client.put(
            f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": "   "}
        )
    assert r.status_code == 200
    assert r.json() == {"decision_note": None}
    with pg_session_factory() as s:
        row = s.execute(
            select(CallDecision).where(CallDecision.user_id == uid)
        ).scalar_one()
    assert row.note is None


def test_note_over_1000_chars_is_rejected(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        r = client.put(
            f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": "x" * 1001}
        )
    assert r.status_code == 422


def test_note_requires_auth(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    from fastapi.testclient import TestClient

    with TestClient(app) as anon:
        r = anon.put(f"/api/calls/{cid}/note", params={"profile_id": 1}, json={"note": "x"})
    assert r.status_code == 401
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/parlar_ai/dev/grant-lab
uv run pytest tests/test_api_calls_note.py -v
```

Expected: every test FAILS. The round-trip and 404 tests fail with `405 Method Not Allowed` (the path exists for no verb) or `404` from the router, not from your handler. **If you see collection ERRORs instead of FAILED, the import of the helpers from `test_api_calls_hide` is wrong — fix that first. An ERROR is not the red you wanted.**

- [ ] **Step 3: Add the request/response models**

In `src/grant_lab/web/api/calls.py`, immediately after the `CallDecisionOut` class (it ends with `state: Literal["saved", "hidden"] | None`), add:

```python
class DecisionNoteIn(BaseModel):
    """The user's own words about one decision.

    The field is `note`, not `decision_note`, and the asymmetry with
    `CallOut.decision_note` is deliberate (D6). The collision that forces the
    longer name exists on `CallOut`, which carries `applicant_note_count` --
    the MODEL's inferred things-to-check -- right beside it. A single-field body
    on an endpoint already named /note has no such neighbour.
    """

    note: str | None = Field(default=None, max_length=1000)


class DecisionNoteOut(BaseModel):
    decision_note: str | None
```

- [ ] **Step 4: Add the endpoint**

In the same file, immediately after the `unsave_call` function, add:

```python
@router.put("/calls/{call_id}/note", response_model=DecisionNoteOut)
def set_decision_note(
    request: Request,
    call_id: int,
    body: DecisionNoteIn,
    profile_id: int = Query(...),
    user: User = Depends(get_current_user),  # noqa: B008
):
    """Write the user's reason onto an EXISTING decision.

    404, not an upsert, when there is no decision row (D5): `state` is NOT NULL
    and constrained to saved|hidden, so creating one here would make this
    endpoint guess a decision the user did not make.

    DOES NOT TOUCH `decided_at`, `decided_by` or `state`. A note is not a new
    decision, and the Saved/Hidden lists are ordered by `decided_at` (Phase 2
    decision D12) -- bumping it would silently reorder the list under a user as
    they annotate it. Both neighbouring endpoints DO set decided_at in their
    upsert, which is exactly why this is written as a plain attribute
    assignment: SQLAlchemy emits an UPDATE for the changed column only.

    Whitespace-only is stored as NULL (D8), so "has a note" has one answer.
    """
    sf = request.app.state.session_factory
    with sf() as session:
        if session.get(Call, call_id) is None:
            raise HTTPException(status_code=404, detail="call not found")
        _readable_active_profile_or_404(session, user, profile_id)
        row = session.scalar(
            select(CallDecision).where(
                CallDecision.user_id == user.id,
                CallDecision.profile_id == profile_id,
                CallDecision.call_id == call_id,
            )
        )
        if row is None:
            raise HTTPException(status_code=404, detail="decision not found")
        note = (body.note or "").strip() or None
        row.note = note
        session.commit()
    return DecisionNoteOut(decision_note=note)
```

`CallDecision` is already imported in this file's `from grant_lab.db.models import (...)` block; `select`, `Field`, `Query` and `HTTPException` are already imported. Verify with `grep -n 'CallDecision' src/grant_lab/web/api/calls.py | head -3` before assuming.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/test_api_calls_note.py -v
```

Expected: 6 passed.

- [ ] **Step 6: Run the adverse mutation for G4**

The gate is only a gate if it can go red. Add `decided_at=func.now()` to the write and confirm `test_a_note_write_does_not_move_decided_at` fails:

```bash
cd /home/parlar_ai/dev/grant-lab
cp src/grant_lab/web/api/calls.py /tmp/calls.py.bak
trap 'cp /tmp/calls.py.bak /home/parlar_ai/dev/grant-lab/src/grant_lab/web/api/calls.py' EXIT
python3 - <<'PY'
from pathlib import Path
p = Path("/home/parlar_ai/dev/grant-lab/src/grant_lab/web/api/calls.py")
s = p.read_text()
old = "        row.note = note\n        session.commit()"
new = "        row.note = note\n        row.decided_at = func.now()\n        session.commit()"
assert s.count(old) == 1
p.write_text(s.replace(old, new))
PY
uv run pytest tests/test_api_calls_note.py::test_a_note_write_does_not_move_decided_at -v
```

Expected: **FAILED**. Then let the trap restore, and confirm:

```bash
git diff --stat src/grant_lab/web/api/calls.py
```

Expected: the only changes are yours from steps 3–4. **Use the absolute path in the trap** — a relative path in a trap that runs after a `cd` silently fails and leaves the mutant in the tree.

- [ ] **Step 7: Commit**

```bash
git add src/grant_lab/web/api/calls.py tests/test_api_calls_note.py
git commit -m "feat(calls): let a researcher say why, on a decision they already made

PUT /api/calls/{id}/note writes call_decisions.note -- a column migration
0071 created for exactly this and nothing has written since.

404 rather than upsert when there is no decision (state is NOT NULL and
constrained to saved|hidden, so creating one would guess). Plain attribute
assignment rather than an upsert so decided_at cannot move: both
neighbouring endpoints do bump it, and the Saved/Hidden lists order by it.
Whitespace-only stores NULL so \"has a note\" has one answer.

The G4 gate was run against a decided_at=func.now() mutant and reddened."
```

---

### Task 2: A flip clears the note

**Files:**
- Modify: `src/grant_lab/web/api/calls.py` (the two `on_conflict_do_update` calls)
- Test: `tests/test_api_calls_note.py` (extend)

**Interfaces:**
- Consumes: Task 1's endpoint.
- Produces: no new names. Behaviour only.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_calls_note.py`:

```python
def test_hiding_a_saved_call_clears_its_note(monkeypatch, pg_url, pg_session_factory):
    """G5, D9. Today the upserts leave `note` out of their set_, so a note
    written while saving survives into the hide. The code already calls a flip
    a NEW decision -- it moves decided_at and decided_by for that reason -- so
    carrying the old decision's reasoning into it is wrong, and D7 would then
    date that note by the new decision."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/save", params={"profile_id": pid})
        client.put(
            f"/api/calls/{cid}/note",
            params={"profile_id": pid},
            json={"note": "ask Anna before the deadline"},
        )
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
    with pg_session_factory() as s:
        row = s.execute(
            select(CallDecision).where(CallDecision.user_id == uid)
        ).scalar_one()
    assert (row.state, row.note) == ("hidden", None)


def test_saving_a_hidden_call_clears_its_note(monkeypatch, pg_url, pg_session_factory):
    """The other direction. Written separately, not parametrized: dropping
    note=None from ONE of the two set_ blocks must redden exactly one of these
    two tests, and a parametrized pair makes that impossible to see."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        client.put(
            f"/api/calls/{cid}/note",
            params={"profile_id": pid},
            json={"note": "budget far too small"},
        )
        client.post(f"/api/calls/{cid}/save", params={"profile_id": pid})
    with pg_session_factory() as s:
        row = s.execute(
            select(CallDecision).where(CallDecision.user_id == uid)
        ).scalar_one()
    assert (row.state, row.note) == ("saved", None)


def test_unhiding_destroys_the_note(monkeypatch, pg_url, pg_session_factory):
    """G3, D4. Undecided is the ABSENCE of a row, so the note dies with the
    decision. Must reject a soft-delete: re-hiding starts blank."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        client.put(
            f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": "no"}
        )
        client.delete(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        r = client.put(
            f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": None}
        )
    assert r.json() == {"decision_note": None}
    with pg_session_factory() as s:
        row = s.execute(select(CallDecision)).scalar_one()
    assert row.note is None
```

- [ ] **Step 2: Run to verify the two flip tests fail**

```bash
uv run pytest tests/test_api_calls_note.py -v -k "clears_its_note or unhiding"
```

Expected: the two `clears_its_note` tests FAIL (the note survives). `test_unhiding_destroys_the_note` should already PASS — the DELETE removes the row, so nothing to fix. That asymmetry is expected and correct: it is a regression guard, not a new behaviour.

- [ ] **Step 3: Add `note=None` to both upserts**

In `hide_call`, change the `set_` dict:

```python
            .on_conflict_do_update(
                index_elements=["user_id", "profile_id", "call_id"],
                # FOUR columns now. `note` because a flip IS a new decision --
                # this block already moves decided_at and decided_by for that
                # reason -- and one decision's reasoning must not be filed
                # against a different one (D9). Without it, hide->save->hide
                # resurrects a note about a state the user has twice left, and
                # hands it to the golden set as fresh evidence.
                set_={
                    "state": "hidden",
                    "decided_at": func.now(),
                    "decided_by": user.id,
                    "note": None,
                },
            )
```

In `save_call`, the same, with `"state": "saved"`:

```python
            .on_conflict_do_update(
                index_elements=["user_id", "profile_id", "call_id"],
                # See hide_call: `note` is cleared because a flip is a new
                # decision (D9).
                set_={
                    "state": "saved",
                    "decided_at": func.now(),
                    "decided_by": user.id,
                    "note": None,
                },
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_api_calls_note.py -v
```

Expected: 9 passed.

- [ ] **Step 5: Run the adverse mutation — one `set_` at a time**

```bash
cd /home/parlar_ai/dev/grant-lab
cp src/grant_lab/web/api/calls.py /tmp/calls2.bak
trap 'cp /tmp/calls2.bak /home/parlar_ai/dev/grant-lab/src/grant_lab/web/api/calls.py' EXIT
python3 - <<'PY'
from pathlib import Path
p = Path("/home/parlar_ai/dev/grant-lab/src/grant_lab/web/api/calls.py")
s = p.read_text()
old = '''                set_={
                    "state": "hidden",
                    "decided_at": func.now(),
                    "decided_by": user.id,
                    "note": None,
                },'''
new = '''                set_={
                    "state": "hidden",
                    "decided_at": func.now(),
                    "decided_by": user.id,
                },'''
assert s.count(old) == 1
p.write_text(s.replace(old, new))
PY
uv run pytest tests/test_api_calls_note.py -v -k "clears_its_note"
```

Expected: `test_hiding_a_saved_call_clears_its_note` **FAILED**, `test_saving_a_hidden_call_clears_its_note` **PASSED**. Exactly one red. If both go red, the two tests are not independent and one of them is testing the wrong direction.

- [ ] **Step 6: Commit**

```bash
git add src/grant_lab/web/api/calls.py tests/test_api_calls_note.py
git commit -m "fix(calls): a flip is a new decision, so it clears the old reason

hide/save upserts left \`note\` out of their set_, so a note written while
saving survived into the hide -- one decision's reasoning filed against a
different one. Both blocks already move decided_at and decided_by because
a flip IS a new decision; note now moves with them.

Without this, hide->save->hide resurrects a note about a state the user
has twice left and hands it to the golden-set miner as fresh evidence.

Mutation: dropping note=None from the hide block alone reddens exactly
one of the two direction tests, which is why they are not parametrized."
```

---

### Task 3: `decision_note` on the list response

**Files:**
- Modify: `src/grant_lab/web/dashboard.py`
- Modify: `src/grant_lab/web/api/calls.py`
- Test: `tests/web/test_calls_decision_note_projection.py` (create)
- Test: `tests/web/test_calls_list_query_count.py` (extend)

**Interfaces:**
- Consumes: Task 1's stored note.
- Produces: `DashboardView.calls: list[tuple[Call, CallRanking | None, str | None]]` — a **3-tuple**. `CallOut.decision_note: str | None`. Two unpack sites in `calls.py` change with it.

- [ ] **Step 1: Write the failing tests**

Create `tests/web/test_calls_decision_note_projection.py`:

```python
"""The note is projected on saved/hidden and NOWHERE else (§7 G1, G8, G9).

G1 asserts against the COMPILED SQL, not against a null in the response. A
populated control shows why: an inbox row has no decision row either way, so
`decision_note is None` is true with or without a needless subquery. A null
assertion cannot see the thing it is meant to test.
"""

from __future__ import annotations

import re

from sqlalchemy import select

from grant_lab.db.models import CallDecision
from tests.test_api_calls_hide import _login, _seed_call, _setup


def _projects_note(view: str, *, profile_id: int | None) -> bool:
    """Compile the statement build_dashboard_view would run for one view."""
    from grant_lab.web.dashboard import _decision_predicate  # noqa: F401

    # Word boundaries: "call_decisions.note" must not be matched by a column
    # whose name merely starts with it. `calls.embedding` matching
    # `calls.embedding_model` is a trap this repo has hit once already.
    raise NotImplementedError("replaced in step 3 by a captured-SQL helper")


def test_hidden_view_returns_the_note(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        client.put(
            f"/api/calls/{cid}/note",
            params={"profile_id": pid},
            json={"note": "wrong field"},
        )
        r = client.get("/api/calls", params={"view": "hidden", "profile": pid})
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1, "the fixture must produce a row, or this gate is vacuous"
    assert items[0]["decision_note"] == "wrong field"


def test_saved_view_returns_the_note(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/save", params={"profile_id": pid})
        client.put(
            f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": "apply"}
        )
        r = client.get("/api/calls", params={"view": "saved", "profile": pid})
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["decision_note"] == "apply"


def test_inbox_does_not_query_the_note_column(monkeypatch, pg_url, pg_session_factory):
    """G1. THE SQL, not the value. An undecided call has no decision row, so a
    null assertion here passes whether or not the column was selected."""
    _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    statements: list[str] = []
    from sqlalchemy import event

    # app.state.engine, NOT the session_factory's bind: the seeding fixtures
    # run on pg_session_factory's engine, a DIFFERENT Engine on the same
    # database, so only request traffic lands here. Precedent and the reason:
    # _statement_counter in tests/web/test_calls_list_query_count.py.
    engine = app.state.engine

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    with client:
        r = client.get("/api/calls", params={"view": "inbox", "profile": pid})
    assert r.status_code == 200
    pattern = re.compile(r"\bcall_decisions\.note\b")
    offenders = [s for s in statements if pattern.search(s)]
    assert offenders == [], offenders


def test_hidden_view_DOES_query_the_note_column(monkeypatch, pg_url, pg_session_factory):
    """The control. Without this, deleting the column entirely would also make
    the inbox gate above pass -- the pair pins a DIFFERENCE, not an absence."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    statements: list[str] = []
    from sqlalchemy import event

    # app.state.engine, NOT the session_factory's bind: the seeding fixtures
    # run on pg_session_factory's engine, a DIFFERENT Engine on the same
    # database, so only request traffic lands here. Precedent and the reason:
    # _statement_counter in tests/web/test_calls_list_query_count.py.
    engine = app.state.engine

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        statements.clear()
        client.get("/api/calls", params={"view": "hidden", "profile": pid})
    pattern = re.compile(r"\bcall_decisions\.note\b")
    assert any(pattern.search(s) for s in statements), statements


def test_another_users_note_is_invisible(monkeypatch, pg_url, pg_session_factory):
    """G9. Must reject a query keyed on (profile_id, call_id) without user_id --
    the natural mistake, because the golden-set miner IS keyed that way."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        client.put(
            f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": "mine"}
        )
    # A SECOND user's decision on the SAME (profile, call), written straight
    # through the session: there is no endpoint for "act as someone else".
    from grant_lab.db.models import User

    with pg_session_factory() as s:
        other = User(status="active", email="colleague@umu.se")
        s.add(other)
        s.flush()
        s.add(
            CallDecision(
                user_id=other.id,
                profile_id=pid,
                call_id=cid,
                state="hidden",
                note="THEIRS",
                decided_by=other.id,
            )
        )
        s.commit()
    with client:
        r = client.get("/api/calls", params={"view": "hidden", "profile": pid})
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["decision_note"] == "mine"
    assert "THEIRS" not in r.text
```

Delete the `_projects_note` stub before committing — it is scaffolding that documents the reasoning, and a `NotImplementedError` helper left in a test file is a trap for the next reader.

- [ ] **Step 2: Run to verify they fail**

```bash
uv run pytest tests/web/test_calls_decision_note_projection.py -v
```

Expected: the round-trip and control tests FAIL (`KeyError: 'decision_note'` or the SQL assertion). `test_inbox_does_not_query_the_note_column` will PASS trivially — that is the vacuity the docstring names, and its control is the test immediately below it.

- [ ] **Step 3: Select the note in the `decision_ordered` branch**

In `src/grant_lab/web/dashboard.py`, in the `if decision_ordered:` block, add `.add_columns(CallDecision.note)` to the chain:

```python
        if decision_ordered:
            stmt = (
                stmt.join(
                    CallDecision,
                    (CallDecision.call_id == Call.id)
                    & (CallDecision.user_id == user_id)
                    & (CallDecision.profile_id == profile_id)
                    & (CallDecision.state == view),
                )
                # The row is ALREADY here, joined for the ORDER BY below. The
                # note is one more column off it -- no new FROM entry, no
                # subselect, no extra statement. Conditional, so inbox and
                # archive (which do not join) pay nothing and cannot
                # accidentally project it.
                .add_columns(CallDecision.note)
                .order_by(CallDecision.decided_at.desc(), Call.id.desc())
            )
```

- [ ] **Step 4: Widen the normalized row tuple**

Still in `dashboard.py`, change the assembly at the "Normalize to..." comment:

```python
    rows = session.execute(stmt.where(*list_where)).unique().all()
    # Normalize to (Call, CallRanking|None, note|None) shape regardless of
    # branch. Only the decision_ordered branch added a third column, and
    # `decision_ordered` is true exactly when that branch ran -- so indexing
    # row[2] anywhere else would be an IndexError, not a None.
    calls: list[tuple[Call, CallRanking | None, str | None]] = [
        (
            row[0],
            row[1] if profile_id is not None else None,
            row[2] if decision_ordered else None,
        )
        for row in rows
    ]
```

And the dataclass field:

```python
@dataclass
class DashboardView:
    # Three-tuple since 2026-09-07: the third slot is the user's own decision
    # note, non-None only on the saved/hidden views (spec §4).
    calls: list[tuple[Call, CallRanking | None, str | None]]
```

- [ ] **Step 5: Add the field to `CallOut` and fix both unpack sites**

In `src/grant_lab/web/api/calls.py`, add to `CallOut` after `escalated: bool`:

```python
    # The user's OWN words about their own decision (spec 2026-09-07). NOT
    # `note`: `applicant_note_count` above is the MODEL's inferred
    # things-to-check, a different thing on the same object. Non-null only on
    # the saved and hidden views -- inbox and archive do not join the decision
    # row at all, so this is None there by construction, not by accident.
    decision_note: str | None
```

Then the two unpack sites. The first:

```python
        call_ids = [call.id for call, _, _ in dashboard.calls]
```

The second, in the `items = [...]` comprehension — change the `for` clause and add the field:

```python
                escalated=(ranking.escalated if ranking else False),
                decision_note=decision_note,
            )
            for call, ranking, decision_note in dashboard.calls
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
uv run pytest tests/web/test_calls_decision_note_projection.py -v
```

Expected: 5 passed.

- [ ] **Step 7: Add the N+1 gate**

Append to `tests/web/test_calls_list_query_count.py`, following
`test_the_list_costs_the_same_number_of_statements_at_1_and_10_calls` in that
file — reuse its fixtures and its counting helper rather than writing new ones:

```python
def test_the_hidden_view_costs_the_same_number_of_statements_at_1_and_10_calls(
    make_app, pg_session_factory
):
    """G8. Per-row growth is what an N+1 looks like; comparing `hidden` against
    `inbox` compares two DIFFERENT queries and would miss it entirely."""
```

Write the body by copying the existing 1-vs-10 test and changing the view to
`hidden`, seeding a `hidden` `CallDecision` with a note for every seeded call.
Add the identical test for `saved`.

- [ ] **Step 8: Run the full backend suite and re-measure**

```bash
uv run pytest -n 16 --dist loadfile -q 2>&1 | tail -5
```

Expected: `1 skipped`, zero FAILED. Record the new collected total; the baseline is `5484` and your delta is the number of tests you added in Tasks 1–3. Do not adjust an expected number to make the arithmetic close.

- [ ] **Step 9: Lint and commit**

```bash
uv run ruff check src tests && uv run ruff format --check src/grant_lab/web/dashboard.py src/grant_lab/web/api/calls.py
git add src/grant_lab/web/dashboard.py src/grant_lab/web/api/calls.py tests/web/
git commit -m "feat(calls): return the decision note on the Saved and Hidden views

The note is read off a join that was already there. build_dashboard_view
joins CallDecision for saved/hidden to ORDER BY decided_at; the note is
one more column off that row -- no subselect, no extra statement. The
design spec's rev 1 prescribed a correlated subselect on the belief that
the query never reaches the decision row, having read only
_decision_predicate (which is indeed a subquery) and generalised.

Conditional on decision_ordered, so inbox and archive cannot project it.
The gate asserts the compiled SQL, not a null in the response: an
undecided call has no decision row either way, so a null assertion is
insensitive to the thing it tests. Its control pins the difference.

DashboardView.calls is now a 3-tuple; both unpack sites are in calls.py."
```

---

### Task 4: `user_notes` on golden-set candidates

**Files:**
- Modify: `src/grant_lab/ranking/golden_set.py`
- Modify: `src/grant_lab/web/api/admin_golden_set.py`
- Modify: `frontend/src/api/schema.gen.ts` (generated — do not hand-edit)
- Test: `tests/test_golden_set_notes.py` (create)

**Interfaces:**
- Consumes: Task 1's stored notes; Task 2's guarantee that a saved row's note was cleared on the flip (which is *not* a substitute for the state filter — a call saved and annotated without ever being hidden still carries a note).
- Produces: `CandidateRow.user_notes: list[str]` and `CandidateOut.user_notes: list[str]`, newest decision first, at most 3, hidden decisions only, unattributed.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_golden_set_notes.py`:

```python
"""Notes reach the golden-set candidate list, unattributed (§7 G7, G12).

G12 is an HTTP round trip on purpose. `CandidateOut(**asdict(c))` builds a
Pydantic model, and Pydantic v2 IGNORES unknown keys by default -- so a field
added only to the CandidateRow dataclass is dropped with no error, no warning
and an absent key. A miner unit test cannot see that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from grant_lab.db.models import (
    Call,
    CallDecision,
    CallRanking,
    Profile,
    ProfileKind,
    Source,
    User,
)
from grant_lab.ranking.golden_set import candidates_dismissed_high_score

_CAP = 3


def _admin_client(app, pg_session_factory):
    """An ADMIN session. `_login` in test_api_calls_hide.py seeds a plain user,
    and /api/admin/golden-set is behind require_admin -- so this seeds its own
    rather than UPDATE-ing someone into an admin after the fact."""
    from fastapi.testclient import TestClient

    with pg_session_factory() as s:
        u = User(status="active", email="gsadmin@umu.se", is_admin=True)
        s.add(u)
        s.commit()
        uid = u.id
    client = TestClient(app)
    client.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return client


def _seed_pair(session, *, n_hidden_notes: int, n_saved_notes: int):
    """One call ranked 80 for one profile, hidden by `n_hidden_notes` users who
    each wrote a note, and saved-with-a-note by `n_saved_notes` others."""
    src = session.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    owner = User(status="active", email="gsowner@umu.se")
    session.add(owner)
    session.flush()
    prof = Profile(
        name="Group GS",
        # `.value`, matching tests/test_api_calls_hide.py's _login.
        kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
        level="group",
        description="x",
        structured={},
        created_by=owner.id,
    )
    # Same shape as _seed_call in tests/test_api_calls_hide.py: `fetched_at`
    # takes its column default, `published_at` is the one set explicitly.
    call = Call(
        source_id=src.id,
        external_id="gs-1",
        url="https://example.org/gs-1",
        title="GS Call",
        published_at=datetime.now(UTC),
    )
    session.add_all([prof, call])
    session.flush()
    session.add(
        CallRanking(
            call_id=call.id,
            profile_id=prof.id,
            aggregate_score=80,
            rationale="",
            model="m",
            prompt_version="1",
            raw_response={},
            ranked_at=datetime.now(UTC),
        )
    )
    base = datetime.now(UTC)
    for i in range(n_hidden_notes):
        u = User(status="active", email=f"hider{i}@umu.se")
        session.add(u)
        session.flush()
        session.add(
            CallDecision(
                user_id=u.id,
                profile_id=prof.id,
                call_id=call.id,
                state="hidden",
                note=f"hidden-{i}",
                # Ascending, so hidden-(n-1) is the NEWEST decision.
                decided_at=base + timedelta(minutes=i),
                decided_by=u.id,
            )
        )
    for i in range(n_saved_notes):
        u = User(status="active", email=f"saver{i}@umu.se")
        session.add(u)
        session.flush()
        session.add(
            CallDecision(
                user_id=u.id,
                profile_id=prof.id,
                call_id=call.id,
                state="saved",
                note=f"SAVED-{i}",
                decided_at=base + timedelta(hours=1, minutes=i),
                decided_by=u.id,
            )
        )
    session.commit()
    return call.id, prof.id


def test_candidate_carries_the_hidden_notes_newest_first(pg_session_factory):
    with pg_session_factory() as s:
        _seed_pair(s, n_hidden_notes=2, n_saved_notes=0)
        rows = candidates_dismissed_high_score(s)
    assert len(rows) == 1
    assert rows[0].user_notes == ["hidden-1", "hidden-0"]


def test_saved_notes_are_excluded_and_the_filter_precedes_the_cap(pg_session_factory):
    """G7. FIVE saved notes against ONE hidden one: with fewer notes than the
    cap, filtering after the cap gives the same answer as filtering before it,
    so the seed must EXCEED the cap or the gate proves nothing. The saved notes
    are also the NEWEST, so a cap applied first would evict the hidden one."""
    with pg_session_factory() as s:
        _seed_pair(s, n_hidden_notes=1, n_saved_notes=5)
        rows = candidates_dismissed_high_score(s)
    assert rows[0].user_notes == ["hidden-0"]


def test_notes_are_capped_at_three(pg_session_factory):
    with pg_session_factory() as s:
        _seed_pair(s, n_hidden_notes=5, n_saved_notes=0)
        rows = candidates_dismissed_high_score(s)
    assert rows[0].user_notes == ["hidden-4", "hidden-3", "hidden-2"]
    assert len(rows[0].user_notes) == _CAP


def test_the_http_response_carries_user_notes(monkeypatch, pg_url, pg_session_factory):
    """G12. The serialization boundary, not the miner."""
    from tests.test_api_calls_hide import _setup

    with pg_session_factory() as s:
        _seed_pair(s, n_hidden_notes=1, n_saved_notes=0)
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/api/admin/golden-set")
    assert r.status_code == 200
    cands = r.json()["candidates"]
    hit = [c for c in cands if c["reason"] == "dismissed_high_score"]
    assert hit, cands
    assert hit[0]["user_notes"] == ["hidden-0"]


def test_the_candidate_response_carries_no_author_metadata(
    monkeypatch, pg_url, pg_session_factory
):
    """G6, D3. Fields, deliberately. This gate does not and cannot assert that
    free text is anonymous -- only that the system attaches no author."""
    from tests.test_api_calls_hide import _setup

    with pg_session_factory() as s:
        _seed_pair(s, n_hidden_notes=1, n_saved_notes=0)
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/api/admin/golden-set")
    for c in r.json()["candidates"]:
        for banned in ("user_id", "email", "name", "decided_by"):
            assert banned not in c, (banned, c)
    assert "@umu.se" not in r.text
```

`User.is_admin` is real (`src/grant_lab/db/models/identity.py`) — verified 2026-09-07.

- [ ] **Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_golden_set_notes.py -v
```

Expected: FAILED with `AttributeError: 'CandidateRow' object has no attribute 'user_notes'` and `KeyError: 'user_notes'`.

- [ ] **Step 3: Add the field and the mining query**

In `src/grant_lab/ranking/golden_set.py`, add to the dataclass:

```python
@dataclass
class CandidateRow:
    call_id: int
    profile_id: int
    call_title: str
    profile_name: str
    aggregate_score: int
    embed_similarity: float | None
    reason: str
    # The HUMANS' own words, not the mining reason above. Unattributed (D3),
    # hidden decisions only, newest decision first, at most _NOTE_CAP.
    # Empty for the two candidate queries that need no decision at all.
    user_notes: list[str] = field(default_factory=list)
```

Add `field` to the `dataclasses` import at the top of the file, and a constant beside `_CANDIDATE_LIMIT`:

```python
_NOTE_CAP = 3
```

Then a helper, above `candidates_dismissed_high_score`:

```python
def _notes_for(session, pairs: list[tuple[int, int]]) -> dict[tuple[int, int], list[str]]:
    """The hidden decisions' notes for each (call_id, profile_id) pair.

    ONE query for the whole candidate page, not one per candidate.

    `state == "hidden"` is applied HERE, in the WHERE clause, and the cap is
    applied afterwards in Python -- so the filter precedes the cap (D7). A
    saved decision can carry a note too, and letting one through would file
    "we should apply for this" as evidence that the model scored it too high.

    Ordered newest DECISION first. There is no note-edit timestamp and this
    feature does not add one, so a note edited today on a decision made in
    March sorts as March. `id` breaks ties, which two decisions committed in
    the same transaction will have.
    """
    if not pairs:
        return {}
    out: dict[tuple[int, int], list[str]] = {}
    rows = session.execute(
        select(CallDecision.call_id, CallDecision.profile_id, CallDecision.note)
        .where(
            tuple_(CallDecision.call_id, CallDecision.profile_id).in_(pairs),
            CallDecision.state == "hidden",
            CallDecision.note.is_not(None),
        )
        .order_by(CallDecision.decided_at.desc(), CallDecision.id.desc())
    ).all()
    for call_id, profile_id, note in rows:
        bucket = out.setdefault((call_id, profile_id), [])
        if len(bucket) < _NOTE_CAP:
            bucket.append(note)
    return out
```

Add `tuple_` to the `sqlalchemy` import line at the top of the file.

- [ ] **Step 4: Populate it in the miner**

Change the return of `candidates_dismissed_high_score` from a comprehension to a two-step build, because the notes query needs the pairs first:

```python
    found = session.execute(q).all()
    notes = _notes_for(session, [(r.call_id, r.profile_id) for r, _t, _n in found])
    return [
        CandidateRow(
            call_id=r.call_id,
            profile_id=r.profile_id,
            call_title=title,
            profile_name=name,
            aggregate_score=r.aggregate_score,
            embed_similarity=r.embed_similarity,
            reason="dismissed_high_score",
            user_notes=notes.get((r.call_id, r.profile_id), []),
        )
        for r, title, name in found
    ]
```

Leave `candidates_cosine_disagreement` and `candidates_escalation_disagreement` untouched: neither requires a decision to exist, so the dataclass default (empty list) is correct and the UI renders nothing rather than branching.

- [ ] **Step 5: Add the field to the serializer**

In `src/grant_lab/web/api/admin_golden_set.py`:

```python
class CandidateOut(BaseModel):
    call_id: int
    profile_id: int
    call_title: str
    profile_name: str
    aggregate_score: int
    embed_similarity: float | None
    reason: str
    # THE BOUNDARY. `CandidateOut(**asdict(c))` below is a Pydantic v2 model,
    # which IGNORES unknown keys -- so a field added only to the CandidateRow
    # dataclass is dropped here silently, with no error and an absent key.
    user_notes: list[str] = []
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
uv run pytest tests/test_golden_set_notes.py -v
```

Expected: 5 passed.

- [ ] **Step 7: Run the adverse mutation for G12**

```bash
cd /home/parlar_ai/dev/grant-lab
cp src/grant_lab/web/api/admin_golden_set.py /tmp/ags.bak
trap 'cp /tmp/ags.bak /home/parlar_ai/dev/grant-lab/src/grant_lab/web/api/admin_golden_set.py' EXIT
python3 - <<'PY'
from pathlib import Path
p = Path("/home/parlar_ai/dev/grant-lab/src/grant_lab/web/api/admin_golden_set.py")
s = p.read_text()
old = "    user_notes: list[str] = []\n"
assert s.count(old) == 1
p.write_text(s.replace(old, ""))
PY
uv run pytest tests/test_golden_set_notes.py -v
```

Expected: `test_the_http_response_carries_user_notes` **FAILED** (`KeyError: 'user_notes'`) while the three miner tests **PASS**. That split is the finding this gate exists for.

- [ ] **Step 8: Regenerate the OpenAPI types**

```bash
cd /home/parlar_ai/dev/grant-lab/frontend
EMBEDDING_MODEL=bge-m3 pnpm run gen:api
cd /home/parlar_ai/dev/grant-lab
git diff --stat frontend/src/api/schema.gen.ts
```

Expected: `schema.gen.ts` changed, carrying `decision_note` (Task 3) and `user_notes` plus the new `/api/calls/{call_id}/note` path. If the diff is empty the generator did not run — check its stdout; this script has a known stdout gotcha.

- [ ] **Step 9: Lint, typecheck and commit**

```bash
uv run ruff check src tests
cd frontend && pnpm run build && cd ..
git add src/grant_lab/ranking/golden_set.py src/grant_lab/web/api/admin_golden_set.py tests/test_golden_set_notes.py frontend/src/api/schema.gen.ts
git commit -m "feat(golden-set): show the human's reason beside the score they disagreed with

candidates_dismissed_high_score already mines calls scoring >=60 that a
human hid, as evidence the model scored them too high. The person
labelling them saw the title, the profile and the score, never why a
human rejected it -- so they guessed.

Hidden decisions only, and the state filter is in the WHERE clause while
the cap is applied afterwards: a saved decision can carry a note too, and
letting one through files \"we should apply for this\" as evidence of an
over-score. The gate seeds five saved notes against one hidden, because
with fewer notes than the cap both orderings agree.

Unattributed (D3). CandidateOut needed the field too -- Pydantic v2
ignores unknown keys, so the dataclass alone would have dropped it
silently; the mutation confirms the HTTP gate reddens while the three
miner tests stay green."
```

---

### Task 5: Mount the golden-set page in the match edition

**Files:**
- Modify: `src/grant_lab/web/api/__init__.py`
- Modify: `tests/architecture/test_edition_surface.py`
- Modify: `tests/architecture/test_match_gates_are_wired.py`
- Modify: `frontend/src/router.simple.tsx`
- Modify: `frontend/src/routes/AdminNav.tsx`
- Modify: `frontend/src/routes/AdminNav.test.tsx`
- Modify: `frontend/e2e/match.spec.ts`

**Interfaces:**
- Consumes: Task 4's `user_notes` (this is what makes the page worth mounting).
- Produces: `/admin/golden-set` reachable in a `match` build, API and page.

**Read first:** spec §10. It explains why this is seven files and not one line — `admin_golden_set.router` is in `_full_only_routers()`, so the match edition serves no golden-set API at all.

- [ ] **Step 1: Write the failing backend gate**

Append to `tests/architecture/test_edition_surface.py`:

```python
def test_match_serves_the_golden_set_api():
    """G13, backend half. The page is useless without it, and until the
    2026-09-07 decision-notes work the match edition served neither."""
    app = _match_app()  # use whatever this file's existing match-app fixture is named
    paths = {r.path for r in app.routes}
    assert "/api/admin/golden-set" in paths
```

Replace `_match_app()` with the fixture this file already defines — read the file and reuse it; do not add a second way to build a match app.

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/architecture/test_edition_surface.py -v -k golden
```

Expected: FAILED — the path is absent from a match build.

- [ ] **Step 3: Move the router into `_ALWAYS`**

In `src/grant_lab/web/api/__init__.py`, remove `admin_golden_set` from the deferred import inside `_full_only_routers()` and remove `admin_golden_set.router` from its return tuple. Add it to the `_ALWAYS` collection with a comment:

```python
    # Moved back out of _full_only_routers 2026-09-07. It went there on
    # 2026-09-04 with drafting and retention, because six require_admin
    # proposal-assist endpoints were live in a deployment that does not serve
    # proposal assist. The golden set is NOT proposal assist: it is the
    # ranking system's own evaluation surface, and the match edition is the
    # only edition that ships. Its imports are dataclasses, FastAPI, Pydantic,
    # SQLAlchemy, auth.dependencies, db.models and ranking.golden_set -- and
    # ranking/golden_set.py imports only dataclasses, SQLAlchemy and
    # db.models. Nothing from the proposal-assist half, so the process cut
    # (tests/architecture/test_match_edition_process_cut.py) still reports 0.
```

Update the long comment above `admin.drafting_router` in `_full_only_routers()`, which currently says "The three ADMIN routers" and names `AdminGoldenSet.tsx` among the frontend consumers `router.simple.tsx` mounts none of. It is two routers now, and that sentence is about to be false.

- [ ] **Step 4: Fix the excluded-prefix list and re-measure its comment**

In `tests/architecture/test_edition_surface.py`, remove `"/api/admin/golden-set"` from `EXCLUDED_PREFIXES`.

Its inline comment carries a measured claim — that the three admin prefixes cover "exactly the six PATHS (seven operations) the move drops", enumerated by hand. **Re-measure it; do not decrement by arithmetic:**

```bash
uv run python - <<'PY'
import os
os.environ["APP_EDITION"] = "full"
from grant_lab.main import create_app
full = {r.path for r in create_app().routes}
os.environ["APP_EDITION"] = "match"
import importlib, grant_lab.main
importlib.reload(grant_lab.main)
match = {r.path for r in grant_lab.main.create_app().routes}
dropped = sorted(p for p in full - match if p.startswith("/api/admin/"))
print(len(dropped), dropped)
PY
```

Rewrite the comment with the printed values. If this snippet does not produce a clean match app (edition is read at import in places), build the two apps in separate `uv run python -c` invocations instead and diff the outputs — the number is what matters, not the elegance of getting it.

- [ ] **Step 5: Mount the route and the nav link**

In `frontend/src/router.simple.tsx`, add the lazy import beside the other admin ones:

```tsx
const AdminGoldenSet = lazy(() =>
  import("./routes/AdminGoldenSet").then((m) => ({ default: m.AdminGoldenSet })),
);
```

and the child route, after `users/pending`:

```tsx
          {
            path: "golden-set",
            element: (
              <Suspense fallback={<RouteSpinner />}>
                <AdminGoldenSet />
              </Suspense>
            ),
          },
```

Confirm the exported name with `grep -n 'export' frontend/src/routes/AdminGoldenSet.tsx` — if it is a default export, adjust the lazy import accordingly.

In `frontend/src/routes/AdminNav.tsx`, remove `"/admin/golden-set"` from `MATCH_EXCLUDED` and rewrite the comment above it, which currently reads "Release 1 mounts only /admin, /admin/users and /admin/users/pending". `routes.json`'s `spa_routes` already contains `/admin`, which owns `/admin/*` by subtree — **no `routes.json` change is needed**, and adding one would be wrong.

- [ ] **Step 6: Update the three frontend gates**

`frontend/src/routes/AdminNav.test.tsx`: the `MATCH_HIDDEN` constant (drop the golden-set entry), the match-edition omission case, and the comment at the top naming the three unmounted routes.

`frontend/e2e/match.spec.ts` line ~392: rename the test to `"the admin subnav offers only the four sections release 1 mounts"` and change its expected array to `["/admin", "/admin/users", "/admin/users/pending", "/admin/golden-set"]` — **in the order `AdminNav.tsx`'s `LINKS` array produces**, which puts golden-set third, before Users. Read `LINKS` and write the order it yields, not the order that reads nicely.

`tests/architecture/test_match_gates_are_wired.py`: change the matching string in `REQUIRED_SCENARIOS`. This is a **rename, not an addition**, so `len(titles) == len(REQUIRED_SCENARIOS)` still balances. The `expects == 128` assertion should also hold — you are changing the contents of an existing `expect(...)`, not adding one — but re-measure rather than assume:

```bash
uv run pytest tests/architecture/test_match_gates_are_wired.py -v
```

If the expect count moved, update the number **and** the stale message beside it, which currently says "this gate pins 120" while asserting 128.

- [ ] **Step 7: Run everything**

```bash
cd /home/parlar_ai/dev/grant-lab
uv run pytest tests/architecture/ -v 2>&1 | tail -20
cd frontend && pnpm run test -- AdminNav && pnpm run build && cd ..
```

Expected: all green. `AdminNav.test.tsx`'s *"every admin link the match edition renders is a route the match router mounts"* is the case that couples steps 5's two halves — if you lifted the nav exclusion without mounting the route, it is the one that goes red.

- [ ] **Step 8: Run the adverse mutation for G13**

Revert **only** the `_ALWAYS` move (step 3) and confirm the split:

```bash
git stash push src/grant_lab/web/api/__init__.py
uv run pytest tests/architecture/test_edition_surface.py -v -k golden
cd frontend && pnpm run test -- AdminNav && cd ..
git stash pop
```

Expected: the backend gate **FAILED**, the AdminNav suite **PASSED**. Two independent halves. If the nav suite also reddens, the two are coupled and the backend gate is not proving anything of its own.

- [ ] **Step 9: Regenerate the architecture map and commit**

```bash
uv run python scripts/gen_architecture.py
git add -A
git commit -m "feat(admin): the match edition can reach its own golden set

admin_golden_set.router moved back into _ALWAYS. It went into
_full_only_routers on 2026-09-04 with drafting and retention, because six
require_admin proposal-assist endpoints were live in a deployment that
does not serve proposal assist. The golden set is not proposal assist --
it is the ranking system's evaluation surface -- and match is the only
edition that ships, so the notes added in the previous commit had no
reader at all.

No damage to the process cut: admin_golden_set imports FastAPI, Pydantic,
SQLAlchemy, auth.dependencies, db.models and ranking.golden_set, and that
imports only dataclasses, SQLAlchemy and db.models. Zero proposal-assist
modules, re-measured.

The excluded-prefix comment in test_edition_surface.py carried a measured
enumeration of the paths those prefixes drop; it was re-measured, not
decremented. AdminNav.test.tsx's \"every admin link the match edition
renders is a route the match router mounts\" couples the nav and the
router, so the two cannot drift apart silently -- the mutation confirms
the backend gate reddens alone."
```

---

### Task 6: The strip

**Files:**
- Modify: `frontend/src/api/calls.ts`
- Create: `frontend/src/routes/DecisionNoteStrip.tsx`
- Create: `frontend/src/routes/DecisionNoteStrip.test.tsx`
- Modify: `frontend/src/routes/TriageFeed.tsx`

**Interfaces:**
- Consumes: Task 1's endpoint via `schema.gen.ts` (regenerated in Task 4).
- Produces: `useSetDecisionNote()` returning a mutation taking `{ id, profileId, note }`; `<DecisionNoteStrip target={{callId, profileId, title, state}} onDismiss={...} />`.

**Read first:** spec D10. Every bullet there is a behaviour this task owes, and four of them are the reason the strip is its own component rather than markup inside `TriageFeed`.

- [ ] **Step 1: Add the hook**

In `frontend/src/api/calls.ts`, after `useUnsaveCall`:

```ts
export type NoteArgs = { id: number; profileId: number; note: string | null };

// NOT invalidateDecisions: a note changes no count and moves no call between
// views. Invalidating the rail here would refetch three counts on every
// keystroke-committed note for no observable change. The list query is
// invalidated so the Saved/Hidden rows show the new text.
export function useSetDecisionNote() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, profileId, note }: NoteArgs) =>
      unwrap(
        await api.PUT("/api/calls/{call_id}/note", {
          params: { path: { call_id: id }, query: { profile_id: profileId } },
          body: { note },
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["calls"] }),
  });
}
```

Confirm the list query key with `grep -n 'queryKey' frontend/src/api/calls.ts` and use the real one.

- [ ] **Step 2: Write the failing component tests**

Create `frontend/src/routes/DecisionNoteStrip.test.tsx`:

```tsx
// D10's contract, one test per bullet. The strip is a separate component
// precisely so these can be written without standing up the whole feed.
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DecisionNoteStrip } from "./DecisionNoteStrip";

const target = { callId: 7, profileId: 3, title: "ERC Synergy", state: "hidden" as const };

function setup(save = vi.fn().mockResolvedValue({})) {
  render(<DecisionNoteStrip target={target} onSave={save} onDismiss={vi.fn()} />);
  return save;
}

it("names the call it is about", () => {
  setup();
  expect(screen.getByText(/ERC Synergy/)).toBeInTheDocument();
});

it("discloses that an administrator can read it", () => {
  // D3. A privacy property the user cannot see is not a privacy property.
  setup();
  expect(screen.getByText(/administrator can read/i)).toBeInTheDocument();
});

it("commits on Enter, with the ids it was created for", async () => {
  const save = setup();
  const box = screen.getByRole("textbox");
  await userEvent.type(box, "wrong field{Enter}");
  await waitFor(() =>
    expect(save).toHaveBeenCalledWith({ id: 7, profileId: 3, note: "wrong field" }),
  );
});

it("commits on blur", async () => {
  const save = setup();
  await userEvent.type(screen.getByRole("textbox"), "too small");
  await userEvent.tab();
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
});

it("Escape reverts and does not save", async () => {
  const save = setup();
  await userEvent.type(screen.getByRole("textbox"), "oops{Escape}");
  expect(save).not.toHaveBeenCalled();
});

it("does not save on every keystroke", async () => {
  const save = setup();
  await userEvent.type(screen.getByRole("textbox"), "abcdef");
  expect(save).not.toHaveBeenCalled();
});

it("keeps the typed text and shows the error when the save fails", async () => {
  const save = vi.fn().mockRejectedValue(new Error("boom"));
  render(<DecisionNoteStrip target={target} onSave={save} onDismiss={vi.fn()} />);
  await userEvent.type(screen.getByRole("textbox"), "kept{Enter}");
  await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  expect(screen.getByRole("textbox")).toHaveValue("kept");
});
```

- [ ] **Step 3: Run to verify they fail**

```bash
cd frontend && pnpm run test -- DecisionNoteStrip
```

Expected: FAILED — the module does not exist. A "Command vitest not found" here means `node_modules` is missing (it is gitignored), not a cooldown problem: run `pnpm install` first.

- [ ] **Step 4: Write the component**

Create `frontend/src/routes/DecisionNoteStrip.tsx`. It takes its target as a prop and never reads "the current call" — that binding is the whole defence against a late write landing on the next call (D10). It renders a `<label>`, an `<input>`, the disclosure line, and an error `role="alert"` when the save rejects. `onSave` is called with `{ id, profileId, note }` on Enter or blur, never on change; Escape clears the local value and calls `onDismiss`.

Do not add `aria-live` to the disclosure or the error region beyond the single `role="alert"` on the error — the feed already announces list changes and a second live region talks over it.

- [ ] **Step 5: Run to verify they pass**

```bash
cd frontend && pnpm run test -- DecisionNoteStrip
```

Expected: 7 passed.

- [ ] **Step 6: Wire it into the feed**

In `frontend/src/routes/TriageFeed.tsx`, hold the strip's target in state, set it in the decision mutations' `onSuccess` (never optimistically — a failed hide must not solicit a note for a decision that did not happen), and render it inside `<div className={styles.column}>` immediately after the `data.ranking.pending` status paragraph, before the `items.length === 0` branch. Rendering it there rather than inside the non-empty branch means it survives the list emptying under it.

- [ ] **Step 7: Add the two feed-level gates**

Append to `frontend/src/routes/TriageFeed.test.tsx`:

```tsx
it("the strip does not appear when the decision request fails", async () => {
  // D10. Optimistic display would solicit a note for a decision that did not
  // happen -- and the note endpoint would then 404 on a row that never existed.
});

it("a keystroke inside the strip does not decide the focused call", async () => {
  // G10. VACUITY: a test that never focuses the input passes whether or not
  // isEditableTarget guards the listener. Focus it first, and assert the
  // decision mutation was NOT called.
});

it("a late note write lands on its own call, not the next one", async () => {
  // G11. Type a note, decide the NEXT call before the first PUT resolves,
  // assert the PUT carried the FIRST call's id. Adverse mutation: read the id
  // from current focus instead of the strip's bound target.
});
```

Write the bodies following this file's existing patterns for mocking the API and driving the keyboard. Use `waitFor` around assertions rather than `findBy*` — `findBy*` waits for PRESENCE, and an always-mounted probe races it.

- [ ] **Step 8: Run, typecheck, mutate, commit**

```bash
cd frontend && pnpm run test && pnpm run build && cd ..
```

Then run G11's adverse mutation: change the strip's `onSave` call to read the focused call's id instead of `target.callId`, and confirm only the late-write test reddens. Restore, then:

```bash
git add frontend/src/api/calls.ts frontend/src/routes/DecisionNoteStrip.tsx frontend/src/routes/DecisionNoteStrip.test.tsx frontend/src/routes/TriageFeed.tsx frontend/src/routes/TriageFeed.test.tsx
git commit -m "feat(triage): ask why, without stopping the keyboard loop

Pressing h removes the card, so there is nothing left to annotate. A
strip takes its place at the top of the list and is ignorable: j/k/s/h
keep working, and the next decision replaces it.

It is bound to the (call, profile) it was created for and never reads
current focus, which is what stops a late write landing on the next call
-- the same reason CallDetail already keeps {id, profileId} for its Undo.
Appears only after the decision SUCCEEDS: optimistic display would
solicit a note for a decision that did not happen, and the endpoint would
404 on a row that never existed.

Enter or blur commits; Escape reverts. No autosave -- a debounced timer
that outlives the strip is the same bug as an unbound write. A failed
save keeps the typed text.

The keyboard gate focuses the input first: without that it passes whether
or not isEditableTarget guards the listener."
```

---

### Task 7: Show the notes

**Files:**
- Modify: `frontend/src/routes/TriageCard.tsx` (+ its test)
- Modify: `frontend/src/routes/CallDetail.tsx` (+ its test)
- Modify: `frontend/src/routes/AdminGoldenSet.tsx` (+ its test)

**Interfaces:**
- Consumes: `CallOut.decision_note` (Task 3), `CandidateOut.user_notes` (Task 4), `useSetDecisionNote` (Task 6).
- Produces: nothing later tasks depend on. This is the last task.

- [ ] **Step 1: Write the failing tests**

In `TriageCard.test.tsx`: a card in the `hidden` view whose `decision_note` is `"wrong field"` renders that text; a card in `inbox` with `decision_note: null` renders no note affordance. **`pnpm run build` is what catches fixtures missing the new field — vitest will not**, so expect tsc errors in existing fixtures and add `decision_note: null` to each.

In `CallDetail.test.tsx`: after hiding, a note input appears beside the existing Undo control.

In `AdminGoldenSet.test.tsx`: a candidate with `user_notes: ["a", "b"]` renders both, and one with `user_notes: []` renders no notes element.

- [ ] **Step 2: Run to verify they fail**

```bash
cd frontend && pnpm run test -- TriageCard CallDetail AdminGoldenSet
```

- [ ] **Step 3: Implement all three**

`TriageCard.tsx`: render the note under the title when `view` is `saved` or `hidden` and `decision_note` is non-null; clicking it opens the same input the strip uses, committing through `useSetDecisionNote`.

`CallDetail.tsx`: the note input beside the existing hide/undo control, using the `{id, profileId}` it already holds in state.

`AdminGoldenSet.tsx`: inside `CandidateRow`, under the existing `<Code>{c.reason}</Code>` cell, render `c.user_notes` as a short list. No author, no date — there is none to show (D3).

- [ ] **Step 4: Run tests and typecheck**

```bash
cd frontend && pnpm run test && pnpm run build && cd ..
```

Expected: all green. Fix every fixture tsc names.

- [ ] **Step 5: Full suite, both halves**

```bash
cd /home/parlar_ai/dev/grant-lab
uv run pytest -n 16 --dist loadfile -q 2>&1 | tail -5
cd frontend && pnpm run test && pnpm run build && pnpm run lint && cd ..
uv run ruff check src tests
uv run python scripts/gen_architecture.py && git diff --stat docs/ARCHITECTURE.md
```

Expected: `1 skipped` exactly. Zero FAILED. Report the collected total against the `5484` baseline as a delta you can account for test-by-test.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(calls): show the reason, everywhere it was written

The note renders under the title in Saved and Hidden, beside the existing
hide/undo on the call page, and under the mining reason on the golden-set
candidates -- unattributed there, because there is no author to show.

tsc caught the existing card fixtures missing decision_note; vitest does
not typecheck, so pnpm run build is the gate that found them."
```

---

## Self-Review

**Spec coverage:** D1 (free text) — Task 1's `DecisionNoteIn`. D2 (strip) — Task 6. D3 (privacy, disclosure, unattributed) — Task 6 step 2's disclosure test, Task 4's G6 test. D4 (un-decide destroys) — Task 2. D5 (404) — Task 1. D6 (naming) — Global Constraints + Task 1 step 3. D7 (multi-user, ordering, cap, state filter before cap) — Task 4. D8 (length, whitespace) — Task 1. D9 (flip clears) — Task 2. D10 (strip contract) — Task 6. §4 wire — Tasks 1, 3, 4. §5 surfaces — Tasks 6, 7. §10 mount — Task 5. Gates G1–G13 — G1/G8/G9 Task 3, G2/G4 Task 1, G3/G5 Task 2, G6/G7/G12 Task 4, G10/G11 Task 6, G13 Task 5. **No gaps.**

**Known soft spots, stated rather than hidden:** Task 3 step 7, Task 5 steps 1 and 6, Task 6 steps 4/6/7 and all of Task 7 describe changes against existing files whose exact current contents the implementer must read. Each names the file, the anchor and the property; none says "similar to Task N". Where a fixture name is uncertain (`_match_app`, the list query key, `is_admin`, `AdminGoldenSet`'s export style) the step says to grep for the real one rather than guessing — those are the places this plan is least able to be literal, and pretending otherwise would be worse than saying so.

**Type consistency:** `decision_note` (response, `CallOut` + `DecisionNoteOut`), `note` (request, `DecisionNoteIn` + DB column), `user_notes` (`CandidateRow` + `CandidateOut`), `NoteArgs {id, profileId, note}`, `DashboardView.calls` 3-tuple with both unpack sites named. Checked against every task.
