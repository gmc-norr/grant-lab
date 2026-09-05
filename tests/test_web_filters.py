from grant_lab.web.filters import phase_label


def test_phase_label_two_mode():
    assert phase_label("setup") == "Setting up"
    assert phase_label("active") == "Active"


def test_phase_label_unknown_passthrough():
    # Unknown/future phase values render as-is rather than blanking the chip.
    assert phase_label("future_mode") == "future_mode"
    assert phase_label(None) == "—"
    assert phase_label("") == "—"
