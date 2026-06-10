# P2 — User-System Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace open self-registration with an admin-controlled, approval-based user system: system roles (admin/staff/read_only), lifecycle (pending/active/suspended), workspace collaboration roles (owner/editor/viewer), an admin Users panel, and owner transfer — all enforced server-side.

**Architecture:** One Alembic migration adds `role`/`status` to `users` and `role` to `workspace_viewers`, drops the `is_admin` column in favour of a read-only `@property`. The pending queue is simply `users WHERE status='pending'` (no separate table). Authentication gains an authoritative `status='active'` check in `get_current_user`; login becomes pending-on-first-request. All user-state transitions live in one serialized service. Workspace authorization is centralized into one guard module with three tiers (member/editor/owner), replacing five duplicated helpers, and every route is remapped.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, Jinja2 + HTMX, pytest, PostgreSQL 16.

**Source spec:** `docs/superpowers/specs/2026-06-10-user-system-and-security-hardening-design.md`.

**Prerequisite:** P1 may land first or in parallel; P2 does not depend on it. P2 **does** touch `post_turn`'s guard (Task 8) — if P1's disclosure gate is already in `post_turn`, keep it; this plan only changes the `_require_owner`→`_require_editor` call there.

**Conventions:** Tests mirror the package path under `tests/`. Migrations live in `alembic/versions/`, named `NNNN_<slug>.py`, `revision`/`down_revision` as strings, `from __future__ import annotations`, `import sqlalchemy as sa`, `from alembic import op`, `branch_labels = None`, `depends_on = None`. Enums are **Text + CheckConstraint** named `ck_<table>_<desc>` (never native ENUM — migration 0034 is the cautionary tale). Commit style: `feat(area): …`. Run tests: `uv run pytest`. DB tests use `pg_session_factory`; web tests use `client_authed_owner` and the `make_user`/`make_workspace` helpers in `tests/proposal_assist/conftest.py`; `tests/conftest.py` has a profile/user factory at ~line 116.

---

## Why these decisions (recap from spec §13)

- Pending queue = `status='pending'` rows; no `approval_requests` table (the migration adds only columns).
- `status` server_default **must** be `'active'` and `role` default `'staff'`, or existing rows and ~1273 test fixtures become locked-out/pending.
- `is_admin` becomes a read-only `@property` (`role == 'admin'`); every **write/query** site must move to `role` *before* the column is dropped (a property is invisible to SQLAlchemy SQL).
- Suspending an owner must not strand collaborators → owner transfer + `actor.status='active'` write gate.

---

## File structure

| File | Responsibility | Tasks |
|---|---|---|
| `src/call_manager/db/models.py` | `User.role/status` + `is_admin` property; `WorkspaceViewer.role` | 1 |
| `alembic/versions/0042_user_roles_status.py` (new) | the migration | 1 |
| `src/call_manager/admin_bootstrap.py` | write `role`/`status` not `is_admin` | 2 |
| `scripts/seed_dev_data.py` | query `User.role` not `User.is_admin` | 2 |
| `tests/conftest.py`, `tests/proposal_assist/conftest.py`, ~15 test files | fixture `is_admin=` → `role=` | 2 |
| `src/call_manager/auth/dependencies.py` | status gate + `require_staff` | 3 |
| `src/call_manager/web/routes/auth.py` | pending-on-request login; verify refuses non-active | 4 |
| `src/call_manager/web/templates/login_sent.html` | neutral message covering both outcomes | 4 |
| `src/call_manager/users/service.py` (new) | all user-state transitions, serialized + guarded | 5 |
| `src/call_manager/web/routes/admin.py` | `/admin/users/*` routes | 6 |
| `src/call_manager/web/templates/admin_users.html` (new) | Users panel UI | 6 |
| `src/call_manager/web/templates/admin.html` | Users nav link | 6 |
| `src/call_manager/proposal_assist/web/_guards.py` (new) | `_require_member/_require_editor/_require_owner/_require_owner_or_admin` | 7 |
| 7 router files | import centralized guards; flip per route map | 7, 8 |
| `src/call_manager/proposal_assist/web/workspaces.py` | `add_viewer` role param; `transfer_owner`; `soft_delete` admin | 9 |
| `src/call_manager/web/templates/workspace_settings.html` | role select + transfer form | 9 |
| ~10 templates | `is_owner`→`can_edit`/`can_manage`; `can_create` gates | 10 |
| route handlers passing `is_owner` | also pass `can_edit`/`can_manage`/`can_create` | 10 |
| `src/call_manager/digest/send.py` | filter candidates on `status='active'` | 11 |

---

## Task 1: Migration 0042 + model columns

**Files:**
- Modify: `src/call_manager/db/models.py` (User class lines 35-71; WorkspaceViewer lines 641-658)
- Create: `alembic/versions/0042_user_roles_status.py`
- Test: `tests/test_migration_0042_roles_status.py`

- [ ] **Step 1: Write the failing model test**

```python
# tests/test_migration_0042_roles_status.py
"""User.role/status columns, is_admin property, WorkspaceViewer.role."""
from call_manager.db.models import User, WorkspaceViewer


def test_is_admin_property_reflects_role():
    assert User(email="a@ki.se", role="admin").is_admin is True
    assert User(email="b@ki.se", role="staff").is_admin is False
    assert User(email="c@ki.se", role="read_only").is_admin is False


def test_is_admin_is_read_only_property():
    u = User(email="d@ki.se", role="staff")
    import pytest
    with pytest.raises(AttributeError):
        u.is_admin = True  # property has no setter — writes must use role


def test_defaults():
    # Server defaults apply at flush; the Python-side mapped defaults here
    # mirror them so unit construction matches DB behaviour.
    u = User(email="e@ki.se")
    assert (u.role or "staff") == "staff"
    assert (u.status or "active") == "active"
    wv = WorkspaceViewer(workspace_id=__import__("uuid").uuid4(), user_id=1)
    assert (wv.role or "viewer") == "viewer"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_migration_0042_roles_status.py -v`
Expected: FAIL — `is_admin` is still a column / `role` attr missing.

- [ ] **Step 3: Edit the `User` model**

In `src/call_manager/db/models.py`, replace the `is_admin` column line (line 50):

```python
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
```
with:
```python
    role: Mapped[str] = mapped_column(Text, nullable=False, default="staff", server_default="staff")
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="active", server_default="active"
    )
    approved_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

Add to the `User.__table_args__` tuple (after the existing `ck_users_email_lowercase` CheckConstraint at line 44):
```python
        CheckConstraint("role IN ('admin','staff','read_only')", name="ck_users_role"),
        CheckConstraint(
            "status IN ('pending','active','suspended')", name="ck_users_status"
        ),
```

Add the read-only property at the end of the `User` class body (after the `magic_link_tokens` relationship, line 70):
```python
    @property
    def is_admin(self) -> bool:
        """Read-only compatibility shim. Templates and require_admin read this;
        the source of truth is the `role` column. NO setter — writes use `role`."""
        return self.role == "admin"
```

Confirm `Text` is imported in `models.py` (it is — used by other status columns; `grep -n "Text" src/call_manager/db/models.py | head`).

- [ ] **Step 4: Edit the `WorkspaceViewer` model**

In the `WorkspaceViewer` class (lines 641-658), add a `role` column and a CheckConstraint to its `__table_args__`:
```python
    role: Mapped[str] = mapped_column(
        Text, nullable=False, default="viewer", server_default="viewer"
    )
