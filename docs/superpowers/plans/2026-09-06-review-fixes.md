# Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four defects the 2026-09-06 outside review confirmed in the Plan C and freshness code.

**Architecture:** Four independent fixes. Two need a migration (owner-scoped profile-name uniqueness; QR columns on `board_items`), one is a row lock in a shared helper, one adds a key to a hash payload and bumps its version. No task depends on another.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, Alembic, PostgreSQL 16, pytest (+ testcontainers Postgres).

**Spec:** `docs/superpowers/specs/2026-09-06-review-fixes-design.md`. The spec is INPUT. If it is wrong, that is a `PLAN-DEFECTS.md` entry, not an edit.

## Global Constraints

- **Base:** `master` at `9432d83e`. Branch `fix/review-2026-09-06`, worktree `.worktrees/review-fixes`.
- **Measured baseline: `5412 passed, 1 skipped`.** Verified on this tree: only two docs files changed since the tree that number was measured on. Re-measure yourself before asserting any total.
- **`1 skipped` stays exactly 1.** `0 skipped` means a live Anthropic call ran — stop and record it. Never export `ANTHROPIC_API_KEY`.
- **Never read, write, copy or back up `/home/parlar_ai/dev/grant-lab/.env`.** Set any env var inline in the one command that needs it.
- **`docs/superpowers/specs/` byte-identical at every commit.**
- **Migrations are `0076` and `0077`.** Head is `0075`; verified by listing `alembic/versions/`.
- **`docs/ARCHITECTURE.md` is generated.** `uv run python scripts/gen_architecture.py` in the same commit as any model change. Never hand-edit.
- **Gates:** `uv run pytest -n 16 --dist loadfile` (never `-n auto`, never `--dist loadgroup`); `uv run ruff check src tests alembic scripts`. Never `ruff format` a directory.
- **False-pass signature:** a broken import shows as mass teardown ERRORs with zero FAILED. **Judge every mutation on FAILED, never on ERROR.** A `no tests ran` result is also not a pass.

## Release timing — read before deferring Task 3

Task 3 bumps `FINGERPRINT_VERSION`, which invalidates every stored fingerprint and forces one full re-embed and re-rank. **Migration `0075` already forces exactly that on the first night after it deploys**, because it deliberately leaves `profiles.embedding_fingerprint` NULL.

So Task 3 is **free if it ships before the first production deploy** and a second unplanned corpus re-rank if it ships after. That is a release-sequencing fact, not a reason to reorder the tasks.

## File structure

| File | Responsibility | Task |
|---|---|---|
| `src/grant_lab/web/api/profiles.py` | `FOR UPDATE` in `_profile_for_edit`; scoped name pre-check | 1, 3 |
| `alembic/versions/0076_owner_scoped_profile_names.py` | drop the global index, add the owner-scoped one | 3 |
| `src/grant_lab/db/models/profiles.py` | the index definition | 3 |
| `src/grant_lab/profiles/fingerprint.py` | `embedding_model` in the payload, `v2` | 2 |
| `alembic/versions/0077_board_item_qr.py` | three QR columns + backfill | 4 |
| `src/grant_lab/db/models/boards.py` | `BoardItem` QR columns | 4 |
| `src/grant_lab/web/api/boards.py` | render and store at approval | 4 |
| `src/grant_lab/boards/public.py` | return the stored QR | 4 |
| `src/grant_lab/web/api/public_boards.py` | read the columns, encode nothing | 4 |

---

### Task 1: F2 — the transfer race

**Files:**
- Modify: `src/grant_lab/web/api/profiles.py:136` (`_profile_for_edit`)
- Test: `tests/test_api_profile_transfer_concurrency.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_profile_for_edit` returns a row locked `FOR UPDATE` for the life of the caller's transaction. Its five callers (`:598`, `:632`, `:694`, `:739`, `:794` — the four sharing operations and transfer) are thereby serialised against the same row.

**Why the helper and not the handler.** Patching only the transfer route leaves `add_profile_viewer` racing it. All five callers mutate sharing state on the same row, so the lock belongs where they all pass.

- [ ] **Step 1: Write the failing test**

Two real sessions and two real transactions. A fixture that shares one session proves nothing — SQLAlchemy would serve the second read from the identity map and no lock would be exercised.

