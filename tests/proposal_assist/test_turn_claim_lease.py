"""claim_turn_job must hold the shared lease, so a branch switch's exclusive
mutex linearizes against it rather than racing it (spec §6.3)."""

import inspect

from grant_lab.proposal_assist.turns import processor


def test_claim_turn_job_takes_the_shared_lease():
    src = inspect.getsource(processor.claim_turn_job)
    assert "acquire_shared_lease" in src, (
        "without the lease the switch's running-turn refusal is probabilistic"
    )


def test_the_lease_is_taken_BEFORE_the_claim_update():
    src = inspect.getsource(processor.claim_turn_job)
    assert src.index("acquire_shared_lease") < src.index("update(TurnJob)"), (
        "a lease taken after the write guards nothing"
    )


def test_claim_turn_job_never_upgrades_to_the_exclusive_mutex():
    # The one deadlock shape this protocol has: two holders of shared, each
    # waiting to upgrade. fencing.acquire_shared_lease's docstring forbids it.
    #
    # inspect.getsource() INCLUDES comments, so the implementation's comment must
    # not name the forbidden function literally — see the note at that comment.
    src = inspect.getsource(processor.claim_turn_job)
    assert "acquire_commit_mutex" not in src