```
and in `__table_args__`:
```python
        CheckConstraint("role IN ('editor','viewer')", name="ck_workspace_viewers_role"),
```
> If `WorkspaceViewer` has no `__table_args__` yet, add one as a tuple containing this CheckConstraint.

- [ ] **Step 5: Run the model test**

Run: `uv run pytest tests/test_migration_0042_roles_status.py -v`
Expected: PASS.

- [ ] **Step 6: Write the migration**

```python
# alembic/versions/0042_user_roles_status.py
"""User roles + status; workspace_viewers.role; drop is_admin.

Revision ID: 0042
Revises: 0041
Create Date: 2026-06-10

Spec: docs/superpowers/specs/2026-06-10-user-system-and-security-hardening-design.md §8
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # users.status / users.role — add nullable + default, backfill, constrain, NOT NULL.
    op.add_column(
        "users", sa.Column("status", sa.Text(), nullable=True, server_default="active")
    )
    op.add_column(
        "users", sa.Column("role", sa.Text(), nullable=True, server_default="staff")
    )
    op.execute("UPDATE users SET status = 'active' WHERE status IS NULL")
    op.execute(
        "UPDATE users SET role = CASE WHEN is_admin THEN 'admin' ELSE 'staff' END"
    )
    op.create_check_constraint(
        "ck_users_status", "users", "status IN ('pending','active','suspended')"
    )
    op.create_check_constraint(
        "ck_users_role", "users", "role IN ('admin','staff','read_only')"
    )
    op.alter_column("users", "status", nullable=False)
    op.alter_column("users", "role", nullable=False)
    op.add_column("users", sa.Column("approved_by", sa.BigInteger(), nullable=True))
    op.add_column(
        "users", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_users_approved_by", "users", "users", ["approved_by"], ["id"],
        ondelete="SET NULL",
    )
    # workspace_viewers.role — server_default backfills existing rows to 'viewer'.
    op.add_column(
        "workspace_viewers",
        sa.Column("role", sa.Text(), nullable=False, server_default="viewer"),
    )
    op.create_check_constraint(
        "ck_workspace_viewers_role", "workspace_viewers", "role IN ('editor','viewer')"
    )
    # Drop is_admin LAST (role was backfilled from it).
    op.drop_column("users", "is_admin")


def downgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute("UPDATE users SET is_admin = (role = 'admin')")
    op.execute(
        "ALTER TABLE workspace_viewers DROP CONSTRAINT IF EXISTS ck_workspace_viewers_role"
    )
    op.drop_column("workspace_viewers", "role")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS fk_users_approved_by")
    op.drop_column("users", "approved_at")
    op.drop_column("users", "approved_by")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_role")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_status")
    op.drop_column("users", "role")
    op.drop_column("users", "status")
```

- [ ] **Step 7: Apply the migration and round-trip it**

Run:
```bash
DATABASE_URL="postgresql+psycopg://call_manager:call_manager@localhost:5432/call_manager" uv run alembic upgrade head
DATABASE_URL="...same..." uv run alembic downgrade -1
DATABASE_URL="...same..." uv run alembic upgrade head
```
Expected: `0041 -> 0042` then `0042 -> 0041` then `0041 -> 0042`, no errors. (Requires the dev DB up: `docker compose up -d`.)

- [ ] **Step 8: Commit** (model + migration; tests still red until Task 2 fixes fixtures — that's expected and noted)

```bash
git add src/call_manager/db/models.py alembic/versions/0042_user_roles_status.py tests/test_migration_0042_roles_status.py
git commit -m "feat(users): add role/status columns + is_admin property (migration 0042)"
```

---

## Task 2: Fix `is_admin` write/query sites + test fixtures

A `@property` is invisible to SQLAlchemy SQL. These sites **break** once the column is dropped and must move to `role`. Do this immediately after Task 1 so the suite is green again.

**Files:**
- Modify: `src/call_manager/admin_bootstrap.py` (lines 49-53)
- Modify: `scripts/seed_dev_data.py` (line ~286)
- Modify: `tests/conftest.py`, `tests/proposal_assist/conftest.py`, and the `User(is_admin=…)` construction sites listed below.

- [ ] **Step 1: Fix `admin_bootstrap.py`**

Replace the create/promote block (lines 49-53):
```python
        for email in valid:
            user = session.query(User).filter_by(email=email).one_or_none()
            if user is None:
                session.add(User(email=email, is_admin=True))
            elif not user.is_admin:
                user.is_admin = True
```
with:
```python
        for email in valid:
            user = session.query(User).filter_by(email=email).one_or_none()
            if user is None:
                session.add(User(email=email, role="admin", status="active"))
            else:
                user.role = "admin"
                user.status = "active"
```

- [ ] **Step 2: Fix `seed_dev_data.py`**

Change the admin query at line ~286 from:
```python
        select(User).where(User.is_admin.is_(True)).order_by(User.id).limit(1)
```
to:
```python
        select(User).where(User.role == "admin").order_by(User.id).limit(1)
```
And any `User(is_admin=True)` construction in that file → `User(role="admin", status="active")`.

- [ ] **Step 3: Fix the test fixture factories**

In `tests/conftest.py` (~line 116) and `tests/proposal_assist/conftest.py` (`make_user`, ~line 171): wherever a factory accepts an `is_admin` kwarg or constructs `User(...)`, translate it. The cleanest pattern — keep callers' `is_admin=True` working by mapping it internally:
```python
def make_user(session, *, email, is_admin=False, role=None, status="active", **kw):
    resolved_role = role or ("admin" if is_admin else "staff")
    user = User(email=email, role=resolved_role, status=status, **kw)
    session.add(user)
    session.flush()
    return user
```
> Inspect each factory's real signature first and apply the same idea (default `status="active"`, derive `role` from `is_admin` when only that was passed). This keeps the ~60 inline call sites that pass `is_admin=True`/nothing working unchanged.

- [ ] **Step 4: Fix direct `User(is_admin=…)` constructions in tests**

These files construct `User(is_admin=…)` directly (per the migration review) — change each `is_admin=True`→`role="admin", status="active"` and `is_admin=False`→`role="staff", status="active"`:
`tests/test_admin_bootstrap.py:16`, `tests/test_web_admin.py:21`, `tests/test_web_admin_ranking.py:19,89`, `tests/test_web_admin_embedding_coverage.py:28`, `tests/test_web_admin_drafting_panel.py:44,60`, `tests/test_models.py:12`, `tests/test_web_me.py:110`, the analysis tests (4×), the editing tests (2×), `tests/test_auth_dependencies.py:89,104`, and the raw INSERT in `tests/test_alembic_v2_round_trip.py:63` (use `role`,`status` columns, no `is_admin`).
> Find them all: `grep -rn "is_admin" tests/`. Every construction/kwarg/raw-INSERT site moves to `role`; read-only assertions like `assert user.is_admin` stay (the property still works).

- [ ] **Step 5: Run the previously-red migration test + the broad suite**

Run: `uv run pytest tests/test_migration_0042_roles_status.py tests/test_admin_bootstrap.py tests/test_auth_dependencies.py -v`
then: `uv run pytest -q`
Expected: green. If a test still references the dropped column in a query, fix it the same way (`User.role == 'admin'`).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(users): migrate is_admin writes/queries + fixtures to role column"
```

---

## Task 3: Authoritative status gate + `require_staff`

**Files:**
- Modify: `src/call_manager/auth/dependencies.py`
- Test: `tests/test_auth_dependencies.py` (extend)

- [ ] **Step 1: Write failing tests**

```python
# add to tests/test_auth_dependencies.py
"""get_current_user rejects non-active users even with a valid same-epoch
cookie; require_staff blocks read_only. Spec §4.2."""
import pytest
from fastapi import HTTPException


def test_get_current_user_rejects_suspended(app_request_with_cookie, make_user, pg_session_factory):
    # make_user(..., status="suspended") then present a freshly-minted, valid,
    # same-epoch cookie. Must 401 on status, not pass.
    ...  # build using the existing helpers in this file


def test_require_staff_blocks_read_only(app_request_with_cookie, make_user):
    ...  # read_only user -> require_staff raises 403
```
> This file already exercises `get_current_user`/`require_admin` (it constructs users + cookies). Mirror its existing setup; add a `status="suspended"` case and a `role="read_only"` + `require_staff` case.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_auth_dependencies.py -k "suspended or read_only" -v`
Expected: FAIL (no status gate / no `require_staff` yet).

- [ ] **Step 3: Add the status gate and `require_staff`**

In `src/call_manager/auth/dependencies.py`, inside `get_current_user`, after the `session_epoch` check (line 26) and before `db.expunge(user)`:
```python
        # Authoritative account-state gate: pending and suspended users are
        # never authenticated, even with a valid same-epoch cookie. This is the
        # single chokepoint every route (incl. require_admin/require_staff)
        # funnels through. Spec §4.2.
        if user.status != "active":
            raise HTTPException(status_code=401, detail="account inactive")
```
Add a new dependency after `require_admin`:
```python
def require_staff(request: Request, session: str | None = Cookie(default=None)) -> User:
    """Gate routes that read_only users must not reach (create profile/workspace)."""
    user = get_current_user(request, session=session)
    if user.role not in ("staff", "admin"):
        raise HTTPException(status_code=403, detail="staff access required")
    return user
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_auth_dependencies.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/auth/dependencies.py tests/test_auth_dependencies.py
git commit -m "feat(auth): authoritative status=active gate + require_staff dependency"
```

---

## Task 4: Pending-on-request login

**Files:**
- Modify: `src/call_manager/web/routes/auth.py` (`login_submit` lines 142-152; `login_verify` lines 208-211)
- Modify: `src/call_manager/web/templates/login_sent.html` (neutral message)
- Test: `tests/test_web_auth.py` (extend)

- [ ] **Step 1: Write failing tests**

```python
# add to tests/test_web_auth.py
"""Self-serve login creates a PENDING user and sends NO link; only active
users get a magic link; verify refuses non-active. Spec §4.1/§4.2."""
from sqlalchemy import select
from call_manager.db.models import User, MagicLinkToken


def test_new_email_creates_pending_no_token(client, pg_session_factory):
    r = client.post("/login", data={"email": "newperson@ki.se"},
                    headers={"origin": "http://testserver"})
    assert r.status_code == 200  # neutral page
    with pg_session_factory() as s:
        u = s.scalar(select(User).where(User.email == "newperson@ki.se"))
        assert u is not None and u.status == "pending"
        assert s.scalar(select(MagicLinkToken).where(MagicLinkToken.user_id == u.id)) is None


def test_active_user_gets_token(client, make_user, pg_session_factory):
    make_user(... email="active@ki.se", status="active" ...)
    r = client.post("/login", data={"email": "active@ki.se"},
                    headers={"origin": "http://testserver"})
    assert r.status_code == 200
    with pg_session_factory() as s:
        u = s.scalar(select(User).where(User.email == "active@ki.se"))
        assert s.scalar(select(MagicLinkToken).where(MagicLinkToken.user_id == u.id)) is not None
```
> Use this file's existing `client` + DB fixtures and origin header (CSRF). Build `make_user` per Task 2's factory.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_web_auth.py -k "pending or active_user_gets_token" -v`
Expected: FAIL — today every new email is created active and issued a token.

- [ ] **Step 3: Rewrite the `login_submit` user block**

In `src/call_manager/web/routes/auth.py`, replace lines 142-152:
```python
    with session_factory() as session:
        user = session.execute(
            select(User).where(User.email == email_normalized)
        ).scalar_one_or_none()
        if user is None:
            user = User(email=email_normalized)
            session.add(user)
            session.flush()

        raw = issue_token(session, user_id=user.id, ttl_minutes=15)
        session.commit()
```
with:
```python
    with session_factory() as session:
        user = session.execute(
            select(User).where(User.email == email_normalized)
        ).scalar_one_or_none()
        if user is None:
            # Self-serve request: create a PENDING account for admin approval.
            # No token, no email — the user signs in only once approved. The
            # response below is the SAME neutral page on every branch, so this
            # does not leak account state (enumeration-resistance preserved).
            session.add(User(email=email_normalized, status="pending", role="staff"))
            session.commit()
            _logger.info(
                "login: new self-serve request -> pending",
                extra={"event": "login_pending_created", "email_fp": email_fp},
            )
            return response
        if user.status != "active":
            # pending (awaiting approval) or suspended (offboarded): no link.
            _logger.info(
                "login: non-active account, no link",
                extra={"event": "login_non_active", "email_fp": email_fp},
            )
            return response
        raw = issue_token(session, user_id=user.id, ttl_minutes=15)
        session.commit()
```

- [ ] **Step 4: Refuse non-active users at verify**

In `login_verify`, change lines 208-211:
```python
    with session_factory() as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=400, detail="invalid link")
```
to:
```python
    with session_factory() as session:
        user = session.get(User, user_id)
        if user is None or user.status != "active":
            # Belt-and-suspenders: tokens are only issued to active users, but a
            # suspend between issue and redeem must still be refused.
            raise HTTPException(status_code=400, detail="invalid link")
```

- [ ] **Step 5: Reword `login_sent.html` to cover both outcomes neutrally**

Replace the body message so it is truthful for active AND pending requesters without revealing which (keep the existing template structure / classes):
```html
  <p>Thanks. If your address is eligible, you'll receive a sign-in link shortly.
     New accounts require administrator approval before the first sign-in —
     you'll be notified once approved.</p>
```

- [ ] **Step 6: Run the tests + the auth suite**

Run: `uv run pytest tests/test_web_auth.py -v`
Expected: PASS. Existing tests that assumed first-login auto-activates must be updated to create the user `active` first (that's the new contract).

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/web/routes/auth.py src/call_manager/web/templates/login_sent.html tests/test_web_auth.py
git commit -m "feat(auth): self-serve login creates pending account; active-only sign-in"
```

---

## Task 5: User-management service

All `role`/`status` writes live here, serialized on the user row, with guardrails. The only writer of `role`/`status` outside the migration/bootstrap.

**Files:**
- Create: `src/call_manager/users/service.py` (+ `src/call_manager/users/__init__.py`)
- Test: `tests/users/test_service.py`

- [ ] **Step 1: Write failing tests (guardrails + transitions)**

```python
# tests/users/test_service.py
"""User-state service: create/approve/deny/set_role/deactivate/reactivate with
guardrails. Spec §4.1/§4.3/§4.4."""
import pytest
from call_manager.users import service
from call_manager.users.service import GuardrailError


def test_create_account_active(pg_session_factory):
    with pg_session_factory() as s:
        u = service.create_account(s, email="new@ki.se", role="staff", actor_id=_admin(s).id)
        s.commit()
        assert u.status == "active" and u.role == "staff"


def test_create_over_pending_approves_in_place(pg_session_factory):
    with pg_session_factory() as s:
        admin = _admin(s)
        s.add(_pending(s, "p@ki.se")); s.commit()
        u = service.create_account(s, email="p@ki.se", role="staff", actor_id=admin.id)
        s.commit()
        assert u.status == "active"


def test_cannot_demote_self(pg_session_factory):
    with pg_session_factory() as s:
        admin = _admin(s)
        with pytest.raises(GuardrailError):
            service.set_role(s, target_id=admin.id, new_role="staff", actor_id=admin.id)


def test_cannot_remove_last_admin(pg_session_factory):
    with pg_session_factory() as s:
        a1 = _admin(s)
        a2 = service.create_account(s, email="a2@ki.se", role="staff", actor_id=a1.id)
        s.commit()
        # a1 is the only admin; demoting a1 by a2-as-admin... make a2 admin first
        service.set_role(s, target_id=a2.id, new_role="admin", actor_id=a1.id); s.commit()
        service.set_role(s, target_id=a1.id, new_role="staff", actor_id=a2.id); s.commit()
        # now a2 is the last admin; a2 cannot be demoted (and not by self)
        with pytest.raises(GuardrailError):
            service.set_role(s, target_id=a2.id, new_role="staff", actor_id=a2.id)


def test_deactivate_bumps_epoch(pg_session_factory):
    with pg_session_factory() as s:
        admin = _admin(s)
        u = service.create_account(s, email="u@ki.se", role="staff", actor_id=admin.id); s.commit()
        before = u.session_epoch
        service.deactivate(s, target_id=u.id, actor_id=admin.id); s.commit()
        assert u.status == "suspended" and u.session_epoch == before + 1


def test_cannot_demote_below_staff_while_owning(pg_session_factory, make_workspace):
    with pg_session_factory() as s:
        admin = _admin(s)
        owner = service.create_account(s, email="own@ki.se", role="staff", actor_id=admin.id); s.commit()
        make_workspace(s, owner=owner)  # live workspace
        with pytest.raises(GuardrailError):
            service.set_role(s, target_id=owner.id, new_role="read_only", actor_id=admin.id)


# helpers _admin/_pending build rows directly; reuse make_user/make_workspace.
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/users/test_service.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the service**

```python
# src/call_manager/users/__init__.py
```
```python
# src/call_manager/users/service.py
"""All user role/status transitions, serialized on the user row, with the
guardrails from spec §4.3/§4.4. This module is the ONLY writer of
users.role / users.status outside the migration and admin bootstrap."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from call_manager.db.models import User, Workspace


