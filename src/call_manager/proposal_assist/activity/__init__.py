"""SP2b-3e Activity feed: curated, read-only render of recent WorkspaceEvents."""

from call_manager.proposal_assist.activity.feed import (
    ActivityRow,
    ActivityTarget,
    actor_user_id,
    build_activity_feed,
)

__all__ = ["ActivityRow", "ActivityTarget", "actor_user_id", "build_activity_feed"]
