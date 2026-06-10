# User-system redesign + security hardening — design

Status: draft for review
Date: 2026-06-10
Supersedes the open-self-registration auth model.

## 1. Why

Two motivations, addressed together because they touch the same auth surface:

1. **Internet-exposure readiness.** A whole-app security review found one HIGH
   blocker (cloud-LLM disclosure gate enforced UI-only) plus medium robustness
   gaps and deployment-config requirements. These must be closed before the app
   is reachable from outside the hospital network.
2. **Admin control over users.** Today any email in an allowed domain can
   self-register and is instantly active ([auth.py:147] creates the `User` row
   on the fly at first magic-link request). `ALLOWED_EMAIL_DOMAINS` is the only
   gate — and the dev `.env` lists `gmail.com`, so a config leak would grant the
   world access. We move to an **admin-controlled, approval-based** model with
   real roles and offboarding.

This design was pressure-tested by a four-lens review (authz red-team, lifecycle
state machine, route coverage, migration hazards) against the real codebase; the
findings are folded in below and the resolved forks are listed in §13.

## 2. Scope

**In scope**
- Two-layer authorization: system membership (admin-gated) + workspace
  collaboration (owner-gated).
- System roles `admin | staff | read_only`; user lifecycle `pending | active |
  suspended`.
- Self-serve request → admin approval; admin account-creation; deactivate /
  reactivate offboarding.
- Workspace collaboration roles `owner | editor | viewer` (new: **editor**).
- Admin Users panel (list/create/approve/deny/set-role/deactivate/reactivate/
  revoke-sessions) and workspace owner-transfer.
- The four folded-in security fixes: server-side disclosure gate, file-parse
  wall-clock timeout, catch-all exception handler, deployment hardening docs.

**Out of scope (v1)**
- Hard-delete / GDPR-erase of users. Every substantive user is referenced by a
  `RESTRICT` FK (owned workspaces, profiles, uploads, turns, approvals), so DB
  delete is infeasible; offboarding = `suspended`. A scrub job is a separate
  future effort.
- Invite emails. Admin-created accounts are `active`; the user signs in via the
  normal magic-link page (works in dev with `ConsoleEmailSender`). Account
  creation does **not** send mail.
- Roles richer than the three tiers; per-feature permission matrices beyond what
  §3 defines.
- Multi-instance deployment (the single-process APScheduler constraint stands).

## 3. Authorization model

### 3.1 Two layers

- **System membership** — whether you can log in and your global capabilities.
  Admin-gated.
- **Workspace collaboration** — your access to one proposal. Owner-gated.

They are independent. An owner may only add **existing approved users** to a
workspace — there is no auto-provisioning side-door.

### 3.2 System roles

| Capability | admin | staff | read_only |
|---|---|---|---|
| Browse calls / rankings, hide calls | ✓ | ✓ | ✓ |
| Create & edit own profiles | ✓ | ✓ | — |
| Create workspaces / be workspace **editor** or **owner** | ✓ | ✓ | — |
| Be a workspace **viewer** | ✓ | ✓ | ✓ |
| Post turns (cloud drafting) | ✓\* | ✓\* | — |
| Manage users (approve/deny/create/role/deactivate) | ✓ | — | — |
| Operational admin panels | ✓ | — | — |

\* only as owner/editor of that workspace, and only after acknowledging the
disclosure gate (§9).

**Cross-layer invariant:** a `read_only` system user may be a workspace
**viewer** only — never editor/owner. Enforced server-side (§3.4), not in the
template.

### 3.3 Workspace roles

| Capability | owner | editor | viewer |
|---|---|---|---|
| View proposal / chat / files / refs / citations | ✓ | ✓ | ✓ |
| Post turns; edit sections; upload/fetch/manage refs, files, citations, annotations | ✓ | ✓ | — |
| Acknowledge disclosure (per-user) | ✓ | ✓ | n/a |
| Cancel an in-flight turn | ✓ | ✓ | — |
| Workspace settings (title/status/style/deadline) | ✓ | — | — |
| Add/remove collaborators; transfer ownership | ✓ | — | — |
| Soft-delete workspace | ✓ | — | — |
| Confirm Phase-0 rubric review | ✓ | — | — |