class GuardrailError(Exception):
    """A transition refused for a safety reason (self-demote, last-admin,
    demote-below-staff-while-owning). Mapped to 4xx by the web layer."""


def _lock(session: Session, user_id: int) -> User:
    user = session.execute(
        select(User).where(User.id == user_id).with_for_update()
    ).scalar_one_or_none()
    if user is None:
        raise GuardrailError("user not found")
    return user


def _active_admin_count(session: Session, *, excluding: int | None = None) -> int:
    q = select(func.count()).select_from(User).where(
        User.role == "admin", User.status == "active"
    )
    if excluding is not None:
        q = q.where(User.id != excluding)
    return session.scalar(q) or 0


def _owns_live_workspace(session: Session, user_id: int) -> bool:
    return session.scalar(
        select(Workspace.id).where(
            Workspace.owner_id == user_id, Workspace.soft_deleted_at.is_(None)
        ).limit(1)
    ) is not None


def create_account(session: Session, *, email: str, role: str, actor_id: int) -> User:
    """Admin account-only create. Idempotent over an existing row:
    pending -> approve-in-place; suspended -> caller must pass reactivate=True
    (kept simple here: reactivate via reactivate())."""
    email = email.strip().lower()
    existing = session.scalar(select(User).where(User.email == email))
    if existing is None:
        user = User(email=email, role=role, status="active")
        user.approved_by = actor_id
        user.approved_at = datetime.now(UTC)
        session.add(user)
        session.flush()
        return user
    user = _lock(session, existing.id)
    if user.status == "pending":
        user.status = "active"
        user.role = role
        user.approved_by = actor_id
        user.approved_at = datetime.now(UTC)
        return user
    # active or suspended: do not silently overwrite — caller decides via
    # approve()/reactivate(); return as-is.
    return user


