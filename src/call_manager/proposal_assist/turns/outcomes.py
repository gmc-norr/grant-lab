"""Centralized terminal-outcome recording for proposal-assist turns.

Every terminal failed/cancelled transition routes through
``record_terminal_outcome`` so the status flip, the viewer-safe ``system``
chat row, and the audit event stay consistent across all five writers
(processor finalize, reaper, orphan sweep, cancel route, soft-delete).
"""

from __future__ import annotations


def outcome_message(outcome: str, raw_error: str | None) -> str:
    """Map an outcome + raw error to a short, viewer-safe sentence.

    NEVER echoes ``raw_error`` (it can contain stack/path diagnostics).
    Phrased neutrally: the same row is shown to viewers who cannot act,
    and 'orphaned' can follow either a crash or a redeploy.
    """
    if outcome == "cancelled":
        return "This turn was cancelled before it started."
    if raw_error is not None and raw_error.startswith("reaped:"):
        return "This turn was stopped after running too long."
    if raw_error == "orphaned":
        return "This turn was interrupted. Please try again."
    return "The drafting assistant couldn't finish this turn. Please try again."
