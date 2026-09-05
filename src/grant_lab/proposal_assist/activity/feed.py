"""Pure, DB-free core for the Activity feed (SP2b-3e).

`build_activity_feed` turns a newest-first list of WorkspaceEvent rows into
curated ActivityRows: it drops system/gate/superseded kinds, collapses
consecutive same-actor edit runs, and renders each surviving group with a
human-readable summary and an optional cheap deep-link target. All domain
knowledge lives here; the web layer only queries + resolves actor names.
"""

from __future__ import annotations

from dataclasses import dataclass

from grant_lab.db.models import WorkspaceEvent

# Kinds that never produce a row: pure system reconciliation, the one-time LLM
# disclosure gate ack, and the pending half of a turn (superseded by its
# terminal cloud_turn_{completed,failed,cancelled} event, which carries the
# same actor_id).
_DROP: frozenset[str] = frozenset(
    {"git_drift_reconciled", "disclosure_acknowledged", "cloud_turn_sent"}
)


@dataclass(frozen=True)
class ActivityTarget:
    top_tab: str | None = None
    bottom_tab: str | None = None

    def as_dict(self) -> dict:
        return {"top_tab": self.top_tab, "bottom_tab": self.bottom_tab}


@dataclass(frozen=True)
class ActivityRow:
    id: str
    category: str
    actor: str | None
    summary: str
    count: int
    created_at: str
    target: ActivityTarget | None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "category": self.category,
            "actor": self.actor,
            "summary": self.summary,
            "count": self.count,
            "created_at": self.created_at,
            "target": self.target.as_dict() if self.target else None,
        }


# kind -> (category, summary, target). user_edit is handled separately (collapse).
# Targets reuse only existing TopTab/BottomTab values; reference.* get no target.
_DRAFT = ActivityTarget(top_tab="draft")
_SPEC = ActivityTarget(top_tab="spec")
_REVIEW = ActivityTarget(top_tab="review")
_PROBLEMS = ActivityTarget(bottom_tab="problems")

_RENDER: dict[str, tuple[str, str, ActivityTarget | None]] = {
    "created": ("created", "created the workspace", None),
    "user_citation_insert": ("citation", "added a citation", _DRAFT),
    "collab_checkpoint": ("edit", "saved a version", _DRAFT),
    "cloud_turn_completed": ("turn", "ran the assistant", None),
    "cloud_turn_failed": ("turn", "had an assistant turn fail", None),
    "cloud_turn_cancelled": ("turn", "cancelled an assistant turn", None),
    "review_run_triggered": ("review", "started a panel review", _REVIEW),
    "uncited_claims_run_triggered": ("uncited", "started an uncited-claims check", _PROBLEMS),
    "uncited_claim_dismissed": ("dismissal", "dismissed an uncited claim", _PROBLEMS),
    "user_claim_dismissed": ("dismissal", "dismissed a flagged claim", None),
    "reference.added": ("reference", "added a reference", None),
    "reference.metadata_approved": ("reference", "approved reference metadata", None),
    "reference_metadata_waived": ("reference", "waived reference metadata", None),
    "reference_metadata_waiver_cleared": ("reference", "cleared a reference metadata waiver", None),
    "reference.kind_changed": ("reference", "changed a reference type", None),
    "reference.reprocessed": ("reference", "reprocessed a reference", None),
    "reference.tombstoned": ("reference", "removed a reference", None),
    "reference.deleted": ("reference", "deleted a reference", None),
    "rubric_initialized": ("rubric", "set up the funder criteria", _SPEC),
    "rubric_edited": ("rubric", "edited the funder criteria", _SPEC),
    "workspace_activated": ("rubric", "activated the workspace", None),
    "member_added": ("member", "added collaborator", None),
    "member_removed": ("member", "removed collaborator", None),
}


