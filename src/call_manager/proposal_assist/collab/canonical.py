"""Read-only room-canonical artifact projection (Slice 3a).

The durable Yjs update log is the canonical committed state of a collab-enabled
artifact (every accepted edit persists before it is broadcast). This module
derives an artifact's NORMALIZED markdown WITHOUT touching the live in-memory
room and WITHOUT persisting anything — so every consumer that compares a captured
base against a recomputed live section SHA uses the same representation. Callers
gate on ``is_collab_enabled``; the flag-off paths keep reading raw git.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from pycrdt import Doc

from call_manager.proposal_assist.artifacts import Artifact, filename_for
from call_manager.proposal_assist.collab import store
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import ydoc_to_pm_json

_ARTIFACT = {"draft": Artifact.DRAFT, "spec": Artifact.SPEC}

# ProseMirror's ``doc`` node requires ``block+`` (at least one block child), so an
# empty / whitespace-only / absent HEAD markdown has no valid PM representation.
# Canonicalize it to a single empty paragraph — the same seed hydration uses
# (hydrate imports this constant + markdown_to_pm_or_empty so the two cannot drift).
_EMPTY_DOC = {"type": "doc", "content": [{"type": "paragraph"}]}


def markdown_to_pm_or_empty(md: str | None) -> dict:
    """``markdown_to_pm``, but map ``None`` / whitespace-only to ``_EMPTY_DOC``.
    Raw ``markdown_to_pm`` raises ``ValueError`` ("Invalid content for node doc")
    on blank input; this is the single canonicalizer shared with hydration."""
    if md is None or not md.strip():
        return _EMPTY_DOC
    return markdown_to_pm(md)


def build_doc(updates: list[bytes]) -> Doc:
    """Replay Yjs updates into a fresh Doc."""
    doc = Doc()
    for u in updates:
        doc.apply_update(u)
    return doc


def read_head_markdown(repo_dir, artifact: str) -> str | None:
    """The artifact's on-disk HEAD markdown, or ``None`` when the file is absent."""
    path = Path(repo_dir) / filename_for(_ARTIFACT[artifact])
    return path.read_text() if path.exists() else None


def canonical_artifact_markdown(
    session, *, workspace_id: uuid.UUID, artifact: str, repo_dir
) -> str:
    """Room-canonical (normalized) markdown for a collab-enabled artifact.

    Log non-empty -> serialize the durable log (canonical committed state).
    Log empty, absent file -> ``""`` — matches the un-seeded room's serialization
    (hydration does NOT seed for an absent file; the empty room round-trips to "").
    Log empty, whitespace-only existing file -> the canonical empty doc, matching
    the hydration seed (``markdown_to_pm_or_empty`` maps whitespace to ``_EMPTY_DOC``).
    NEVER writes. Callers MUST gate on ``is_collab_enabled``; the flag-off path
    keeps reading raw git.
    """
    updates = store.load_updates(session, workspace_id=workspace_id, artifact=artifact)
    if updates:
        return pm_to_markdown(ydoc_to_pm_json(build_doc(updates)))
    md = read_head_markdown(repo_dir, artifact)
    if md is None:
        # Absent file: mirror hydration's no-seed rule. An empty room serializes to
        # "" (no blocks), so the base captured here is byte-identical to the live SHA.
        return ""
    return pm_to_markdown(markdown_to_pm_or_empty(md))


def current_artifact_text_and_sha(
    session, *, workspace_id: uuid.UUID, artifact: str, repo_dir
) -> tuple[str, str]:
    """The artifact's CURRENT text and its content sha — one read, one basis.

    Collab ON  -> the room-canonical projection of the durable log (the text the
                  researcher is actually looking at).
    Collab OFF -> the raw HEAD bytes, byte-for-byte as before.

    Missing / empty -> ``("", "")``, preserving the convention that ``""`` marks
    "no artifact" on BOTH sides of every staleness comparison.

    Why one function and not two: the sha MUST be taken from the very text that was
    assembled. Hashing the file while sending the room's text reopens exactly the
    TOCTOU gap ``_read_artifact``'s docstring said it closed, and it also makes every
    staleness comparison permanently unequal — which would turn an unattended pass
    into an unattended spend loop. Every caller that assembles content and every
    caller that later re-checks it must go through here.
    """
    from call_manager.proposal_assist.collab.state import is_collab_enabled

    if is_collab_enabled(session, workspace_id=workspace_id, artifact=artifact):
        text = canonical_artifact_markdown(
            session, workspace_id=workspace_id, artifact=artifact, repo_dir=repo_dir
        )
    else:
        text = read_head_markdown(repo_dir, artifact) or ""
    if not text:
        return "", ""
    return text, hashlib.sha256(text.encode()).hexdigest()


def current_artifact_sha(session, *, workspace_id: uuid.UUID, artifact: str, repo_dir) -> str:
    """The sha half of :func:`current_artifact_text_and_sha`, for the re-check sites."""
    return current_artifact_text_and_sha(
        session, workspace_id=workspace_id, artifact=artifact, repo_dir=repo_dir
    )[1]
