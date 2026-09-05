"""The ONE definition of who may READ a profile.

Every surface that shows, lists, ranks or digests a profile composes the
subquery below. Nothing anywhere else may compare a profile's `visibility` --
the class attribute `Profile.visibility` or a loaded row's `p.visibility` alike
-- or build a select over `ProfileViewer` to decide access -- that rule is enforced by
tests/architecture/test_profile_access_single_source.py, which also pins the
list of functions that must call in here.

The gate exists because the failure mode is silent and partial: eleven of the
twelve surfaces get the rule right, the twelfth keeps its own existence-only
lookup, and a private profile leaks through exactly one route. The first draft
of this plan listed seven surfaces; outside review found three more, the
controller found an eleventh (calls.py's ?profile= resolution), and the final
whole-branch review found a twelfth in a route that never mentions profiles in
its name -- web/api/workspaces.py's create_workspace_api, which validated its
optional profile_id by EXISTENCE (PD-9).

Shape copied deliberately from web/api/workspaces.py:1471-1480 -- same composite
viewer table, same or_() over owner/viewer. Spec 4.3 requires that no second
permission model appears in this codebase, and boards are built on this rule.
"""

from __future__ import annotations

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from grant_lab.db.models import Profile, ProfileViewer


def readable_profile_ids(user) -> Select:
    """Subquery of profile ids `user` may READ.

    Four arms, in the order they are cheap to evaluate:
      created_by == user.id   -- the owner. NEVER has a profile_viewers row.
      user.is_admin           -- same override profiles.py:342 uses for editing.
      visibility == 'org'     -- the default, so this is most profiles today.
      profile_viewers         -- explicitly shared.

    READING IS WIDER THAN EDITING, always. Editing stays created_by-or-admin
    (profiles.py:342-343, spec 4.3). Do not collapse the two: a colleague a
    profile is shared with must be able to read it and must not be able to
    change it.

    `user` is anything carrying `.id` and `.is_admin` -- the User the auth
    dependency hands a route, or one a background caller loaded with
    `session.get(User, user_id)`.

    Returns a Select of ids, NOT a list. Every caller composes it into a bigger
    query (`Profile.id.in_(readable_profile_ids(user))`); materialising it here
    would make the dashboard's facet query O(profiles) round trips.

    Deliberately does NOT filter is_active. Callers already do, and the two
    concerns are orthogonal: archiving a profile changes whether it is OFFERED,
    not who is ALLOWED to see it. The admin and archive surfaces want inactive
    rows, and folding the filter in here would push them around the single
    source.
    """
    if user.is_admin:
        # Admin arm. Short-circuited rather than OR'd in so the query planner
        # sees an unconditional select for the one caller class that reads
        # everything; the result set is identical either way.
        return select(Profile.id)
    shared_ids = select(ProfileViewer.profile_id).where(ProfileViewer.user_id == user.id)
    return select(Profile.id).where(
        or_(
            Profile.created_by == user.id,
            Profile.visibility == "org",
            Profile.id.in_(shared_ids),
        )
    )


def can_read_profile(session: Session, user, profile_id: int) -> bool:
    """Whether `user` may read one profile, by id.

    Implemented BY the subquery above rather than beside it -- a second
    hand-written arm list is exactly the drift this module exists to prevent,
    and the architecture gate asserts this body contains that call.

    False for a profile id that does not exist, so a caller can use this as its
    404 check without a second lookup.
    """
    return (
        session.scalar(
            select(Profile.id).where(
                Profile.id == profile_id,
                Profile.id.in_(readable_profile_ids(user)),
            )
        )
        is not None
    )