```python
import threading

from grant_lab.db.models import Profile


def test_two_concurrent_transfers_do_not_both_authorize_against_the_old_owner(
    session_factory, client, owner_profile, auth_headers, make_user
):
    """The loser must see the CURRENT owner, not the one it loaded before the
    winner committed. Without FOR UPDATE both readers pass authorization and
    the second computes outgoing_lost_read for an owner that no longer holds
    the profile."""
    b = make_user(email="b@umu.se")
    c = make_user(email="c@umu.se")
    results = []
    barrier = threading.Barrier(2)

    def transfer(email):
        barrier.wait()
        r = client.put(
            f"/api/profiles/{owner_profile.id}/owner",
            json={"email": email},
            headers=auth_headers,
        )
        results.append((email, r.status_code, r.json() if r.status_code == 200 else None))

    t1 = threading.Thread(target=transfer, args=(b.email,))
    t2 = threading.Thread(target=transfer, args=(c.email,))
    t1.start(); t2.start(); t1.join(); t2.join()

    with session_factory() as s:
        final = s.get(Profile, owner_profile.id).created_by

    ok = [r for r in results if r[1] == 200]
    assert len(ok) >= 1
    # Whoever the row says owns it must be who the winning response named.
    winners = [r for r in ok if r[2]["owner"]["user_id"] == final]
    assert winners, (
        f"no successful response named the final owner {final}; "
        f"a transfer authorized against an owner that had already changed"
    )


def test_a_transfer_and_a_viewer_add_serialize(
    session_factory, client, owner_profile, auth_headers, make_user
):
    """The other four callers of _profile_for_edit must serialize with transfer.
    Locking the transfer handler alone would leave this racing."""
    b = make_user(email="b@umu.se")
    v = make_user(email="v@umu.se")
    barrier = threading.Barrier(2)
    errors = []

    def do(fn):
        barrier.wait()
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=do, args=(lambda: client.put(
        f"/api/profiles/{owner_profile.id}/owner", json={"email": b.email},
        headers=auth_headers),))
    t2 = threading.Thread(target=do, args=(lambda: client.post(
        f"/api/profiles/{owner_profile.id}/viewers", json={"email": v.email},
        headers=auth_headers),))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert not errors, f"a concurrent sharing mutation raised: {errors}"
```

`make_user`, `owner_profile` and `auth_headers` are **assumed and unverified**. Check
each against `tests/conftest.py` and the existing `tests/test_api_profile_transfer.py`
before writing this file, and reuse whatever that module already uses. Note that
`client_unauth` — which a later task might reach for — is package-scoped to
`tests/proposal_assist/` and is NOT available under `tests/`.

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/test_api_profile_transfer_concurrency.py -v`
Expected: FAIL on the first test — no successful response names the final owner. If instead it ERRORs on a missing fixture, that is the false-pass signature, not a pass.

- [ ] **Step 3: Implement**

In `_profile_for_edit`, load the row `FOR UPDATE`. Read the existing body first and keep its 404/403 logic exactly; only the load changes:

```python
    # FOR UPDATE, not a bare get. All five callers of this helper (the four
    # sharing operations and transfer) mutate sharing state on this row, and
    # without the lock two of them can both read owner A, both pass the
    # authorization check below, and the second then computes its effects for
    # an owner that no longer holds the profile.
    #
    # The lock lives here rather than in the transfer handler on purpose:
    # locking one caller would leave the other four racing it.
    p = session.execute(
        select(Profile).where(Profile.id == profile_id).with_for_update()
    ).scalar_one_or_none()
```

Confirm `select` is imported in this module (it is used elsewhere in the file).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_api_profile_transfer_concurrency.py tests/test_api_profile_transfer.py -v`
Expected: PASS, including the pre-existing transfer tests.

- [ ] **Step 5: Confirm no lock-ordering edge was added**

The documented order is `mutex < jobs < workspaces < calls`. Grep the five callers for any second row lock taken while this one is held:

Run: `grep -n 'with_for_update' src/grant_lab/web/api/profiles.py`
Expected: exactly **one** hit, in `_profile_for_edit`. More than one means a caller takes a second lock and the ordering claim needs checking before this ships.

- [ ] **Step 6: Prove the gate bites**

Revert the change to a plain `session.get(Profile, profile_id)`.
Run: `uv run pytest tests/test_api_profile_transfer_concurrency.py -v`
Expected: `test_two_concurrent_transfers_do_not_both_authorize_against_the_old_owner` FAILS.
**Restore** and re-run.

- [ ] **Step 7: Commit**