def approve(session: Session, *, target_id: int, role: str, actor_id: int) -> User:
    user = _lock(session, target_id)
    if user.status != "pending":
        return user  # idempotent: already resolved
    user.status = "active"
    user.role = role
    user.approved_by = actor_id
    user.approved_at = datetime.now(UTC)
    return user


def deny(session: Session, *, target_id: int, actor_id: int) -> User:
    user = _lock(session, target_id)
    if user.status == "pending":
        user.status = "suspended"
    return user


def set_role(session: Session, *, target_id: int, new_role: str, actor_id: int) -> User:
    if new_role not in ("admin", "staff", "read_only"):
        raise GuardrailError("invalid role")
    user = _lock(session, target_id)
    if target_id == actor_id and new_role != "admin":
        raise GuardrailError("cannot demote yourself")
    if user.role == "admin" and new_role != "admin":
        if _active_admin_count(session, excluding=target_id) < 1:
            raise GuardrailError("cannot remove the last admin")
    if new_role == "read_only" and _owns_live_workspace(session, target_id):
        raise GuardrailError("transfer owned workspaces before demoting to read_only")
    user.role = new_role
    if new_role == "read_only":
        # Spec §3.4: a read_only user cannot be a workspace editor — downgrade
        # any of their editor rows to viewer in the same transaction.
        from call_manager.db.models import WorkspaceViewer

        for wv in session.scalars(
            select(WorkspaceViewer).where(
                WorkspaceViewer.user_id == target_id, WorkspaceViewer.role == "editor"
            )
        ):
            wv.role = "viewer"
    return user


def deactivate(session: Session, *, target_id: int, actor_id: int) -> User:
    user = _lock(session, target_id)
    if target_id == actor_id:
        raise GuardrailError("cannot suspend yourself")
    if user.role == "admin" and _active_admin_count(session, excluding=target_id) < 1:
        raise GuardrailError("cannot suspend the last admin")
    user.status = "suspended"
    user.session_epoch += 1  # kill live sessions
    return user


def reactivate(session: Session, *, target_id: int, actor_id: int) -> User:
    user = _lock(session, target_id)
    if user.status == "suspended":
        user.status = "active"
    return user


def revoke_sessions(session: Session, *, target_id: int, actor_id: int) -> User:
    user = _lock(session, target_id)
    user.session_epoch += 1
    return user
```

- [ ] **Step 4: Run the service tests**

Run: `uv run pytest tests/users/test_service.py -v`
Expected: PASS. (Add `tests/users/__init__.py` if the test dir needs it; match how other test subdirs are set up.)

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/users/ tests/users/
git commit -m "feat(users): serialized user-state service with last-admin/self/owner guards"
```

---

## Task 6: Admin Users panel

**Files:**
- Modify: `src/call_manager/web/routes/admin.py` (add `/admin/users/*` behind `require_admin`)
- Create: `src/call_manager/web/templates/admin_users.html`
- Modify: `src/call_manager/web/templates/admin.html` (nav link in the subnav)
- Test: `tests/test_web_admin_users.py`

