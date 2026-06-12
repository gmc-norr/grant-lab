from call_manager.web.filters import phase_label


def test_phase_label_known_phases():
    assert phase_label("setup") == "Setting up"
    assert phase_label("spec") == "Drafting"
    assert phase_label("plan") == "Planning"
    assert phase_label("draft") == "Revising"
    assert phase_label("review") == "Review"


def test_phase_label_unknown_falls_back_to_raw():
    # Defensive: a future phase value (uninsertable today via the DB CHECK)
    # renders as-is rather than blanking the chip.
    assert phase_label("negotiation") == "negotiation"


def test_phase_label_none():
    assert phase_label(None) == "—"
