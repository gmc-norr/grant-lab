"""The suite must never write into the repo's live `data/` directory.

`ranking/eval.py` computes `_REPORT_PATH` and `_HISTORY_PATH` at import, both
under `CALL_MANAGER_DATA_DIR` (default `data/`). Individual modules used to
redirect `_REPORT_PATH` and forget `_HISTORY_PATH`, so a full run appended six
rows to the human's real `data/golden_set_history.jsonl` — measured, not
assumed. The redirect now lives in `conftest.py` and covers both; this pins it.
"""

from pathlib import Path

from grant_lab.ranking import eval as eval_mod

_REPO_DATA = (Path(__file__).resolve().parents[1] / "data").resolve()


def test_the_golden_set_paths_never_point_into_the_live_data_dir():
    for name in ("_REPORT_PATH", "_HISTORY_PATH"):
        resolved = getattr(eval_mod, name).resolve()
        assert _REPO_DATA not in resolved.parents, (
            f"{name} resolves to {resolved}, inside the live data dir {_REPO_DATA}. "
            "A test run would append to the human's real golden-set files."
        )