Owner is implicitly `>= editor >= viewer`. Collaborator management is
**owner-only** in v1 (editors cannot invite).

### 3.4 Where each gate lives (no UI-only guards)

- **System role** is read fresh from the DB on every request (`get_current_user`
  loads the `User` row each time, [dependencies.py:20]), so a role change takes
  effect on the next request with no cookie reissue.
- **`read_only` cannot be editor** is enforced in `add_viewer` (and any future
  role-change route): reject `role=editor` when `target.role == 'read_only'`.
  Belt-and-suspenders: at the moment a user is demoted to `read_only`, any
  `workspace_viewers` rows of theirs with `role=editor` are downgraded to
  `viewer` in the same transaction (single source of truth), and the
  editor-write guard additionally requires `actor.role in {staff, admin}`.

## 4. User lifecycle

States: `pending → active → suspended` (all transitions admin-driven except the
initial self-serve `→ pending`).

### 4.1 State-transition table

| From | Event | To | Rules |
|---|---|---|---|
| (none) | self-serve login request, allowed domain | `pending` | create `User(status=pending)` + one approval-request row; **no** magic link sent; neutral "request received" page |
| (none) | admin creates account (email + role) | `active` | created `active` with chosen role; no email sent |
| `pending` | self-serve request again (same email) | `pending` | idempotent — no duplicate approval row, no token |
| `pending` | admin **approve** (+role) | `active` | set role; close approval request; record `approved_by/at` |
| `pending` | admin **deny** | `suspended` | blocks re-request spam; no hard-delete |
| `pending` | admin **create** for same email | `active` | approve-in-place: set `active` + chosen role, close approval |
| `active` | admin **deactivate** | `suspended` | bump `session_epoch` (kills live sessions); invalidate outstanding magic-link tokens |
| `active` | admin **revoke-sessions** | `active` | bump `session_epoch` only (force re-login, no offboard) |
| `suspended` | admin **reactivate** (explicit confirm) | `active` | reversible; digest/subscription state restored automatically (no flags were touched) |
| `suspended` | self-serve request | `suspended` | silent no-op — no token, no resurrection |
| `suspended` | admin **create**/approve for same email | `active` | requires explicit reactivate confirm (don't silently un-offboard) |
| any | role change (admin) | same status | last-admin + self guards (§4.3); demote-below-staff blocked while owning a live workspace (§4.4) |

All user-state transitions serialize on the user row with `SELECT … FOR UPDATE`
(or an idempotent `UPDATE … WHERE status='pending' RETURNING`) so two concurrent
admins converge instead of last-write-wins. The self-serve-vs-admin-create race
relies on `UNIQUE(email)`: the loser catches `IntegrityError`, re-reads, and
applies the merge rule.

### 4.2 Authentication enforcement (the authoritative gates)

- **`get_current_user`** ([dependencies.py:9]): after loading the user and before
  expunge, add `if user.status != 'active': raise HTTPException(401, ...)`. This
  single check makes `pending` and `suspended` unauthenticated on **every** route
  (every guard funnels through here, including `require_admin`). It is the
  authority; the `session_epoch` bump is a complementary immediate-revocation
  mechanism, not the sole one.
- **`login_submit`** ([auth.py:82]): branch by existing row —
  - no row, allowed domain → create `pending` + approval request, show "request
    received"; **do not** issue a magic link.
  - `active` → unchanged (issue magic link).
  - `pending`/`suspended` → neutral page, no token (preserve the existing
    enumeration-resistance contract — same response shape regardless).
- **`login_verify`** ([auth.py:179]): after loading the user, refuse to mint a
  cookie when `status != 'active'`. Session-minting is the chokepoint, so the
  same login page serves admin-created `active` users without special-casing.
- **`require_staff`** (new dependency, role in `{staff, admin}`): gates
  create-profile and create-workspace routes. Browse/list routes stay on
  `get_current_user` (read_only may browse).

### 4.3 Guardrails (admin user-management routes)

In every role/status mutation handler, server-side, in order:
1. `require_admin`.
2. If `target.id == actor.id` and the change is a self-demotion or self-suspend
   → 403 "cannot demote/suspend yourself".
3. If the change would drop the count of `(role==admin AND status==active)` users
   below 1 → 409 "cannot remove the last admin". Computed with `SELECT … FOR
   UPDATE` inside the mutation transaction to avoid a TOCTOU race between two
   concurrent demotions.

### 4.4 Owner suspension & offboarding cleanup

`workspaces.owner_id` is `ondelete=RESTRICT` and access is gated purely on
`owner_id == user.id`, so naively suspending an owner strands every collaborator
(no one can post turns, change settings, or delete). Policy:

- Suspension does **not** touch owned-workspace rows.
- Owner-only and editor write actions additionally require `actor.status ==
  active` (inherited from `get_current_user`), so a suspended owner simply loses
  access while the workspace and its collaborators keep working read-wise.
- New **`POST /workspaces/{id}/transfer-owner`** (owner or admin) reassigns
  `owner_id` to another `active` staff/admin user; the old owner becomes an
  editor row or is removed. This is the supported path to fully offboard an owner
  and let their workspaces be managed/archived.
- `soft_delete` accepts **admin** as well as owner, so an admin can archive a
  suspended owner's workspace after (or instead of) transfer.
- Demotion to `read_only` is **forbidden while the user owns a live
  (non-soft-deleted) workspace** — force a transfer first. (A read_only owner
  would contradict the role's stated capabilities.)

### 4.5 Digest interaction

Digest recipients are selected by subscription + `digest_disabled_at IS NULL`
([send.py:72]) with no status filter — a suspended user would keep receiving
weekly mail. Fix: add `User.status == 'active'` to the digest candidate query.
Leave `digest_disabled_at`/`digest_bounce_count` untouched so reactivation
restores the prior digest state automatically.

## 5. Workspace authorization refactor

Today two helpers (`_require_owner`, `_require_owner_or_viewer`) are **duplicated
verbatim across 5 files** (workspaces.py, files.py, references.py, plus
rubric_review.py's own private `_require_owner`; sections/annotations/citations
import copies). `_require_owner_or_viewer` returns success for any viewer row
regardless of role. Adding an `editor|viewer` column without rewriting all copies
would let viewers write or lock editors out, inconsistently per router.

**Centralize** in one module (`proposal_assist/web/_guards.py`); delete every
duplicate. Three guards keyed off the effective role (owner > editor > viewer):

- `_require_member` — any role (read). Rename of `_require_owner_or_viewer`.
- `_require_editor` — owner OR `workspace_viewers.role == 'editor'`; also requires
  `actor.role in {staff, admin}` (write).
- `_require_owner` — owner only (manage/settings/delete).

### 5.1 Route → guard map (backbone of the plan)

Mutation routes moving `_require_owner` → `_require_editor`: `post_turn`,
`cancel_turn`, section edit/save, all annotation writes (insert/dismiss/
reanalyze), file upload/delete, reference upload/fetch/metadata/waiver.

Staying owner-only: settings GET/POST, add/remove collaborator, transfer-owner,
soft-delete, rubric-review confirm.

Read routes `_require_owner_or_viewer` → `_require_member` (identical semantics,
new name): workspace main, SSE stream, all artifact GETs, section view, section
annotations GET, files list, serve file, references panel, serve reference raw,
citations panel, validate, export(s), disclosure POST.

System-role gate (`get_current_user` → `require_staff`): `GET/POST
/profiles/new`, `GET/POST /workspaces/new`. `profile_edit_submit` keeps its
owner-or-admin body check and additionally blocks `read_only`.

`post_disclosure` → `_require_member` (editors must be able to ack; the actual
block on unacked editors is the `post_turn` gate). The disclosure modal is hidden
from pure viewers (it gates nothing for them).

Full per-route table (47 routes) is captured in the implementation plan; the
review's table is the source.

### 5.2 Template flag split

`is_owner` is overloaded for both "can write" and "can manage". Split into:
- `can_edit` (owner OR editor) → composer, upload/delete forms, section-edit
  buttons, annotation actions, reference metadata/waiver controls.
- `can_manage` (owner only) → Settings link, collaborator management,
  transfer/delete.
- New `can_create` (role != read_only) → "+ New profile", "+ New workspace",
  "Start drafting workspace" buttons.

`user.is_admin` reads in `base.html`/`me.html` survive unchanged via the property
shim (§8).

## 6. Admin Users panel

New routes under `/admin/users/*`, all behind `require_admin`, surfaced as a
subnav link on `/admin`:

| Route | Action |
|---|---|
| `GET /admin/users` | list users: email, name, role, status, last login, #owned workspaces; highlight pending |
| `POST /admin/users` | account-only create (email + role) → `active`; merge rules per §4.1 |
| `POST /admin/users/{id}/approve` | `pending → active` + assign role |
| `POST /admin/users/{id}/deny` | `pending → suspended` |
| `POST /admin/users/{id}/role` | set role (guardrails §4.3; demote-below-staff block §4.4) |
| `POST /admin/users/{id}/deactivate` | `active → suspended` + bump `session_epoch` |
| `POST /admin/users/{id}/reactivate` | `suspended → active` (confirm step) |
| `POST /admin/users/{id}/revoke-sessions` | bump `session_epoch` only |

UI: a single table with inline action buttons; pending requests pinned to the top
with approve/deny. Confirm dialogs (existing `data-confirm`) for deactivate/
reactivate/role changes. Styled with the existing design system.

## 7. Workspace collaborator UI

`workspace_settings.html` add-collaborator form gains a `role` select
(`editor | viewer`). Each existing collaborator row shows its role. `add_viewer`
accepts `role: Form('viewer')`, allowlists it, and rejects `editor` for a
`read_only` target (400/403). Owner-transfer is a separate small form (pick an
active staff/admin member).

## 8. Data model & migration

Recommended type: **Text + CheckConstraint** for all new enums — matches house
style (Workspace.status/current_phase/citation_style, ChatTurn.role are all
Text+Check), trivial future value additions, and no native-enum downgrade trap
(migration 0034 is the documented cautionary tale).

### 8.1 Columns

```python
# users — replace is_admin Boolean with:
role:   Mapped[str] = mapped_column(Text, nullable=False, server_default="staff")
status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
approved_by: Mapped[int | None] = mapped_column(
    BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
approved_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True)
# table_args:
CheckConstraint("role IN ('admin','staff','read_only')", name="ck_users_role")
CheckConstraint("status IN ('pending','active','suspended')", name="ck_users_status")

@property
def is_admin(self) -> bool:      # read-only shim; NO setter
    return self.role == "admin"

# workspace_viewers — add:
role: Mapped[str] = mapped_column(Text, nullable=False, server_default="viewer")
CheckConstraint("role IN ('editor','viewer')", name="ck_workspace_viewers_role")
```

### 8.2 Migration 0042 (Alembic, down_revision 0041)

Upgrade order: add `status`/`role` nullable with server_default → backfill
(`status='active'`; `role = CASE WHEN is_admin THEN 'admin' ELSE 'staff' END`) →
add check constraints → set NOT NULL → add audit cols + FK → add
`workspace_viewers.role` (server_default `'viewer'` backfills existing rows) →
**drop `is_admin` last** (after role is derived from it). Downgrade re-adds
`is_admin Boolean`, sets `is_admin = (role='admin')`, drops the new
columns/constraints. With Text+Check there is no `DROP TYPE` ordering hazard.

### 8.3 `is_admin` column-drop fallout — must fix BEFORE dropping the column

A Python `@property` is invisible to SQLAlchemy query compilation. Read sites via
the property are fine; write/query sites break (silent 500s if missed):

| Site | Fix |
|---|---|
| `admin_bootstrap.py:51` `User(email=…, is_admin=True)` | `User(email=…, role='admin', status='active')` |
| `admin_bootstrap.py:53` `user.is_admin = True` | `user.role = 'admin'` (+ ensure `status='active'`) |
| `seed_dev_data.py:286` `User.is_admin.is_(True)` (query expr) | `User.role == 'admin'` |
| `test_alembic_v2_round_trip.py:63` raw INSERT `is_admin` | use `role`,`status` columns |
| ~15 test files constructing `User(is_admin=…)` | convert kwarg → `role=` |

Read sites that survive via the property: `dependencies.py:33` (`require_admin`),
`profiles.py:321` (owner-or-admin), `base.html:41`, `me.html:6`. Add a unit test
pinning `is_admin == (role == 'admin')`.

### 8.4 Test-fixture remediation

`status` server_default **must** be `active` and `role` default `staff`, or the
~1273 existing test functions whose `User(email=…)` fixtures omit role/status
become `pending`/`read_only` and the suite collapses once the auth gate lands.
Fixture factories to verify: `tests/conftest.py:116`,
`tests/proposal_assist/conftest.py:171` (`make_user`). Tests that exercise
read_only/viewer/editor or pending/suspended set those explicitly.

`bootstrap_admins` updated to set `role='admin', status='active'` idempotently;
`INITIAL_ADMIN_EMAIL` behavior otherwise unchanged.

## 9. Folded-in security fixes

1. **Disclosure gate (HIGH).** In `post_turn`, after the editor check and before
   `submit_user_turn`, query for a prior `WorkspaceEvent(kind=
   'disclosure_acknowledged', actor_id=user.id, workspace_id=ws.id)`; if absent,
   return 403 / an HTMX fragment re-showing the modal. Per-actor: each editor
   acknowledges before their first turn. `post_disclosure` is `_require_member`
   so editors can ack.
2. **File-parse wall-clock timeout (MED).** `extract_text` (pdfminer / striprtf /
   lxml) runs synchronously in-process with byte/page caps but no CPU/time bound,
   so a small pathological file can pin the single worker. Run extraction in a
   bounded executor with a wall-clock kill (or subprocess with rlimit); on
   timeout, mark the reference `text_status=failed`.
3. **Catch-all exception handler (MED).** Add `@app.exception_handler(Exception)`
   returning a styled `error.html` (text/html) or `{"detail":"internal error"}`
   (JSON), logging server-side with no internals in the body. FastAPI dispatches
   the more specific `HTTPException` handler first, so the existing 401→/login
   and `_HTML_ERROR_STATUSES` behavior is preserved.
4. **Deployment hardening.** Add `.env.production.example` + a checklist:
   real `ALLOWED_EMAIL_DOMAINS` (no `gmail.com`), TLS-terminating proxy with
   uvicorn bound to loopback, `APP_BASE_URL=https://…` (enables Secure cookies +
   HSTS, forces strong `SESSION_SECRET`, refuses console email), `TRUSTED_PROXY=
   true` + `TRUSTED_PROXY_HOPS` with the edge proxy overwriting `X-Forwarded-For`,
   and `RESEND_API_KEY`/`SMTP_HOST`. (The approval gate now also defangs an
   `ALLOWED_EMAIL_DOMAINS` misconfig: a stray-domain self-request only creates a
   `pending` row an admin must approve.)

## 10. Testing strategy

- **Authz matrix:** per router, assert a viewer gets 403 on every editor route
  and an editor gets 403 on every owner-only route; read_only gets 403 on
  create-profile/create-workspace; read_only cannot be added as editor.
- **Lifecycle:** pending/suspended user with a forged-but-valid-epoch cookie →
  401 from `get_current_user`; self-serve creates pending + one approval (idempotent
  on repeat); deny → suspended blocks re-login; suspend bumps epoch and kills a
  live session; reactivate restores access and digest.
- **Guardrails:** self-demote/self-suspend → 403; last-admin demote/suspend →
  409 (incl. a concurrent-demotion race test); demote-to-read_only while owning a
  live workspace → blocked.
- **Owner transfer:** suspended owner's workspace is accessible to editors,
  manageable after transfer; admin can soft-delete a suspended owner's workspace.
- **Disclosure gate:** editor with no ack POSTs `/turns` → blocked; after
  `/disclosure` → allowed.
- **Migration:** round-trip up/down; backfill correctness; `is_admin` property
  equivalence; full suite green after fixture remediation.

## 11. Rollout / sequencing

**P1 — Security fixes (small, independent, ship first):** disclosure-gate
enforcement (per-actor), file-parse timeout, catch-all exception handler,
deployment docs. No schema change; landable immediately.

**P2 — User-system redesign (the larger piece):** migration 0042 + model →
auth-enforcement changes (`get_current_user` status gate, login rewrite,
`require_staff`) → centralized workspace guards + route remap + template flag
split → admin Users panel + owner-transfer → digest filter → fixture remediation.
P2's editor model extends P1's disclosure gate to every editor.

## 12. Component boundaries

- `auth/dependencies.py` — `get_current_user` (status gate), `require_admin`,
  new `require_staff`. The single authentication chokepoint.
- `proposal_assist/web/_guards.py` (new) — `_require_member/_require_editor/
  _require_owner`, imported everywhere; deletes 5 duplicates.
- `users/service.py` (new) — all user state transitions (create/approve/deny/
  set-role/deactivate/reactivate), each serialized + guardrailed; the only writer
  of `role`/`status`.
- `web/routes/admin.py` — Users-panel routes (thin; delegate to the service).
- Migration 0042 + `db/models.py` — schema.

Keeping role/status mutation behind one service (not scattered across handlers)
is the structural defense against a careless self-promotion endpoint.

## 13. Resolved design decisions

| Question | Decision |
|---|---|
| Registration model | Domain self-serve + admin approval |
| Invites | Account-only (admin pre-seeds `active`; user self-signs-in); no invite email |
| System roles | Three tiers: admin / staff / read_only |
| Workspace collaboration | Existing approved users only; roles owner/editor/viewer |
| Editors managing collaborators | Owner-only in v1 |
| Deny semantics | `pending → suspended` (no hard-delete) |
| Pending/suspended enforcement | Authoritative `status` check in `get_current_user` + refuse at `login_verify` |
| Workspace guard refactor | Centralize 3 guards in one module; delete 5 duplicates |
| read_only→editor invariant | App-layer check in `add_viewer` + downgrade-on-demotion; tests (no cross-table trigger in v1) |
| Last-admin/self guards | `SELECT … FOR UPDATE` count in the mutation transaction |
| Owner suspension | Block owner access, preserve workspace, explicit transfer-owner route; admin soft-delete allowed |
| Demote-to-read_only while owning | Forbidden until ownership transferred |
| Digest vs suspension | Filter digest query on `status='active'`; leave digest flags untouched |
| Editor row on demotion | Auto-downgrade editor→viewer at demotion (single source of truth) |
| Hard-delete | Out of scope; suspend only |
| WorkspaceViewer.role backfill | Existing rows → `viewer` (faithful to today's read-only) |
| Enum representation | Text + CheckConstraint (not native ENUM) |
| reference waiver | Editor action (refs/citations management) |

[auth.py:147]: ../../../src/call_manager/web/routes/auth.py
[auth.py:82]: ../../../src/call_manager/web/routes/auth.py
[auth.py:179]: ../../../src/call_manager/web/routes/auth.py
[dependencies.py:9]: ../../../src/call_manager/auth/dependencies.py
[dependencies.py:20]: ../../../src/call_manager/auth/dependencies.py
[dependencies.py:33]: ../../../src/call_manager/auth/dependencies.py
[send.py:72]: ../../../src/call_manager/digest/send.py
[profiles.py:321]: ../../../src/call_manager/web/routes/profiles.py