```bash
uv run ruff check src tests
git add src/grant_lab/web/api/profiles.py tests/test_api_profile_transfer_concurrency.py
git commit -m "fix(profiles): sharing mutations serialize on the profile row"
```

---

### Task 2: F4 — an embedding-model swap must invalidate rankings

**Files:**
- Modify: `src/grant_lab/profiles/fingerprint.py:23` and `:110-118`
- Test: `tests/test_profile_fingerprint.py` (extend)

**Interfaces:**
- Consumes: `Profile.embedding_model`.
- Produces: `FINGERPRINT_VERSION = "v2"`; `rank_fingerprint`'s payload gains `"embedding_model"`. Every fingerprint string is now `v2:`-prefixed.

**What this costs.** The version bump invalidates every stored fingerprint, forcing one full re-embed and re-rank. Migration `0075` already forces that on its first night, so this is free before the first deploy — see "Release timing" above.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_profile_fingerprint.py`:

```python
def test_a_model_swap_changes_the_rank_fingerprint():
    """The embedding repair predicate knows about model identity; the ranking
    one inherited only a text hash. So swapping EMBEDDING_MODEL replaced every
    vector, left both fingerprints byte-identical, and reconcile treated every
    ranking as current while its stored cosine described the old vectors.

    This repo has already swapped embedding models once (mxbai -> bge-m3)."""
    a = _profile()
    a.embedding_fingerprint = "v2:same"
    a.embedding_model = "mxbai-embed-large"
    b = _profile()
    b.embedding_fingerprint = "v2:same"
    b.embedding_model = "bge-m3"
    assert rank_fingerprint(a) != rank_fingerprint(b)


def test_the_version_tag_is_v2():
    p = _profile()
    assert embedding_fingerprint(p).startswith("v2:")
    assert rank_fingerprint(p).startswith("v2:")
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_profile_fingerprint.py -v`
Expected: both FAIL — the model swap gives equal hashes, and the prefix is `v1:`.

- [ ] **Step 3: Implement**

```python
FINGERPRINT_VERSION = "v2"
```

and in `rank_fingerprint`'s payload, after `"embedding_fingerprint"`:

```python
            "embedding_fingerprint": profile.embedding_fingerprint,
            # The MODEL as well as the fingerprint. embedding_fingerprint
            # digests build_profile_text's output, which is text only, so
            # swapping EMBEDDING_MODEL replaces every vector while leaving that
            # hash byte-identical -- and reconcile would then treat every
            # ranking as current while its stored embed_similarity describes
            # the previous vectors. The embedding repair predicate already
            # knows about model identity; this is what carries it into ranking
            # freshness.
            "embedding_model": profile.embedding_model,
```

Update the module docstring's version note: adding this key is exactly the deliberate, visible payload change the version tag exists to force.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_profile_fingerprint.py tests/test_ranking_reconcile_staleness.py tests/test_embeddings_refresh_fingerprint.py -v`
Expected: PASS. Any test asserting a literal `v1:` string is now wrong and must be updated to `FINGERPRINT_VERSION`, not to a hardcoded `v2:`.

- [ ] **Step 5: Prove both gates bite**

Remove `"embedding_model"` from the payload.
Expected: `test_a_model_swap_changes_the_rank_fingerprint` FAILS.
Restore, then set `FINGERPRINT_VERSION = "v1"`.
Expected: `test_the_version_tag_is_v2` FAILS.
**Restore both** and re-run.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src tests
git add src/grant_lab/profiles/fingerprint.py tests/test_profile_fingerprint.py
git commit -m "fix(freshness): a model swap invalidates rankings, not just vectors"
```

---

### Task 3: F3 — profile names stop confirming what a caller cannot read

**Files:**
- Create: `alembic/versions/0076_owner_scoped_profile_names.py`
- Modify: `src/grant_lab/db/models/profiles.py:52-57` (the index)
- Modify: `src/grant_lab/web/api/profiles.py:365-369` (the pre-check)
- Modify: `docs/ARCHITECTURE.md` (generated)
- Test: `tests/test_api_profile_name_privacy.py`

**Interfaces:**
- Consumes: nothing.
- Produces: index `uq_profiles_active_owner_name` on `(created_by, name)` where `is_active = true`. `uq_profiles_active_name` is gone.

**Scoping the query alone is not enough.** `db/models/profiles.py:52-56` carries `uq_profiles_active_name`, a partial UNIQUE index on `name` where `is_active`. Postgres would raise on insert even with a scoped pre-check, so the leak would move from a 409 to a 500. The index and the handler must change together.

- [ ] **Step 1: Write the failing tests**

These three are **one set**. Any one alone passes a wrong implementation: 1 proves the leak is closed, 2 proves the constraint still does its job, 3 proves the scope actually changed.

```python
def test_a_name_held_by_an_unreadable_private_profile_does_not_409(
    client, make_user, make_profile, auth_headers_for
):
    """GATE 8. profiles.py:250-256 states the rule this closes: "A 403 confirms
    the profile exists, and profile NAMES are the thing worth not confirming."
    The detail route returns 404 for that reason; creation used to confirm the
    same fact by another door."""
    other = make_user(email="other@umu.se")
    make_profile(owner=other, name="Hepatology", visibility="private")
    me = make_user(email="me@umu.se")

    r = client.post(
        "/api/profiles",
        json={"name": "Hepatology", "kind": "research_group"},
        headers=auth_headers_for(me),
    )
    assert r.status_code != 409, "creation confirmed a profile the caller cannot read"
    assert r.status_code < 400, r.text


