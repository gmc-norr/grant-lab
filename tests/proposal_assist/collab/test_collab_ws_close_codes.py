"""WebSocket close codes distinguish terminal policy, transient contention,
and a document identity change that requires rebuilding the client Y.Doc."""

import inspect

from call_manager.proposal_assist.web import collab_ws


def _arm(src: str, header: str) -> str:
    """The body of one `except` arm, or "" if that arm does not exist.

    Returns "" rather than raising so a missing arm fails the ASSERTION below
    with a readable message, instead of blowing up on an IndexError that says
    nothing about what is wrong."""
    if header not in src:
        return ""
    return src.split(header, 1)[1].split("    except ", 1)[0]


def test_generation_stale_is_not_grouped_with_retired_and_deleted():
    # The three exceptions had ONE handler; a switch needs the stale arm to close
    # with document-replaced while the other two keep 1008.
    src = inspect.getsource(collab_ws)
    assert "except WorkspaceGenerationStale:" in src, (
        "WorkspaceGenerationStale must have its own document-replaced arm"
    )


def test_the_stale_arm_closes_with_document_replaced():
    arm = _arm(inspect.getsource(collab_ws), "except WorkspaceGenerationStale:")
    assert "WS_DOCUMENT_REPLACED" in arm, "the stale arm must discard the old Y.Doc"
    assert "_WS_TRY_AGAIN_LATER" not in arm, "1013 would reconnect the retained Y.Doc"
    assert "_WS_POLICY_VIOLATION" not in arm, "1008 is terminal on the client"


def test_retired_and_deleted_still_close_with_policy_violation():
    arm = _arm(inspect.getsource(collab_ws), "except (WorkspaceRetired, WorkspaceDeleted):")
    assert "_WS_POLICY_VIOLATION" in arm, "a deleted workspace must stay terminal"


def test_the_codes_are_the_rfc_values():
    assert collab_ws.WS_DOCUMENT_REPLACED == 4409
    assert collab_ws._WS_TRY_AGAIN_LATER == 1013
    assert collab_ws._WS_POLICY_VIOLATION == 1008
