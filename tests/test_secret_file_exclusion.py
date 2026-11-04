"""Dotenv files stay out of the Docker build context and out of git, at ANY depth.

Security review 2026-10-31 S5: .dockerignore listed `.env*` and `*.env`, which
Docker matches at the context ROOT only. `frontend/Dockerfile` copies
`frontend/` whole, so `frontend/.env`, `frontend/.env.production` and any
other nested dotenv file entered the builder image's layers -- extracting only
the bundle afterwards does not remove them from the image or its cache.

The Docker check builds a real (FROM scratch, nothing pulled) image from a
canary context that carries the repo's own .dockerignore, and exports what the
build actually saw. A pattern-matching re-implementation in Python would only
test our reading of Docker's rules.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SECRET_FILES = [
    ".env",
    ".env.production",
    "frontend/.env",
    "frontend/.env.production",
    "frontend/.env.local",
    "frontend/.env.bak-20261031",
    "frontend/src/.env",
    "frontend/app.env",
    "frontend/api_key.txt",
]
# Files the SPA build needs: excluding these would "fix" the leak by breaking
# the build.
NEEDED_FILES = ["frontend/package.json", "frontend/src/main.tsx", "frontend/.npmrc"]


def _docker_usable() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "info"], capture_output=True, timeout=30)
    return r.returncode == 0


@pytest.mark.skipif(not _docker_usable(), reason="needs a Docker daemon")
def test_no_dotenv_file_enters_the_docker_build_context(tmp_path):
    ctx = tmp_path / "ctx"
    for rel in SECRET_FILES + NEEDED_FILES:
        p = ctx / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("CANARY=not-a-real-secret\n")
    shutil.copy(ROOT / ".dockerignore", ctx / ".dockerignore")
    dockerfile = tmp_path / "Dockerfile.canary"  # outside the context
    dockerfile.write_text("FROM scratch\nCOPY . /ctx/\n")
    out = tmp_path / "out"

    r = subprocess.run(
        [
            "docker",
            "build",
            "-q",
            "-f",
            str(dockerfile),
            "--output",
            f"type=local,dest={out}",
            str(ctx),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr

    seen = {str(p.relative_to(out / "ctx")) for p in (out / "ctx").rglob("*") if p.is_file()}
    assert seen, "the canary build exported nothing; the check would pass vacuously"
    leaked = seen & set(SECRET_FILES)
    assert not leaked, f"secret files entered the build: {leaked}"
    assert set(NEEDED_FILES) <= seen, f"needed files were excluded: {set(NEEDED_FILES) - seen}"


@pytest.mark.parametrize("rel", SECRET_FILES)
def test_git_ignores_every_dotenv_variant(rel):
    r = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", rel], cwd=ROOT, capture_output=True
    )
    assert r.returncode == 0, f"git would let {rel} be committed"


@pytest.mark.parametrize("rel", [".env.example"])
def test_git_still_tracks_the_examples(rel):
    r = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", rel], cwd=ROOT, capture_output=True
    )
    assert r.returncode == 1, f"{rel} would be ignored; the example would stop being shipped"