- [ ] **Step 1: Write failing route tests**

```python
# tests/test_web_admin_users.py
"""Admin Users panel: list, create, approve, deny, set-role, deactivate.
All behind require_admin; guardrails surface as 4xx. Spec §6."""
def test_non_admin_cannot_list_users(client_authed_staff):
    assert client_authed_staff.get("/admin/users").status_code == 403


def test_admin_lists_users(client_authed_admin):
    r = client_authed_admin.get("/admin/users")
    assert r.status_code == 200


def test_admin_approves_pending(client_authed_admin, make_user, pg_session_factory):
    u = make_user(... email="p@ki.se", status="pending", role="staff" ...)
    r = client_authed_admin.post(f"/admin/users/{u.id}/approve",
                                 data={"role": "staff"}, headers={"origin": "http://testserver"})
    assert r.status_code in (200, 303)
    with pg_session_factory() as s:
        assert s.get(type(u), u.id).status == "active"


def test_self_demote_blocked(client_authed_admin, admin_user):
    r = client_authed_admin.post(f"/admin/users/{admin_user.id}/role",
                                 data={"role": "staff"}, headers={"origin": "http://testserver"})
    assert r.status_code in (403, 409)
```
> Reuse the admin client fixture pattern from `tests/test_web_admin.py`. Add a `client_authed_staff`/`client_authed_admin` if not present (a client whose user is `role='staff'`/`'admin'`).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_web_admin_users.py -v`
Expected: FAIL — routes 404.

- [ ] **Step 3: Add the routes**

In `src/call_manager/web/routes/admin.py`, add (mirroring the existing `require_admin` handlers; `_session_dep` per this module's pattern, or use `request.app.state.session_factory`):
```python
from fastapi import Form
from sqlalchemy import select
from call_manager.db.models import User, Workspace
from call_manager.users import service
from call_manager.users.service import GuardrailError


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, admin=Depends(require_admin)):  # noqa: B008
    with request.app.state.session_factory() as session:
        rows = session.scalars(select(User).order_by(User.status, User.email)).all()
        owned = {
            uid: cnt
            for uid, cnt in session.execute(
                select(Workspace.owner_id, func.count())
                .where(Workspace.soft_deleted_at.is_(None))
                .group_by(Workspace.owner_id)
            ).all()
        }
        users = [{"u": u, "owned": owned.get(u.id, 0)} for u in rows]
        session.expunge_all()
    return request.app.state.templates.TemplateResponse(
        request, "admin_users.html",
        {"users": users, "user": admin, "pending": [x for x in users if x["u"].status == "pending"]},
    )


def _user_action(request, admin, target_id, fn, **kw):
    try:
        with request.app.state.session_factory() as session:
            fn(session, target_id=target_id, actor_id=admin.id, **kw)
            session.commit()
    except GuardrailError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/admin/users")
def admin_users_create(request: Request, admin=Depends(require_admin),  # noqa: B008
                       email: str = Form(...), role: str = Form("staff")):
    try:
        with request.app.state.session_factory() as session:
            service.create_account(session, email=email, role=role, actor_id=admin.id)
            session.commit()
    except GuardrailError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/admin/users/{target_id}/approve")
def admin_users_approve(request: Request, target_id: int, admin=Depends(require_admin),  # noqa: B008
                        role: str = Form("staff")):
    return _user_action(request, admin, target_id, service.approve, role=role)


@router.post("/admin/users/{target_id}/deny")
def admin_users_deny(request: Request, target_id: int, admin=Depends(require_admin)):  # noqa: B008
    return _user_action(request, admin, target_id, service.deny)


@router.post("/admin/users/{target_id}/role")
def admin_users_role(request: Request, target_id: int, admin=Depends(require_admin),  # noqa: B008
                     role: str = Form(...)):
    return _user_action(request, admin, target_id, service.set_role, new_role=role)


@router.post("/admin/users/{target_id}/deactivate")
def admin_users_deactivate(request: Request, target_id: int, admin=Depends(require_admin)):  # noqa: B008
    return _user_action(request, admin, target_id, service.deactivate)


@router.post("/admin/users/{target_id}/reactivate")
def admin_users_reactivate(request: Request, target_id: int, admin=Depends(require_admin)):  # noqa: B008
    return _user_action(request, admin, target_id, service.reactivate)


@router.post("/admin/users/{target_id}/revoke-sessions")
def admin_users_revoke(request: Request, target_id: int, admin=Depends(require_admin)):  # noqa: B008
    return _user_action(request, admin, target_id, service.revoke_sessions)
```
Confirm `require_admin`, `Request`, `Depends`, `HTMLResponse`, `RedirectResponse`, `HTTPException`, `func` are imported in `admin.py` (add any missing).

- [ ] **Step 4: Create `admin_users.html`**

```html
{% extends "base.html" %}
{% block title %}Users — Call Manager{% endblock %}
{% block content %}
<hgroup>
  <h1>Users</h1>
  <p>Approve requests, create accounts, set roles, and offboard users.</p>
</hgroup>
<nav class="subnav" aria-label="Admin sections">
  <a href="/admin">Operational health</a>
  <a href="/admin/drafting">Drafting (B7/B8) panel</a>
  <span class="current">Users</span>
</nav>

{% if pending %}
<article>
  <header><strong>Pending requests ({{ pending|length }})</strong></header>
  {% for row in pending %}
    <div class="reference-row">
      <strong>{{ row.u.email }}</strong>
      <form method="post" action="/admin/users/{{ row.u.id }}/approve" class="inline-form">
        <select name="role"><option value="staff">staff</option><option value="read_only">read_only</option><option value="admin">admin</option></select>
        <button type="submit" class="compact">Approve</button>
      </form>
      <form method="post" action="/admin/users/{{ row.u.id }}/deny" class="inline-form"
            data-confirm="Deny {{ row.u.email }}? They will be blocked from re-requesting.">
        <button type="submit" class="secondary outline compact">Deny</button>
      </form>
    </div>
  {% endfor %}
</article>
{% endif %}

<article>
  <header><strong>Create account</strong></header>
  <form method="post" action="/admin/users" class="reference-fetch">
    <label>Email <input type="email" name="email" required></label>
    <select name="role"><option value="staff">staff</option><option value="read_only">read_only</option><option value="admin">admin</option></select>
    <button type="submit">Create (active)</button>
  </form>
</article>