def test_a_duplicate_name_the_caller_DOES_own_still_409s(
    client, make_user, make_profile, auth_headers_for
):
    """GATE 9. Without this, deleting the check entirely passes gate 8."""
    me = make_user(email="me@umu.se")
    make_profile(owner=me, name="Hepatology")
    r = client.post(
        "/api/profiles",
        json={"name": "Hepatology", "kind": "research_group"},
        headers=auth_headers_for(me),
    )
    assert r.status_code == 409


def test_two_owners_may_both_hold_the_same_active_name(session, make_user, make_profile):
    """GATE 10. Without this, keeping the index global passes gate 9, and
    gate 8 would fail at the DATABASE with a 500 rather than a 409."""
    a = make_user(email="a@umu.se")
    b = make_user(email="b@umu.se")
    make_profile(owner=a, name="Hepatology")
    make_profile(owner=b, name="Hepatology")
    session.commit()  # must not raise IntegrityError
```

`make_profile(owner=..., name=..., visibility=...)` and `auth_headers_for(user)` are
**assumed and unverified**. Check them against `tests/conftest.py` and
`tests/test_api_profile_transfer.py` first; the existing profile tests already build
two users with different visibility, so copy that rather than inventing a fixture.

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_api_profile_name_privacy.py -v`
Expected: gate 8 FAILS with 409; gate 10 FAILS with an `IntegrityError` on `uq_profiles_active_name`.

- [ ] **Step 3: Write the migration**

```python
"""owner-scoped profile-name uniqueness

A globally-unique active name let any authenticated caller confirm the NAME of a
private profile they cannot read, by POSTing it and reading the 409 -- eight lines
below a comment in the same file stating that profile names are the thing worth not
confirming.

Relaxing a uniqueness constraint cannot fail on existing rows in either direction of
data, so this migration is safe whatever is already stored.

Revision ID: 0076
Revises: 0075
"""

from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_profiles_active_name", table_name="profiles")
    op.create_index(
        "uq_profiles_active_owner_name",
        "profiles",
        ["created_by", "name"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )


def downgrade() -> None:
    # Re-creating the GLOBAL index can fail if two owners now hold the same
    # active name -- which this migration exists to permit. Deliberately not
    # papered over: a downgrade must be a decision about that data, not a
    # silent failure at 3am.
    op.drop_index("uq_profiles_active_owner_name", table_name="profiles")
    op.create_index(
        "uq_profiles_active_name",
        "profiles",
        ["name"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )
```

Add `import sqlalchemy as sa` at the top. Read `alembic/versions/0075_freshness_fingerprints.py` first and match its import and header style.

- [ ] **Step 4: Update the ORM index**

Replace the `uq_profiles_active_name` Index in `db/models/profiles.py` (keep the surrounding comment about inactive profiles reusing a name, and add why it is owner-scoped):

```python
        # Partial UNIQUE on active names, scoped to the OWNER. Inactive
        # profiles (is_active=false) can reuse the name -- that's why it's
        # partial. See alembic/versions/0009_* and 0076_*.
        #
        # `created_by` is in the key because a GLOBAL unique active name made
        # creation an existence oracle: POST a guessed name, read the 409, and
        # you have confirmed a private profile you cannot read. The 409 can now
        # only ever name a profile the caller already owns.
        Index(
            "uq_profiles_active_owner_name",
            "created_by",
            "name",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
```