def actor_user_id(event: WorkspaceEvent) -> int | None:
    """The user id to attribute an event to: actor_id when set, else a per-kind
    payload fallback. Only rubric_edited needs the fallback (it leaves actor_id
    null and stores the editor in payload['edited_by_user_id'])."""
    if event.actor_id is not None:
        return event.actor_id
    if event.kind == "rubric_edited":
        uid = (event.payload or {}).get("edited_by_user_id")
        if isinstance(uid, int):
            return uid
    return None


def _resolve_actor(event: WorkspaceEvent, actor_names: dict[int, str]) -> str | None:
    uid = actor_user_id(event)
    return actor_names.get(uid) if uid is not None else None


def _artifact_tab(artifact: object) -> str:
    # The payload "artifact" is always the StrEnum value "spec" or "draft" (Artifact
    # is a StrEnum; the spec-init site stores the literal "spec"). Exact-match "spec"
    # rather than substring so a future artifact name containing "spec" can't be
    # misclassified; anything else (incl. "draft", blank, unknown) maps to draft.
    return "spec" if str(artifact or "").strip().lower() == "spec" else "draft"


def _edit_row(run: list[WorkspaceEvent], actor_names: dict[int, str]) -> ActivityRow:
    """Render a maximal consecutive same-actor user_edit run (newest-first) as
    one row. Summary: a single distinct non-blank heading -> 'edited {heading}';
    else single artifact -> 'edited the {spec|draft}'; else 'made N edits'. '×N'
    is appended to the first two forms when N > 1."""
    count = len(run)
    most_recent = run[0]
    headings = [str((ev.payload or {}).get("heading") or "").strip() for ev in run]
    artifacts = {_artifact_tab((ev.payload or {}).get("artifact")) for ev in run}
    distinct = {h for h in headings if h}
    has_blank = any(not h for h in headings)

    if len(distinct) == 1 and not has_blank:
        base = f"edited {next(iter(distinct))}"
    elif len(artifacts) == 1:
        base = f"edited the {next(iter(artifacts))}"
    else:
        base = f"made {count} edits"

    summary = base if (count == 1 or base.startswith("made ")) else f"{base} ×{count}"
    top_tab = next(iter(artifacts)) if len(artifacts) == 1 else "draft"
    return ActivityRow(
        id=str(most_recent.id),
        category="edit",
        actor=_resolve_actor(most_recent, actor_names),
        summary=summary,
        count=count,
        created_at=most_recent.created_at.isoformat(),
        target=ActivityTarget(top_tab=top_tab),
    )


def build_activity_feed(
    events: list[WorkspaceEvent], actor_names: dict[int, str]
) -> list[ActivityRow]:
    """Curate a newest-first WorkspaceEvent list into ActivityRows. Drops
    excluded/unmapped kinds, collapses consecutive same-actor edit runs, and
    renders every other kind 1:1 via the _RENDER table. Order is preserved."""
    rows: list[ActivityRow] = []
    i = 0
    n = len(events)
    while i < n:
        e = events[i]
        if e.kind in _DROP:
            i += 1
            continue
        if e.kind == "user_edit":
            actor = actor_user_id(e)
            j = i
            run: list[WorkspaceEvent] = []
            while j < n and events[j].kind == "user_edit" and actor_user_id(events[j]) == actor:
                run.append(events[j])
                j += 1
            rows.append(_edit_row(run, actor_names))
            i = j
            continue
        spec = _RENDER.get(e.kind)
        if spec is None:
            i += 1  # unknown/unmapped kind -> drop (fail-safe)
            continue
        category, summary, target = spec
        if e.kind in ("member_added", "member_removed"):
            email = (e.payload or {}).get("target_email")
            if email:
                summary = f"{summary} {email}"
        rows.append(
            ActivityRow(
                id=str(e.id),
                category=category,
                actor=_resolve_actor(e, actor_names),
                summary=summary,
                count=1,
                created_at=e.created_at.isoformat(),
                target=target,
            )
        )
        i += 1
    return rows