<article>
  <header><strong>All users</strong></header>
  <table>
    <thead><tr><th>Email</th><th>Role</th><th>Status</th><th>Owned</th><th>Last login</th><th>Actions</th></tr></thead>
    <tbody>
      {% for row in users %}
      <tr>
        <td>{{ row.u.email }}{% if row.u.id == user.id %} <small class="muted">(you)</small>{% endif %}</td>
        <td>
          <form method="post" action="/admin/users/{{ row.u.id }}/role" class="inline-form">
            <select name="role" data-autosubmit>
              {% for r in ['admin','staff','read_only'] %}
                <option value="{{ r }}" {% if row.u.role == r %}selected{% endif %}>{{ r }}</option>
              {% endfor %}
            </select>
          </form>
        </td>
        <td><span class="status status-{{ 'ok' if row.u.status=='active' else ('pending' if row.u.status=='pending' else 'failed') }}">{{ row.u.status }}</span></td>
        <td>{{ row.owned }}</td>
        <td>{{ row.u.last_login_at | display_dt if row.u.last_login_at else '—' }}</td>
        <td>
          {% if row.u.status == 'active' %}
          <form method="post" action="/admin/users/{{ row.u.id }}/deactivate" class="inline-form"
                data-confirm="Deactivate {{ row.u.email }}? Their sessions end immediately.">
            <button type="submit" class="secondary outline compact">Deactivate</button>
          </form>
          {% elif row.u.status == 'suspended' %}
          <form method="post" action="/admin/users/{{ row.u.id }}/reactivate" class="inline-form"
                data-confirm="Reactivate {{ row.u.email }}?">
            <button type="submit" class="compact">Reactivate</button>
          </form>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</article>
{% endblock %}
```

- [ ] **Step 5: Add the nav link in `admin.html`**

In the existing subnav block of `admin.html`, add:
```html
  <a href="/admin/users">Users</a>
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_web_admin_users.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/web/routes/admin.py src/call_manager/web/templates/admin_users.html src/call_manager/web/templates/admin.html tests/test_web_admin_users.py
git commit -m "feat(users): admin Users panel (approve/deny/create/role/deactivate)"
```

---

## Task 7: Centralized workspace guards

**Files:**
- Create: `src/call_manager/proposal_assist/web/_guards.py`
- Test: `tests/proposal_assist/test_guards.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/proposal_assist/test_guards.py
"""Centralized workspace guards: member/editor/owner tiers, read_only never
writes. Spec §5."""
import pytest
from fastapi import HTTPException
from call_manager.proposal_assist.web import _guards


def test_viewer_blocked_from_editor(pg_session_factory, make_user, make_workspace):
    with pg_session_factory() as s:
        owner = make_user(s, email="o@ki.se", role="staff")
        viewer = make_user(s, email="v@ki.se", role="staff")
        ws = make_workspace(s, owner=owner)
        _add_viewer(s, ws, viewer, role="viewer"); s.commit()
        _guards._require_member(workspace_id=ws.id, user=viewer, session=s)  # ok
        with pytest.raises(HTTPException):
            _guards._require_editor(workspace_id=ws.id, user=viewer, session=s)


def test_editor_allowed_owner_only_blocked(pg_session_factory, make_user, make_workspace):
    with pg_session_factory() as s:
        owner = make_user(s, email="o2@ki.se", role="staff")
        editor = make_user(s, email="e@ki.se", role="staff")
        ws = make_workspace(s, owner=owner)
        _add_viewer(s, ws, editor, role="editor"); s.commit()
        _guards._require_editor(workspace_id=ws.id, user=editor, session=s)  # ok
        with pytest.raises(HTTPException):
            _guards._require_owner(workspace_id=ws.id, user=editor, session=s)


def test_read_only_editor_row_still_denied_write(pg_session_factory, make_user, make_workspace):
    with pg_session_factory() as s:
        owner = make_user(s, email="o3@ki.se", role="staff")
        ro = make_user(s, email="ro@ki.se", role="read_only")
        ws = make_workspace(s, owner=owner)
        _add_viewer(s, ws, ro, role="editor"); s.commit()  # stale editor row
        with pytest.raises(HTTPException):
            _guards._require_editor(workspace_id=ws.id, user=ro, session=s)
```
> `_add_viewer` inserts a `WorkspaceViewer(workspace_id, user_id, role)`. Reuse `make_user`/`make_workspace`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/proposal_assist/test_guards.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `_guards.py`**

```python
# src/call_manager/proposal_assist/web/_guards.py
"""Centralized workspace authorization. Replaces the five duplicated
_require_owner / _require_owner_or_viewer helpers. Three tiers keyed off the
effective workspace role (owner > editor > viewer). Spec §5."""
from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import Workspace, WorkspaceViewer

_RANK = {"viewer": 1, "editor": 2, "owner": 3}


def _load_workspace_or_404(workspace_id: uuid.UUID, session: Session) -> Workspace:
    ws = session.get(Workspace, workspace_id)
    if ws is None or ws.soft_deleted_at is not None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return ws


def _effective_role(ws: Workspace, user, session: Session) -> str | None:
    if ws.owner_id == user.id:
        return "owner"
    return session.scalar(
        select(WorkspaceViewer.role).where(
            WorkspaceViewer.workspace_id == ws.id,
            WorkspaceViewer.user_id == user.id,
        )
    )  # 'editor' | 'viewer' | None


def _require_member(*, workspace_id: uuid.UUID, user, session: Session) -> Workspace:
    ws = _load_workspace_or_404(workspace_id, session)
    if _effective_role(ws, user, session) is None:
        raise HTTPException(status_code=403, detail="you do not have access to this workspace")
    return ws


def _require_editor(*, workspace_id: uuid.UUID, user, session: Session) -> Workspace:
    ws = _load_workspace_or_404(workspace_id, session)
    role = _effective_role(ws, user, session)
    if role is None or _RANK[role] < _RANK["editor"]:
        raise HTTPException(status_code=403, detail="editor access required")
    # System-role gate: a user demoted to read_only loses write even if a stale
    # editor row survives.
    if getattr(user, "role", None) == "read_only":
        raise HTTPException(status_code=403, detail="editor access required")
    return ws


def _require_owner(*, workspace_id: uuid.UUID, user, session: Session) -> Workspace:
    ws = _load_workspace_or_404(workspace_id, session)
    if ws.owner_id != user.id:
        raise HTTPException(
            status_code=403, detail="only the workspace owner may perform this action"
        )
    return ws


def _require_owner_or_admin(*, workspace_id: uuid.UUID, user, session: Session) -> Workspace:
    ws = _load_workspace_or_404(workspace_id, session)
    if ws.owner_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="owner or admin required")
    return ws
