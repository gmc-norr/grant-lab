from grant_lab.profiles.seed import SeedInput


def test_seedinput_carries_level_and_mission():
    s = SeedInput(
        name="Path Lab",
        kind="organizational_unit",
        level="lab",
        mission_text="We run a biobank and digital pathology pipeline.",
    )
    assert s.level == "lab"
    assert "biobank" in s.mission_text
    assert s.members == [] and s.pasted_grants == ""
