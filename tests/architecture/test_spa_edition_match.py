"""The served SPA bundle and APP_EDITION must agree, or the app refuses to start.

scripts/package_spa.sh records which edition the BUNDLE is (read from
dist/module-manifest.json, not from the caller's environment) in
src/call_manager/web/spa/edition.txt — and until this gate nothing read it. The
failure that leaves open is silent in every direction that matters:
APP_EDITION=match served alongside a full SPA gives the full router, with cockpit
links, the full edition's dark theme, and every proposal-assist request 404ing,
while every other gate on this branch stays green (they all inspect the match
BUILD, never the pairing at deploy time).

ABSENCE IS NOT DISAGREEMENT. The CI/Docker path (.github/workflows/ci.yml:132-137)
`docker cp`s frontend/dist into the package tree and never calls package_spa.sh,
so a wheel built there carries no edition.txt at all. Refusing on a missing file
would break the existing deployment, so missing means unknown: proceed.

DB-free: create_app() builds the engine lazily, so a bogus DATABASE_URL is enough
(same technique as test_edition_surface.py).
"""

from __future__ import annotations

import pytest

import call_manager.web.spa_serving as spa_serving
from call_manager.main import create_app


@pytest.fixture
def _base_env(monkeypatch):
    for k, v in {
        "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5999/nope",
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://testserver",
        "SPA_MODE": "prod",
        # APP_EDITION=match refuses to start with drafting enabled (config.py
        # _match_edition_disables_drafting), and it defaults to True.
        "DRAFTING_ENABLED": "false",
    }.items():
        monkeypatch.setenv(k, v)


def _bundle(monkeypatch, tmp_path, *, edition: str | None, marker: str | None = None):
    """A minimal served tree. `marker` overrides the file's whole content."""
    spa_dir = tmp_path / "spa"
    (spa_dir / "assets").mkdir(parents=True)
    (spa_dir / "index.html").write_text("<!doctype html><title>spa</title>")
    if marker is not None:
        (spa_dir / "edition.txt").write_text(marker)
    elif edition is not None:
        (spa_dir / "edition.txt").write_text(
            f"edition={edition}\npackaged=2026-09-04T00:00:00+02:00\n"
        )
    monkeypatch.setattr(spa_serving, "SPA_DIR", spa_dir)
    return spa_dir


@pytest.mark.parametrize("edition", ["match", "full"])
def test_agreeing_editions_start(monkeypatch, _base_env, tmp_path, edition):
    """Both directions: the gate must not refuse a correct pairing."""
    _bundle(monkeypatch, tmp_path, edition=edition)
    monkeypatch.setenv("APP_EDITION", edition)
    create_app()  # must not raise


@pytest.mark.parametrize(
    ("packaged", "configured"),
    [("full", "match"), ("match", "full")],
)
def test_disagreeing_editions_refuse_to_start(
    monkeypatch, _base_env, tmp_path, packaged, configured
):
    """Both directions again. full-bundle/match-backend is the one the CI path
    actually produces; match-bundle/full-backend is the mirror (a Call Match
    shell over a backend that still serves the cockpit)."""
    _bundle(monkeypatch, tmp_path, edition=packaged)
    monkeypatch.setenv("APP_EDITION", configured)
    with pytest.raises(RuntimeError) as exc:
        create_app()
    msg = str(exc.value)
    # The message must name BOTH values — an operator reading only "edition
    # mismatch" cannot tell which half to change.
    assert "edition mismatch" in msg.lower(), msg
    assert repr(packaged) in msg, msg
    assert repr(configured) in msg, msg


def test_absent_edition_file_starts(monkeypatch, _base_env, tmp_path):
    """The CI/Docker bundle carries no edition.txt. Unknown, not wrong."""
    _bundle(monkeypatch, tmp_path, edition=None)
    monkeypatch.setenv("APP_EDITION", "match")
    create_app()  # must not raise


def test_unparsable_edition_file_starts(monkeypatch, _base_env, tmp_path):
    """A marker with no `edition=` line is unknown too — the same tolerance the
    absent case gets, so a format change cannot turn into a boot failure."""
    _bundle(monkeypatch, tmp_path, edition=None, marker="packaged=2026-09-04T00:00:00+02:00\n")
    monkeypatch.setenv("APP_EDITION", "match")
    create_app()  # must not raise


def test_the_gate_is_inert_outside_prod(monkeypatch, _base_env, tmp_path):
    """SPA_MODE=dev/disabled serve no bundle, so a stale marker in a developer's
    tree must not stop the app booting. (This also keeps every other test in the
    suite — all of which run SPA_MODE=disabled against the REAL src/.../spa,
    which currently holds a match bundle — independent of APP_EDITION.)"""
    _bundle(monkeypatch, tmp_path, edition="full")
    monkeypatch.setenv("APP_EDITION", "match")
    for mode in ("dev", "disabled"):
        monkeypatch.setenv("SPA_MODE", mode)
        create_app()  # must not raise


def test_the_marker_the_packaging_script_writes_is_the_one_this_reads(monkeypatch, tmp_path):
    """Ties the reader to the WRITER's format rather than to a copy of it.

    packaged_edition() would happily parse a format package_spa.sh never emits.
    This asserts on the exact printf template in the script's source, so changing
    either side alone goes red.
    """
    from pathlib import Path

    script = (Path(__file__).resolve().parents[2] / "scripts" / "package_spa.sh").read_text()
    assert "printf 'edition=%s\\npackaged=%s\\n'" in script, (
        "package_spa.sh no longer writes `edition=<value>` as the first line of "
        "edition.txt; spa_serving.packaged_edition() parses that shape"
    )
    spa_dir = tmp_path / "spa"
    spa_dir.mkdir()
    (spa_dir / "edition.txt").write_text("edition=match\npackaged=2026-09-04T00:00:00+02:00\n")
    monkeypatch.setattr(spa_serving, "SPA_DIR", spa_dir)
    assert spa_serving.packaged_edition() == "match"