```

- [ ] **Step 4: Run the guard tests**

Run: `uv run pytest tests/proposal_assist/test_guards.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/web/_guards.py tests/proposal_assist/test_guards.py
git commit -m "feat(workspaces): centralized member/editor/owner guard module"
```

---

## Task 8: Remap every workspace route to the centralized guards

Replace the duplicated helpers in all 7 routers with imports from `_guards`, then flip each call site per the map. Delete the local `_require_owner` / `_require_owner_or_viewer` / `_load_workspace_or_404` definitions in `workspaces.py` (365-403), `files.py` (117-150), `references.py` (74-101), `rubric_review.py` (26-43); fix `citations.py`/`sections.py`/`annotations.py` imports to point at `_guards`.

**Files:** `workspaces.py`, `files.py`, `references.py`, `citations.py`, `sections.py`, `annotations.py`, `rubric_review.py` (all under `src/call_manager/proposal_assist/web/`).

- [ ] **Step 1: Swap definitions for imports**

In each router that defines the helpers locally, delete the local defs and add:
```python
from call_manager.proposal_assist.web._guards import (
    _require_member,
    _require_editor,
    _require_owner,
    _require_owner_or_admin,
    _load_workspace_or_404,
)
```
In `citations.py`/`sections.py`/`annotations.py`, change the existing `from ... import _require_owner_or_viewer` lines to import `_require_member` (and `_require_editor`/`_require_owner` as needed) from `_guards`.

- [ ] **Step 2: Flip each call site per this map**

`_require_owner_or_viewer(...)` → `_require_member(...)` everywhere (read routes). `_require_owner(...)` on **write** routes → `_require_editor(...)`. Keep `_require_owner(...)` on manage routes. Exact sites:

| File:line | Handler | New guard |
|---|---|---|
| workspaces.py:693 | post_turn | `_require_editor` |
| workspaces.py (post_disclosure) :637 | post_disclosure | `_require_member` |
| workspaces.py (stream_turn) :954 | stream_turn | `_require_member` |
| workspaces.py artifact raw/history/at/diff | those GETs | `_require_member` |
| workspaces.py:1076 get_settings / :1161 post_settings | settings | `_require_owner` (keep) |
| workspaces.py:1196 soft_delete | soft_delete | `_require_owner_or_admin` |
| workspaces.py:1298 cancel_turn | cancel_turn | `_require_editor` |
| files.py:185 list_files / serve_file | reads | `_require_member` |
| files.py:275 upload_file / :454 delete_file | writes | `_require_editor` |
| references.py:147 panel / serve raw | reads | `_require_member` |
| references.py:180 upload / :268 fetch / :414 + :454 metadata / :541 waiver | writes | `_require_editor` |
| citations.py:47 panel / :104 validate / export | reads | `_require_member` |
| sections.py:121 view | read | `_require_member` |
| sections.py:140 edit / :178 save | writes | `_require_editor` |
| annotations.py:188 GET | read | `_require_member` |
| annotations.py:230 insert / :291 dismiss / :344 reanalyze | writes | `_require_editor` |
| rubric_review.py:54 GET / :112 POST | rubric confirm | `_require_owner` (keep — Phase-0 owner action) |

> The call signature is unchanged (`workspace_id=`, `user=`, `session=`), so each flip is a one-line rename. Grep to confirm none missed: `grep -rn "_require_owner\b\|_require_owner_or_viewer" src/call_manager/proposal_assist/web/`.

- [ ] **Step 3: Run the proposal-assist suite**

Run: `uv run pytest tests/proposal_assist/ -q`
Expected: many existing tests assume owner-only writes; they should still pass (owner is `>= editor`). Tests asserting a **viewer** gets 403 on writes still pass. Any test that set up a "viewer" expecting them to write was already wrong (viewers couldn't write before) — none should exist.

- [ ] **Step 4: Add the editor/viewer matrix test**

```python
# tests/proposal_assist/test_route_authz_matrix.py
"""A viewer gets 403 on every editor route; an editor gets 403 on owner-only
routes. Spec §5.1."""
import pytest

EDITOR_WRITE_ROUTES = [
    ("post", "/workspaces/{id}/turns", {"content": "x"}),
    ("post", "/workspaces/{id}/files", None),
    # ... one representative per router (sections save, references upload,
    #     annotations insert) — fill from the route map above.
]
OWNER_ONLY_ROUTES = [
    ("post", "/workspaces/{id}/settings", {"title": "x"}),
    ("post", "/workspaces/{id}/viewers", {"email": "z@ki.se", "role": "viewer"}),
    ("post", "/workspaces/{id}/delete", None),
]


def test_viewer_denied_editor_routes(client_for_viewer, viewer_workspace):
    for method, path, data in EDITOR_WRITE_ROUTES:
        r = getattr(client_for_viewer, method)(
            path.format(id=viewer_workspace.id), data=data or {},
            headers={"origin": "http://testserver"},
        )
        assert r.status_code == 403, (path, r.status_code)


def test_editor_denied_owner_routes(client_for_editor, editor_workspace):
    for method, path, data in OWNER_ONLY_ROUTES:
        r = getattr(client_for_editor, method)(
            path.format(id=editor_workspace.id), data=data or {},
            headers={"origin": "http://testserver"},
        )
        assert r.status_code == 403, (path, r.status_code)
```
> Build `client_for_viewer`/`client_for_editor` fixtures: a workspace owned by user A, with user B added as a `viewer`/`editor` row, and a TestClient authed as B. Disclosure-gate (P1) returns 403 for the turn route too — that's still a 403, so the assertion holds; if you want to isolate the authz 403, ack disclosure for the editor first.

- [ ] **Step 5: Run + commit**

Run: `uv run pytest tests/proposal_assist/test_route_authz_matrix.py -v && uv run pytest tests/proposal_assist/ -q`
```bash
git add -A
git commit -m "feat(workspaces): remap all routes to member/editor/owner guards + matrix tests"
```

---

## Task 9: Collaborator roles + owner transfer

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` (`add_viewer` 1103-1134; add `transfer_owner`)
- Modify: `src/call_manager/web/templates/workspace_settings.html`
- Test: `tests/proposal_assist/test_web_collaborators.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/proposal_assist/test_web_collaborators.py
"""Owner adds collaborator with a role; read_only can only be viewer;
owner transfer reassigns ownership. Spec §3.3/§4.4/§7."""
def test_add_editor_collaborator(client_authed_owner, workspace, make_user, pg_session_factory):
    target = make_user(... email="ed@ki.se", role="staff" ...)
    r = client_authed_owner.post(f"/workspaces/{workspace.id}/viewers",
        data={"email": "ed@ki.se", "role": "editor"}, headers={"origin": "http://testserver"})
    assert r.status_code in (200, 303)
    # assert a WorkspaceViewer row with role='editor' exists


def test_read_only_cannot_be_editor(client_authed_owner, workspace, make_user):
    make_user(... email="ro@ki.se", role="read_only" ...)
    r = client_authed_owner.post(f"/workspaces/{workspace.id}/viewers",
        data={"email": "ro@ki.se", "role": "editor"}, headers={"origin": "http://testserver"})
    assert r.status_code in (400, 403)


def test_transfer_owner(client_authed_owner, workspace, make_user, pg_session_factory):
    target = make_user(... email="new@ki.se", role="staff" ...)
    # target must be a member first
    client_authed_owner.post(f"/workspaces/{workspace.id}/viewers",
        data={"email": "new@ki.se", "role": "editor"}, headers={"origin": "http://testserver"})
    r = client_authed_owner.post(f"/workspaces/{workspace.id}/transfer-owner",
        data={"user_id": target.id}, headers={"origin": "http://testserver"})
    assert r.status_code in (200, 303)
    # assert workspace.owner_id == target.id
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/proposal_assist/test_web_collaborators.py -v`
Expected: FAIL (no role param / no transfer route).

- [ ] **Step 3: Add `role` to `add_viewer`**

In `add_viewer` (workspaces.py:1103), add a `role: str = Form("viewer")` parameter and, after resolving `target`, before inserting the row:
```python
    if role not in ("editor", "viewer"):
        raise HTTPException(status_code=400, detail="role must be editor or viewer")
    if role == "editor" and target.role == "read_only":
        raise HTTPException(
            status_code=400, detail="read-only users can only be added as viewers"
        )
```
and set `role=role` on the `WorkspaceViewer(...)` insert. (Keep the existing enumeration-resistant generic error when `target` is None.)

- [ ] **Step 4: Add the `transfer_owner` route**

