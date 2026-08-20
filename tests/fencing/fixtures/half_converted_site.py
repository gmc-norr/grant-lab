"""NEGATIVE fixture: half-converted, which is not converted.

One qualname holding BOTH a port write and a residual raw commit. No site in the
tree has this shape after F3d2 — it is the shape a PARTIAL conversion produces, and
the rule exists so that a partial conversion cannot be reported as a finished one.
A rule that only looked for the port call would mark such a qualname fenced and let
its slice declare itself complete over code that still commits directly.
"""


async def fetch_reference_impl(ctx, *, ports, invocation):
    ports.accounting.reserve_fetch_slot(invocation.active_turn_job_id)
    with ctx.session_factory() as s:
        s.commit()
    return {"ok": True}