- [ ] **Step 5: Scope the handler's pre-check**

At `profiles.py:365-369`, add the owner predicate so the pre-check and the index agree:

```python
        existing = session.execute(
            select(Profile.id).where(
                Profile.name == body.name,
                Profile.is_active.is_(True),
                # Scoped to the caller, matching uq_profiles_active_owner_name.
                # The pre-check and the index must agree: a mismatch is how
                # this defect happened -- the handler and the schema each
                # enforced something, and neither enforced what :250-256 says.
                Profile.created_by == user.id,
            )
        ).scalar_one_or_none()
```

Confirm the handler has `user` in scope at that point; if the identifier differs, use the real one.

- [ ] **Step 6: Regenerate and run**

```bash
uv run python scripts/gen_architecture.py
uv run pytest tests/test_api_profile_name_privacy.py tests/architecture tests/test_migration_0076_owner_scoped_profile_names.py -v
```

Expected: PASS. Write the migration test in the shape of `tests/test_migration_0075_freshness_fingerprints.py`, asserting the old index is gone and the new one exists.

- [ ] **Step 7: Prove the set bites**

Run each mutation, restoring between:

| Mutation | Must redden |
|---|---|
| restore the unscoped pre-check | gate 8 |
| delete the pre-check entirely | gate 9 |
| keep the index global (revert step 4 and the migration's `upgrade`) | gate 10 |

If gate 8 stays green under the first mutation, the fixture's other profile is not actually unreadable by the caller and the test proves nothing.

- [ ] **Step 8: Commit**

```bash
uv run ruff check src tests alembic
git add alembic/versions/0076_owner_scoped_profile_names.py \
        src/grant_lab/db/models/profiles.py src/grant_lab/web/api/profiles.py \
        docs/ARCHITECTURE.md tests/test_api_profile_name_privacy.py \
        tests/test_migration_0076_owner_scoped_profile_names.py
git commit -m "fix(profiles): a name collision no longer confirms a profile you cannot read"
```

---

### Task 4: F1 — the anonymous board encodes nothing

**Files:**
- Create: `alembic/versions/0077_board_item_qr.py`
- Modify: `src/grant_lab/db/models/boards.py` (`BoardItem`)
- Modify: `src/grant_lab/web/api/boards.py:680` (the `BoardItem(...)` construction)
- Modify: `src/grant_lab/boards/public.py` (`PublicBoardRow`, `public_board_items`)
- Modify: `src/grant_lab/web/api/public_boards.py:132`
- Modify: `docs/ARCHITECTURE.md` (generated)
- Test: `tests/test_boards_qr_at_approval.py`

**Interfaces:**
- Consumes: `qr_for` from `grant_lab.boards.qr`.
- Produces: `BoardItem.qr_path: Mapped[str | None]`, `BoardItem.qr_modules: Mapped[int]` (default 0), `BoardItem.qr_source_url: Mapped[str | None]`. `PublicBoardRow` gains `qr_modules: int` and `qr_path: str`.

**The defect.** `public_boards.py:132` calls `qr_for(row.url)` on the anonymous path, and `qr.py:313-314` takes `_encode_lock` **per URL, globally**. With `maxsize=256` and a 192-item board ceiling, two published boards with disjoint URL sets thrash the cache and land every request on the cold path. `qr.py:95-118` describes that scenario and concludes the cost is "ONE-TIME, COLD" — true only while the working set fits the cache, which two max-shape boards do not.

- [ ] **Step 1: Write the failing tests**

```python
from grant_lab.boards.qr import qr_for
from grant_lab.db.models import BoardItem


def test_the_public_route_encodes_nothing(client_unauth, published_board_with_items):
    """GATE 1 -- the load-bearing one. Asserting the response is CORRECT would
    pass with the encode still on the hot path. Assert on cache misses."""
    slug = published_board_with_items.slug
    qr_for.cache_clear()
    before = qr_for.cache_info().misses
    r = client_unauth.get(f"/api/public/boards/{slug}")
    assert r.status_code == 200
    assert qr_for.cache_info().misses == before, (
        "the anonymous route ran the encoder"
    )


def test_approval_stores_the_qr_and_its_source_url(session, client, board, auth_headers, make_call):
    """GATE 2."""
    call = make_call(url="https://example.org/call/1")
    session.commit()
    r = client.post(
        f"/api/boards/{board.id}/items",
        json={"call_ids": [call.id]},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    item = session.scalar(
        select(BoardItem).where(BoardItem.board_id == board.id, BoardItem.call_id == call.id)
    )
    assert item.qr_modules > 0
    assert item.qr_path
    assert item.qr_source_url == "https://example.org/call/1"


def test_an_overlong_url_stores_no_qr_and_the_card_still_renders(
    session, client, client_unauth, board, auth_headers, make_call, publish
):
    """GATE 3. qr.py returns modules=0 above _MAX_URL_BYTES; the frontend
    already treats qr_modules === 0 as "no <svg>"."""
    call = make_call(url="https://example.org/" + "x" * 900)
    session.commit()
    client.post(f"/api/boards/{board.id}/items", json={"call_ids": [call.id]},
                headers=auth_headers)
    item = session.scalar(select(BoardItem).where(BoardItem.call_id == call.id))
    assert item.qr_modules == 0

    publish(board)
    body = client_unauth.get(f"/api/public/boards/{board.slug}").json()
    card = [i for i in body["items"] if i["url"].startswith("https://example.org/x")][0]
    assert card["qr_modules"] == 0
    assert card["title"]  # the card still renders


def test_a_url_changed_after_approval_serves_no_qr_rather_than_a_stale_one(
    session, client, client_unauth, board, auth_headers, make_call, publish
):
    """GATE 4. A stored QR is derived data with the same exists-versus-current
    problem the freshness release just fixed for embeddings. A QR pointing
    somewhere the call no longer lives is worse than no QR -- it is on a wall."""
    from grant_lab.db.models import Call

    call = make_call(url="https://example.org/call/1")
    session.commit()
    client.post(f"/api/boards/{board.id}/items", json={"call_ids": [call.id]},
                headers=auth_headers)
    publish(board)

    session.get(Call, call.id).url = "https://example.org/call/MOVED"
    session.commit()

    body = client_unauth.get(f"/api/public/boards/{board.slug}").json()
    card = [i for i in body["items"] if i["url"].endswith("MOVED")][0]
    assert card["qr_modules"] == 0, "served a QR pointing at the old url"
```

**Do NOT use the fixture names above verbatim — they were wrong when this plan was
written, and checking is the point.** Measured on the base tree:

- **`client_unauth` is NOT available here.** It is defined at
  `tests/proposal_assist/conftest.py:129`, which is package-scoped to
  `tests/proposal_assist/` and invisible to a test under `tests/`.
- The real public-board tests use a different pattern entirely:
  `_setup(monkeypatch, pg_url)` to build the app, `profile_factory(...)`,
  `_make_board(pg_session_factory, *, profile_id, created_by, slug, is_published=True, ...)`,
  and a plain `with TestClient(app) as c:` for the anonymous client — see
  `tests/web/test_api_public_board.py:59`, `:76`, `:173`.

**Write these tests in `tests/web/test_api_public_board.py`'s own idiom**, reusing
`_setup` and `_make_board` from that module rather than inventing fixtures. The
freshness dry run found eleven of twelve assumed fixtures did not exist; this is the
same trap, and it has already caught this plan once.

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_boards_qr_at_approval.py -v`
Expected: gate 1 FAILS (misses increased); gates 2-4 FAIL on a missing `qr_path` attribute.

- [ ] **Step 3: Migration `0077`**

Three nullable-or-defaulted columns, then a backfill that renders each approved item's QR:

```python
"""store a board item's QR at approval instead of rendering it on the public path

Revision ID: 0077
Revises: 0076
"""

revision = "0077"
down_revision = "0076"
```

`upgrade()` adds `qr_path TEXT NULL`, `qr_modules INTEGER NOT NULL SERVER_DEFAULT '0'`, `qr_source_url TEXT NULL`, then backfills in Python by joining `board_items` to `calls` and calling `qr_for(url)` per row. `board_items` is empty in production today (boards shipped 2026-09-06 and have not deployed), so the backfill is a no-op there — write it for the general case anyway and **measure the row count before running it elsewhere**.

`downgrade()` drops all three.

- [ ] **Step 4: The ORM columns**

On `BoardItem`, after `approved_at`:

```python
    # The QR, rendered ONCE at approval by an admin in an authenticated,
    # rate-limited request -- not on the anonymous /b/:slug path, where
    # qr.py's per-URL global _encode_lock made two alternating max-shape
    # boards a cache-thrash amplifier.
    qr_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 0 means "no QR" -- the signal the frontend already honours
    # (qr_modules === 0 => no <svg>).
    qr_modules: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # The Call.url this QR was rendered from. NOT bookkeeping: a stored QR is
    # derived data with the same exists-versus-current problem the freshness
    # release fixed for embeddings, and a QR on a corridor wall pointing where
    # the call no longer lives is worse than no QR. The public route compares
    # this and serves qr_modules=0 on a mismatch.
    qr_source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 5: Render at approval**

`boards.py:680` builds `BoardItem(...)` from a bare `cid` with no `Call` loaded. Load the URLs for `new_ids` first, then render:

```python
            urls = dict(
                session.execute(
                    select(Call.id, Call.url).where(Call.id.in_(new_ids))
                ).all()
            )
            for offset, cid in enumerate(new_ids):
                url = urls.get(cid) or ""
                qr = qr_for(url)
                session.add(
                    BoardItem(
                        board_id=board.id,
                        call_id=cid,
                        position=next_position + offset,
                        approved_by=user.id,
                        qr_path=qr.path or None,
                        qr_modules=qr.modules,
                        qr_source_url=url or None,
                    )
                )
```

- [ ] **Step 6: Return the stored QR, and compare the URL**

In `boards/public.py`, add `qr_modules: int` and `qr_path: str` to `PublicBoardRow`, select `BoardItem` alongside `Call` and `Source.name`, and build each row with the **comparison**:

```python
        PublicBoardRow(
            ...,
            url=call.url,
            # Serve the stored QR only while it still describes this url.
            qr_modules=(item.qr_modules if item.qr_source_url == call.url else 0),
            qr_path=(item.qr_path or "") if item.qr_source_url == call.url else "",
        )
```

In `public_boards.py`, delete the `qr = qr_for(row.url)` line and read `row.qr_modules` / `row.qr_path` instead. Remove the now-unused `qr_for` import.

- [ ] **Step 7: Regenerate, run, and prove the gates bite**

```bash
uv run python scripts/gen_architecture.py
uv run pytest tests/test_boards_qr_at_approval.py tests/web/test_api_public_board.py tests/test_boards_qr.py tests/architecture -v
```

Then each mutation, restoring between:

| Mutation | Must redden |
|---|---|
| restore `qr = qr_for(row.url)` in `get_public_board` | gate 1 |
| drop the three `qr_*` kwargs from the `BoardItem(...)` | gate 2 |
| store `qr.path` unconditionally, ignoring `qr.modules` | gate 3 |
| drop the `item.qr_source_url == call.url` comparison | gate 4 |

- [ ] **Step 8: Full suite and commit**

```bash
uv run pytest -n 16 --dist loadfile
```

Expected: **more than 5412 passed, exactly 1 skipped.** Measure the new total; do not predict it. `0 skipped` means a live API call ran — stop.

```bash
uv run ruff check src tests alembic scripts
git add -A
git commit -m "fix(boards): the anonymous board reads a stored QR instead of encoding one"
```

---

## Self-review notes

**Spec coverage.** F2 → Task 1, F4 → Task 2, F3 → Task 3, F1 → Task 4. All twelve spec gates appear: 1-5 in Task 4, 6-7 in Task 1, 8-10 in Task 3, 11-12 in Task 2.

**Task independence.** No task consumes another's output; the order is smallest-blast-radius first. Tasks 3 and 4 each add a migration (`0076`, `0077` respectively) and must land in that order for the revision chain.

**Discriminating controls.** Task 3's gates 8/9/10 are a set — noted in the task itself. Task 1's second test (transfer vs viewer-add) is the control proving the lock is in the helper rather than the handler. Task 4's gate 3 is the control proving gate 2 did not simply store everything unconditionally.

**Fixtures are the known risk.** The freshness dry run found eleven of twelve assumed fixtures did not exist (its D2). Every task here names its assumed fixtures and instructs verification first. Extract and run the plan's code blocks before trusting them.

**Type consistency.** `qr_for(url) -> QrCode` with `.modules` and `.path` is used in Tasks 4 steps 5 and 6 as defined in `boards/qr.py:181`. `FINGERPRINT_VERSION` is defined in Task 2 and referenced in its own tests only. `_profile_for_edit(session, *, profile_id, user) -> Profile` keeps its signature in Task 1.