```python
@router.post("/{workspace_id}/transfer-owner")
def transfer_owner(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
    user_id: int = Form(...),
) -> RedirectResponse:
    """Reassign workspace ownership to another active staff/admin user who is
    already a member. Owner or admin may invoke. Spec §4.4."""
    ws = _require_owner_or_admin(workspace_id=workspace_id, user=user, session=session)
    target = session.get(User, user_id)
    if target is None or target.status != "active" or target.role not in ("staff", "admin"):
        raise HTTPException(status_code=400, detail="invalid transfer target")
    old_owner_id = ws.owner_id
    ws.owner_id = target.id
    # Remove the new owner's viewer/editor row (now redundant); demote the old
    # owner to an editor row so they keep collaborating unless removed.
    session.query(WorkspaceViewer).filter_by(workspace_id=ws.id, user_id=target.id).delete()
    if old_owner_id != target.id:
        existing = session.query(WorkspaceViewer).filter_by(
            workspace_id=ws.id, user_id=old_owner_id
        ).first()
        if existing is None:
            session.add(WorkspaceViewer(workspace_id=ws.id, user_id=old_owner_id, role="editor"))
    session.commit()
    return RedirectResponse(url=f"/workspaces/{ws.id}/settings", status_code=303)
```

- [ ] **Step 5: Update `workspace_settings.html`**

Add a role `<select>` to the add-collaborator form (`name="role"`, options editor/viewer), show each collaborator's role in the viewers list, and add a transfer-owner form (a `<select name="user_id">` of current members + submit). Use the existing form styling classes.

- [ ] **Step 6: Run + commit**

Run: `uv run pytest tests/proposal_assist/test_web_collaborators.py -v`
```bash
git add src/call_manager/proposal_assist/web/workspaces.py src/call_manager/web/templates/workspace_settings.html tests/proposal_assist/test_web_collaborators.py
git commit -m "feat(workspaces): editor/viewer collaborator roles + owner transfer"
```

---

## Task 10: Template flag split (`is_owner` → `can_edit`/`can_manage`, `can_create`)

`is_owner` is overloaded for "can write" and "can manage". Split it so editors get the write UI but only owners get manage UI; gate create-buttons on system role.

**Files:** the handlers that pass `is_owner` and ~10 templates.

- [ ] **Step 1: Compute the flags in each handler**

Wherever a handler currently computes `is_owner = ws.owner_id == user.id` and passes it (get_workspace_main:604/613, list_files, references_panel, citations_panel, section view/edit/save, annotation strip — and the hardcoded `is_owner=True` sites), additionally compute and pass:
```python
    role = _effective_role(ws, user, session)  # from _guards
    can_edit = role in ("owner", "editor") and user.role != "read_only"
    can_manage = role == "owner"
    can_create = user.role != "read_only"
```
Pass `can_edit`, `can_manage` into the workspace templates (keep `is_owner` too during transition if simpler, but new templates use the split). For the hardcoded `is_owner=True` render sites (e.g. the HTMX fragment responses after an editor action), set `can_edit=True` (the actor passed the editor guard to get there).

- [ ] **Step 2: Update templates (exact reads)**

| Template:line | current `is_owner` use | replace with |
|---|---|---|
| workspace_main.html:34 | composer | `can_edit` |
| workspace_main.html:20 (Settings link) | manage | `can_manage` |
| workspace_files.html:18 (upload) | `can_edit` |
| workspace_files.html:14 (Settings) | `can_manage` |
| file_row.html:11 (delete) | `can_edit` |
| workspace_references.html:18 (upload) | `can_edit` |
| workspace_references.html:14 (Settings) | `can_manage` |
| reference_row.html:24/29 (metadata/waiver) | `can_edit` |
| workspace_citations.html:14 (Settings) | `can_manage` |
| section_view.html:10 (edit btn) | `can_edit` |
| annotation_strip.html:13/23/29 (actions) | `can_edit` |

Create-button gates (wrap in `{% if can_create %}`): profiles_index.html:10/18, workspaces_index.html:10/50, call_detail.html:19. Pass `can_create = user.role != "read_only"` from `calls_index`, `profiles_index`, `workspaces_index`, `call_detail` handlers.

- [ ] **Step 3: Add `require_staff` to create routes**

Change the dependency on these four routes from `get_current_user` to `require_staff` (import it): `GET /profiles/new`, `POST /profiles/new` ([profiles.py:55,67](../../../src/call_manager/web/routes/profiles.py#L55)); `GET /workspaces/new`, `POST /workspaces/new` ([workspaces.py:406,424](../../../src/call_manager/proposal_assist/web/workspaces.py#L406)). In `profile_edit_submit` ([profiles.py:258](../../../src/call_manager/web/routes/profiles.py#L258)), add a `read_only` block alongside the existing owner-or-admin check.

- [ ] **Step 4: Tests**

```python
# tests/test_read_only_capabilities.py
def test_read_only_cannot_create_profile(client_authed_read_only):
    assert client_authed_read_only.get("/profiles/new").status_code == 403
    assert client_authed_read_only.post("/profiles/new", data={"name": "x", "kind": "organizational_unit"},
        headers={"origin": "http://testserver"}).status_code == 403

def test_read_only_cannot_create_workspace(client_authed_read_only):
    assert client_authed_read_only.post("/workspaces/new", data={"title": "x"},
        headers={"origin": "http://testserver"}).status_code == 403
```

- [ ] **Step 5: Run + commit**

Run: `uv run pytest tests/test_read_only_capabilities.py -v && uv run pytest tests/proposal_assist -q`
```bash
git add -A
git commit -m "feat(users): split can_edit/can_manage/can_create + require_staff on create routes"
```

---

## Task 11: Digest excludes suspended users

**Files:**
- Modify: `src/call_manager/digest/send.py` (candidate query ~line 72)
- Test: `tests/test_digest_*` (extend the candidate-selection test)

- [ ] **Step 1: Write failing test**

```python
# add to the existing digest candidate test
def test_suspended_user_excluded_from_digest(pg_session_factory, make_user):
    # a suspended user with an active subscription must NOT be a candidate
    ...  # assert their id is not in candidate_user_ids
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ -k "digest and candidate" -v`
Expected: FAIL (suspended user still selected).

- [ ] **Step 3: Add the status filter**

In `src/call_manager/digest/send.py`, add to the candidate query (the `.where(User.digest_disabled_at.is_(None))` at line ~72):
```python
                .where(User.digest_disabled_at.is_(None))
                .where(User.status == "active")
```

- [ ] **Step 4: Run + commit**

Run: `uv run pytest tests/ -k digest -q`
```bash
git add src/call_manager/digest/send.py tests/
git commit -m "fix(digest): exclude suspended users from weekly digest candidates"
```

---

## Final verification

- [ ] **Full suite**

Run: `uv run pytest -q`
Expected: all green.

- [ ] **Migration round-trips cleanly** (already done in Task 1, re-confirm at the end):

Run: `DATABASE_URL=… uv run alembic downgrade 0041 && DATABASE_URL=… uv run alembic upgrade head`

- [ ] **Manual smoke** — start the app; as an admin: see a pending self-serve request appear in `/admin/users`, approve it, create a read_only user (confirm no create buttons for them), add an editor to a workspace and confirm they can post a turn after acknowledging disclosure, transfer ownership, deactivate a user and confirm their session dies.

## Self-review notes (coverage vs spec)
- §3 roles/matrix → Tasks 1, 7, 10. §4 lifecycle/enforcement → Tasks 3, 4, 5. §4.4 owner transfer/demote guard → Tasks 5, 9. §4.5 digest → Task 11. §5 guard refactor + route map → Tasks 7, 8. §6 Users panel → Task 6. §7 collaborator UI → Task 9. §8 migration → Tasks 1, 2. §13 decisions all honored.
- No `approval_requests` table: the pending queue is `status='pending'` (consistent with the spec's columns-only migration).
- Ordering: Task 1→2 must land together (suite red between them); 3/4/5 independent; 7 before 8; 9/10/11 after 7/8.
