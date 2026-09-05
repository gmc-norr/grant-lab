"""SqlQueries — column selects mapped into detached DTOs.

Never `select(Model)` and never `session.get(...)`: an entity result is an
attached instance, and handing one back would hand back its Session. Selecting
columns makes that mistake structurally impossible rather than merely discouraged.

Each method takes the workspace_id and scopes on it, rather than returning a row
for the caller to compare afterwards — a comparison the caller can forget.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from grant_lab.db.models import Reference, ReferenceChunk, Workspace
from grant_lab.proposal_assist.ports.queries import (
    ReferenceChunkView,
    ReferenceView,
    WorkspaceSnapshotView,
)


class SqlQueries:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._sessions = session_factory

    def get_reference(
        self, workspace_id: uuid.UUID, reference_id: uuid.UUID
    ) -> ReferenceView | None:
        with self._sessions() as session:
            row = session.execute(
                select(
                    Reference.id,
                    Reference.workspace_id,
                    Reference.citekey,
                    Reference.title,
                    Reference.tombstoned_at,
                ).where(
                    Reference.id == reference_id,
                    Reference.workspace_id == workspace_id,
                )
            ).first()
        if row is None:
            return None
        return ReferenceView(
            id=row[0],
            workspace_id=row[1],
            citekey=row[2],
            title=row[3],
            is_tombstoned=row[4] is not None,
        )

    def get_reference_chunk(
        self, workspace_id: uuid.UUID, reference_id: uuid.UUID, chunk_index: int
    ) -> ReferenceChunkView | None:
        with self._sessions() as session:
            row = session.execute(
                select(
                    ReferenceChunk.reference_id,
                    ReferenceChunk.workspace_id,
                    ReferenceChunk.chunk_index,
                    ReferenceChunk.section_path,
                    ReferenceChunk.text,
                ).where(
                    ReferenceChunk.reference_id == reference_id,
                    ReferenceChunk.workspace_id == workspace_id,
                    ReferenceChunk.chunk_index == chunk_index,
                )
            ).first()
        if row is None:
            return None
        return ReferenceChunkView(
            reference_id=row[0],
            workspace_id=row[1],
            chunk_index=row[2],
            section_path=row[3],
            text=row[4],
        )

    def get_workspace_snapshot(
        self, workspace_id: uuid.UUID
    ) -> WorkspaceSnapshotView | None:
        with self._sessions() as session:
            row = session.execute(
                select(
                    Workspace.call_body_snapshot,
                    Workspace.rubric_criteria,
                    Workspace.rubric_funder_notes,
                    Workspace.profile_desc_snapshot,
                ).where(Workspace.id == workspace_id)
            ).first()
        if row is None:
            return None
        return WorkspaceSnapshotView(
            call_body=row[0],
            rubric_criteria=list(row[1] or []),
            rubric_funder_notes=row[2] or "",
            profile_desc=row[3],
        )
