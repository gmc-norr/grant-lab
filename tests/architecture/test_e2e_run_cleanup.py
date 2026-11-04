"""scripts/e2e_run.sh must not kill a server it never started.

The cleanup trap frees ports 5173 and 8000 by SIGKILLing whoever listens on
them — a backstop for Vite's esbuild child, which escapes the process-group
kill. But the trap was installed BEFORE the preflight that refuses to run when
those ports are already busy, so `exit 1` from the preflight ran cleanup, and
the script SIGKILLed the very dev server it had just announced it would not
disturb. Reproduced 2026-09-08 against a real python http.server on :8000.

The gate fakes `ss` rather than binding a real port: a test that grabs :8000 on
a developer box would kill their dev server to prove the script no longer does.
The victim is a `sleep` the test owns; the fake `ss` merely claims that pid
holds the port.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "e2e_run.sh"

# The `ss` the script would call. Reports a LISTEN on VICTIM_PORT owned by
# VICTIM_PID, in the two shapes the script parses: `ss -ltn` (preflight, greps
# for LISTEN) and `ss -ltnp` (cleanup, greps for pid=N).
FAKE_SS = """#!/usr/bin/env bash
for a in "$@"; do
  case "$a" in
    *":$VICTIM_PORT")
      echo "LISTEN 0 5 127.0.0.1:$VICTIM_PORT 0.0.0.0:* users:((\\"sleep\\",pid=$VICTIM_PID,fd=3))"
      exit 0
      ;;
  esac
done
exit 0
"""


def _fake_bin(tmp_path: Path) -> Path:
    d = tmp_path / "bin"
    d.mkdir()
    ss = d / "ss"
    ss.write_text(FAKE_SS)
    ss.chmod(0o755)
    return d


def test_a_refused_preflight_does_not_kill_the_server_it_refused_to_disturb(tmp_path):
    victim = subprocess.Popen(["sleep", "120"])
    try:
        env = {
            "PATH": f"{_fake_bin(tmp_path)}:/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "VICTIM_PORT": "8000",
            "VICTIM_PID": str(victim.pid),
        }
        proc = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, check=False
        )
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "already in use" in proc.stdout

        # SIGKILL is not instant from the caller's point of view.
        for _ in range(20):
            if victim.poll() is not None:
                break
            time.sleep(0.1)
        assert victim.poll() is None, (
            "the script killed the pre-existing server on :8000 after refusing to run alongside it"
        )
    finally:
        victim.kill()
        victim.wait()
