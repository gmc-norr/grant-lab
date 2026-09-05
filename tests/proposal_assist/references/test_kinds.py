from grant_lab.proposal_assist.references.kinds import ALLOWED_KINDS


def test_allowed_kinds_exact_set():
    assert frozenset({"paper", "prior_proposal", "analysis", "data"}) == ALLOWED_KINDS
