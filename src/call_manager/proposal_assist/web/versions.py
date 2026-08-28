"""Version history — list a workspace's versions, read one version's markdown.

Read-only and member-gated, modelled on `review.py`'s `review_file`: a sync
`def` so FastAPI runs the git subprocess in its threadpool, and NO commit mutex
(this is a read; taking one would queue reads behind writers for no benefit).

The list is sourced from GIT, not from `artifact_versions`. Four routine commit
paths write no row, and `reconcile` fires only on agent launch, so a collab-only
workspace's DB list can lag git indefinitely. `artifact_versions` is a
provenance OVERLAY joined on sha, every field optional (spec §12).
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import ArtifactVersion, ChatTurn
from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    blob_sha,
    list_commits,
    read_blob,
)
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.ports.artifacts import Artifact, filename_for
from call_manager.proposal_assist.ports.shas import is_sha
from call_manager.proposal_assist.web._deps import _session_dep
from call_manager.proposal_assist.web._perms import require_member

router = APIRouter(prefix="/api/workspaces", tags=["versions"])

#: Matches `_HISTORY_LIMIT` in web/api/admin_retention.py:105, the only other
#: `list_commits` caller. Deliberately NOT a query parameter: measured on the 13
#: live repos, twelve hold one commit and the largest holds 15.
HISTORY_LIMIT = 200

#: A Literal query parameter, so FastAPI itself answers 422 for an unknown
#: value. The alternative — `Artifact(artifact)` inside the body — raises
#: ValueError, which the app-wide handler at main.py:713-716 turns into a 500.
ArtifactParam = Literal["draft", "spec"]


class VersionRow(BaseModel):
    commit_sha: str
    committed_at: str
    subject: str
    #: Overlay fields. Absent means "no artifact_versions row for this commit",
    #: which is ORDINARY — render "not recorded", never an error.
    created_by: str | None = None
    triggering_turn_id: str | None = None
    #: The user's own words from the turn that caused this version, so a row can
    #: answer "why". None wherever there is no turn — which is every
    #: `reconciled` row and every commit with no DB row at all.
    turn_prompt: str | None = None


class VersionListOut(BaseModel):
    versions: list[VersionRow]
    truncated: bool
    #: False when `list_commits` raised: no repo directory, or an unborn HEAD
    #: (a repo with `git init` and no commit). Distinct from an empty list,
    #: which means "a readable history with no commits touching this artifact".
    history_available: bool


class VersionReadOut(BaseModel):
    commit_sha: str
    artifact: str
    markdown: str


#: A prompt is free text with no length bound on the way in. A row shows a
#: reason, not an essay; the marker makes a shortened prompt distinguishable
#: from a short one, mirroring gitio's subject truncation.
_MAX_PROMPT_CHARS = 200
_PROMPT_TRUNCATION_MARKER = "…"


def _prompt_text(content: object) -> str | None:
    """The user's own words out of a `ChatTurn.content` payload.

    `_build_user_content` (turns/submit.py:92-117) emits ONE of two shapes: the
    legacy dict `{"type": "text", "text": …}` for a plain submit, or a LIST of
    blocks whose FIRST block is that same text block when an attachment,
    reference or selection is present. A reader that handles only the dict shape
    silently loses the reason on exactly the turns that carried the most
    context.

    Anything else yields None — no reason shown, never an error.
    """
    if isinstance(content, dict):
        block: object = content
    elif isinstance(content, list) and content:
        block = content[0]
    else:
        return None
    if not isinstance(block, dict) or block.get("type") != "text":
        return None
    text = block.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()
    if len(text) > _MAX_PROMPT_CHARS:
        text = text[:_MAX_PROMPT_CHARS] + _PROMPT_TRUNCATION_MARKER
    return text


@router.get("/{workspace_id}/history", response_model=VersionListOut)
def list_versions(
    workspace_id: uuid.UUID,
    request: Request,
    artifact: ArtifactParam = "draft",
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    require_member(workspace_id=workspace_id, user=user, session=session)
    repo_dir = workspace_repo_dir(request.app.state.settings.workspaces_dir, workspace_id)
    filename = filename_for(Artifact(artifact))

    try:
        commits, truncated = list_commits(repo_dir, limit=HISTORY_LIMIT, pathspec=filename)
    except (OSError, GitCommitError):
        # Legitimate, and the commonest new-workspace state: the repo may not
        # exist yet, or exist with no commits. `git log` on an unborn HEAD exits
        # 128. Report it as a state, exactly as admin_retention.py:291-297 does.
        return {"versions": [], "truncated": False, "history_available": False}

    # sha -> (ArtifactVersion, that turn's content payload or None). An OUTER
    # join: `triggering_turn_id` is nullable and a turn can be deleted
    # (ON DELETE SET NULL), so an inner join would drop provenance rows.
    overlay: dict[str, tuple[ArtifactVersion, object]] = {}
    if commits:
        rows = session.execute(
            select(ArtifactVersion, ChatTurn.content)
            .outerjoin(ChatTurn, ChatTurn.id == ArtifactVersion.triggering_turn_id)
            .where(
                ArtifactVersion.workspace_id == workspace_id,
                ArtifactVersion.commit_sha.in_([c.sha for c in commits]),
            )
        ).all()
        overlay = {av.commit_sha: (av, content) for av, content in rows}

    versions = []
    for c in commits:
        entry = overlay.get(c.sha)
        av = entry[0] if entry else None
        versions.append(
            {
                "commit_sha": c.sha,
                "committed_at": c.authored_at.isoformat(),
                "subject": c.subject,
                "created_by": av.created_by if av else None,
                "triggering_turn_id": (
                    str(av.triggering_turn_id) if av and av.triggering_turn_id else None
                ),
                "turn_prompt": _prompt_text(entry[1]) if entry else None,
            }
        )
    return {"versions": versions, "truncated": truncated, "history_available": True}


@router.get("/{workspace_id}/history/{commit_sha}", response_model=VersionReadOut)
def read_version(
    workspace_id: uuid.UUID,
    commit_sha: str,
    request: Request,
    artifact: ArtifactParam = "draft",
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    require_member(workspace_id=workspace_id, user=user, session=session)
    if not is_sha(commit_sha):
        # 422 at the boundary. `blob_sha` FAILS OPEN — an unresolvable ref is
        # echoed back as if it were a sha — so without this the value reaches
        # `read_blob` and dies with a confusing cat-file message.
        raise HTTPException(status_code=422, detail="commit_sha must be a hex commit sha")

    repo_dir = workspace_repo_dir(request.app.state.settings.workspaces_dir, workspace_id)
    filename = filename_for(Artifact(artifact))
    try:
        markdown = read_blob(repo_dir, blob_sha(repo_dir, commit_sha, filename))
    except (OSError, GitCommitError):
        # Unknown sha, or a real commit that never touched this artifact.
        raise HTTPException(status_code=404, detail="version content unavailable") from None
    return {"commit_sha": commit_sha, "artifact": artifact, "markdown": markdown}
