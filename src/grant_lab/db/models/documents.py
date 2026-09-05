"""What a workspace contains: turns, artefacts, attachments, CRDT state."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from grant_lab.db.base import Base


class ChatTurn(Base):
    __tablename__ = "chat_turns"
    __table_args__ = (
        CheckConstraint(
            "role IN ('user','assistant','tool','system')",
            name="ck_chat_turns_role",
        ),
        Index("ix_chat_turns_ws_created", "workspace_id", "created_at"),
        Index("ix_chat_turns_ws_position", "workspace_id", "position"),
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
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    position: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("nextval('chat_turns_position_seq')"),
    )


class ChatSummary(Base):
    __tablename__ = "chat_summaries"
    __table_args__ = (
        Index(
            "ix_chat_summaries_ws_created",
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
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    summarized_through: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_turns.id", ondelete="CASCADE"),
        nullable=False,
    )
    tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class ArtifactVersion(Base):
    __tablename__ = "artifact_versions"
    __table_args__ = (
        CheckConstraint(
            "created_by IN ('agent','user','system','reconciled','unrecoverable')",
            name="ck_artifact_versions_created_by",
        ),
        Index(
            "ix_artifact_versions_ws_created",
            "workspace_id",
            text("created_at DESC"),
        ),
        Index(
            "uq_artifact_versions_ws_sha",
            "workspace_id",
            "commit_sha",
            unique=True,
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
    commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    triggering_turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_turns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class ProposedChange(Base):
    __tablename__ = "proposed_changes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','accepted','rejected','superseded','stale')",
            name="ck_proposed_changes_status",
        ),
        CheckConstraint(
            "target_artifact IN ('draft','spec')",
            name="ck_proposed_changes_target_artifact",
        ),
        CheckConstraint(
            "kind IN ('prose_edit','citation_insert','citation_remove','citation_update')",
            name="ck_proposed_changes_kind",
        ),
        CheckConstraint(
            "origin_kind IN ('interactive_turn','coherence_session','evaluator_session','human')",
            name="ck_proposed_changes_origin_kind",
        ),
        Index("ix_proposed_changes_ws_status", "workspace_id", "status"),
        Index(
            "ix_proposed_changes_ws_target",
            "workspace_id",
            "target_artifact",
            "section_index",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    target_artifact: Mapped[str] = mapped_column(Text, nullable=False)
    section_index: Mapped[int] = mapped_column(Integer, nullable=False)
    base_commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    base_section_sha: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    origin_kind: Mapped[str] = mapped_column(Text, nullable=False)
    origin_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    triggering_turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_turns.id", ondelete="SET NULL"),
        nullable=True,
    )
    triggering_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resulting_commit_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('figure','preliminary_data','past_grant','other')",
            name="ck_attachments_kind",
        ),
        Index("ix_attachments_ws", "workspace_id"),
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
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by: Mapped[int] = mapped_column(
        BigInteger,
        # Explicit RESTRICT — matches the prior default NO ACTION behavior.
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # B7: image dimensions + pre-computed vision token estimate.
    # Populated at upload time from a Pillow probe (see web/files.py).
    # NULLable for two narrow reasons:
    # 1. T2 panel inserts rows before T3 backfills the upload handler;
    #    rows from that window have NULL dims until re-probed on submit.
    # 2. Pre-B7 rows from any prior B5 demo data.
    # NULL is NOT treated as zero in the cap sum — submit_user_turn
    # re-probes from disk and persists the values before counting
    # (see proposal_assist/turns/submit.py).
    width_px: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height_px: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)


class YjsUpdate(Base):
    """Append-only Yjs update log for the collaborative editor.

    One row per persisted CRDT update, keyed by (workspace_id, artifact) =
    the room. `actor_id` is the authenticated user who sent the update (for
    authorship in later slices). Ordering is by the autoincrement `id`.
    """

    __tablename__ = "yjs_updates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    update_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("artifact IN ('draft','spec')", name="ck_yjs_updates_artifact"),
        Index("ix_yjs_updates_room", "workspace_id", "artifact", "id"),
    )


class CollabArtifactState(Base):
    """Per-(workspace, artifact) collaborative-editing state: the feature flag
    (`enabled_at`, NULL = off) plus the checkpoint watermark (the highest
    yjs_updates.id already checkpointed to git) and last checkpoint commit."""

    __tablename__ = "collab_artifact_state"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    artifact: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checkpoint_update_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_checkpoint_commit_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The document epoch (spec 2026-09-02-collab-doc-epoch-design.md): bumped by
    # state.reset_checkpoint every time the log is truncated, shown to every
    # client on connect, presented back on reconnect. A mismatch is a Y.Doc from
    # a previous document and is refused (4409) before it can send its state.
    doc_epoch: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0", default=0
    )

    __table_args__ = (
        CheckConstraint("artifact IN ('draft','spec')", name="ck_collab_artifact_state_artifact"),
    )
