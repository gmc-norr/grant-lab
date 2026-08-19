"""Tool-facing capability contracts — stdlib only.

Spec: docs/superpowers/specs/2026-08-18-fence-session-ownership-design.md §3.

Tools receive capabilities, never handles. NOTHING in this package may import
sqlalchemy, call_manager.db.models, or any project module that imports either:
an attached ORM instance carries object_session(instance), which is the full
Session again — the leak that defeated the forwarding-proxy design. The
implementations live in call_manager.proposal_assist.port_impl, which tool code
may not import (the gate lands in F3c).
"""
