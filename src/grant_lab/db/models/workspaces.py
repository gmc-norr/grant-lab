"""The proposal-assist workspace and its lifecycle."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from grant_lab.db.base import Base


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint(
            "status IN ('drafting','submitted','awarded','rejected','abandoned')",
            name="ck_workspaces_status",
        ),
        CheckConstraint(
            "current_phase IN ('setup','active')",
            name="ck_workspaces_current_phase",
        ),
        CheckConstraint(
            "citation_style IN ('vancouver','author-year')",
            name="ck_workspaces_citation_style",
        ),
        Index(
            "ix_workspaces_owner_status",
            "owner_id",
            "status",
            postgresql_where=text("soft_deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[int] = mapped_column(
        BigInteger,
        # Explicit RESTRICT makes the (previously default NO ACTION) delete
        # policy intentional: a user who owns workspaces cannot be hard-deleted.
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    call_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("calls.id", ondelete="SET NULL"),
        nullable=True,
        # Indexed so deleting a Call (SET NULL) doesn't full-scan workspaces.
        index=True,
    )
    profile_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("profiles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="drafting",
    )
    deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    soft_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Retention Slice C0. Incremented by EVERY lifecycle transition (delete
    # today, restore in C1) inside that transition's own transaction, under the
    # commit mutex — so the mutex totally orders generations and a transition's
    # teardown can be recognised as belonging to a superseded one. Never read
    # and re-written from Python: see bump_lifecycle_generation.
    lifecycle_generation: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    #: The branch this workspace is on. The DATABASE is the source of truth;
    #: git HEAD is reconciled to this value before any git read or write
    #: (2026-08-29-workspace-branches-design.md §4.1). Never inferred from HEAD.
    current_branch: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'main'"))
    #: The workspace's ROOT branch (§7.3) — the one whose worktree will BE `repo/`
    #: once S2 stops checkout-based switching, and the only branch that cannot be
    #: deleted. **Moved only by promote.** Deliberately NOT `current_branch`:
    #: switching is not promoting, and a pointer that followed every switch would
    #: be `current_branch` under another name.
    #:
    #: Until S2 lands, `switch_branch` step 9 still runs `checkout_branch(repo,
    #: target)` (`branches.py:224-226`), so what `repo/` physically holds drifts
    #: away from this pointer between a switch and S2. That is expected and
    #: harmless here — nothing resolves a path through this column yet — and S2
    #: checks the root back out before it attaches any worktree.
    #:
    #: Nullable in several legitimate states. A workspace still in setup has no repo and
    #: so no branch to point at — and a repo on a DETACHED HEAD has refs but no
    #: knowable root, so `backfill_branch_entities` leaves the pointer alone rather
    #: than guessing which survivor is the root
    #: (`test_a_detached_head_still_founds_the_refs_but_no_root`).
    #:
    #: `tests/architecture/test_branch_entity_invariants.py` therefore pins the
    #: narrower claim that actually holds, and the qualifier is AFTER A SUCCESSFUL
    #: RECONCILE, not merely "has a repo": `backfill_branch_entities` leaves an
    #: attached HEAD with a pointer. Between reconciles it can legitimately be
    #: null with HEAD attached — `create_workspace_branch` clears it when it
    #: de-orphans the row the pointer named, and a workspace the reconciler
    #: SKIPPED (unreadable HEAD) never got one.
    #:
    #: S2 must therefore reconcile before it dereferences this, and handle a
    #: rootless repo explicitly. A repo directory does not imply a pointer.
    root_branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        # use_alter: `workspaces` and `branches` reference each other (this
        # column, and branches.workspace_id -> workspaces.id), so SQLAlchemy
        # cannot order the two for whole-metadata DDL. Deferring THIS side to a
        # post-create ALTER breaks the cycle; it has to be this side because it
        # is the nullable one — branches.workspace_id is NOT NULL and cannot
        # wait. Same shape as fk_users_default_profile_id (models.py:100-112).
        #
        # RESTRICT, not SET NULL: §7.3 says the root branch cannot be deleted,
        # and a database guarantee cannot be forgotten by a future caller the way
        # an `if` can. Safe because nothing hard-deletes a Workspace row —
        # retention is freeze-only (ef8ccb3) — so no cascade collides with it.
        ForeignKey(
            "branches.id",
            ondelete="RESTRICT",
            name="fk_workspaces_root_branch_id",
            use_alter=True,
        ),
        nullable=True,
    )
    current_phase: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="setup",
    )
    spec_locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    spec_locked_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    call_body_snapshot: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="",
    )
    profile_desc_snapshot: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="",
    )
    snapshot_refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    rubric_criteria: Mapped[list[dict]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )
    rubric_funder_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    rubric_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    section_extracts: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    review_runs_per_day_override: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    # Numeric(...) hydrates to decimal.Decimal, not float — annotate as such so
    # arithmetic with Decimal callers stays type-correct (float mixing raises).
    review_dollar_cap_override: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 3),
        nullable=True,
    )
    last_sdk_session_id: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    daily_token_cap_in_override: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    daily_token_cap_out_override: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    citation_style: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="vancouver",
    )


class Branch(Base):
    """One branch of one workspace (spec 2026-08-30-per-branch-worktrees §3.2).

    The **id** is the identity; `name` is a mutable label on it. Every
    branch-scoped row and every job carries the id, never the name — otherwise a
    delete-then-recreate under the same name lets an in-flight job pass the
    workspace fence and write into the replacement. Names are also allowed to
    contain `/` (`git check-ref-format --branch`), which no single URL path
    segment can carry.
    """

    __tablename__ = "branches"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_branches_ws_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    #: The hosted-conversation resume handle for THIS branch (§3.1). Two branches
    #: cannot resume one remote session — `turns/processor.py:389` passes
    #: `resume=<id>`, and sharing one would interleave two branches' turns into a
    #: single upstream conversation. `workspaces.last_sdk_session_id` still exists
    #: and is still the only one read; it is dropped in S4 when its last reader
    #: goes.
    last_sdk_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkspaceViewer(Base):
    __tablename__ = "workspace_viewers"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class WorkspaceEvent(Base):
    __tablename__ = "workspace_events"
    __table_args__ = (
        Index(
            "ix_workspace_events_ws_created",
            "workspace_id",
            text("created_at DESC"),
        ),
        Index(
            "uq_workspace_events_rubric_initialized",
            "workspace_id",
            unique=True,
            postgresql_where=text("kind = 'rubric_initialized'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class PhaseApproval(Base):
    __tablename__ = "phase_approvals"
    __table_args__ = (
        CheckConstraint(
            "phase IN ('spec','plan','draft','review')",
            name="ck_phase_approvals_phase",
        ),
        Index(
            "ix_phase_approvals_ws_phase_at",
            "workspace_id",
            "phase",
            text("approved_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    approving_user_id: Mapped[int] = mapped_column(
        BigInteger,
        # Explicit RESTRICT — matches the prior default NO ACTION behavior.
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approved_artifact_sha: Mapped[str] = mapped_column(Text, nullable=False)


class SpecLockEvent(Base):
    """Append-only audit of spec lock/unlock toggles (cockpit SP2b-2a).

    One row per real state transition (idempotent no-ops write nothing).
    actor_id is SET NULL on user delete so the audit row survives.
    """

    __tablename__ = "spec_lock_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ('lock','unlock')",
            name="ck_spec_lock_events_action",
        ),
        Index(
            "ix_spec_lock_events_ws_created",
            "workspace_id",
            text("created_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    artifact_commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class WorkspaceSetupJob(Base):
    __tablename__ = "workspace_setup_jobs"
    __table_args__ = (
        UniqueConstraint("workspace_id", name="uq_workspace_setup_jobs_ws"),
        CheckConstraint(
            "status IN ('queued','running','waiting_for_rubric_review','done',"
            "'failed','cancelled')",
            name="ck_workspace_setup_jobs_status",
        ),
        CheckConstraint(
            "cancelled_from IS NULL OR cancelled_from IN "
            "('queued','running','waiting_for_rubric_review')",
            name="ck_workspace_setup_jobs_cancelled_from",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # Fence F1 (spec 2026-08-17 §3.3). The workspaces.lifecycle_generation this
    # actor observed AT CLAIM TIME. Every semantic effect it later produces is
    # fenced against this value, so work belonging to an epoch the workspace has
    # left cannot commit. NULL = legacy row, claimed before the fence existed;
    # those fall back to the liveness check. NOT defaulted to 0 — 0 is a real
    # generation. Set by the claim statement in F4-F7; unused in F1.
    claimed_at_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default=text("0"),
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Retention Slice C1 (spec 5.2.2). The non-terminal state this job was
    # cancelled FROM when its workspace was deleted, so restore can map it back
    # to a state that progresses. NULL means "not cancelled by a deletion" —
    # restore leaves those alone. Consumed (set back to NULL) by restore.
    cancelled_from: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
