"""Live handshake de-risk driver (Slice 3b-3, Task 1). GO/NO-GO gate.

Boots real Postgres + uvicorn, seeds a collab-enabled draft, runs the Node/ws
probe against the real server, exits with the probe's code. Run:

    EMBEDDING_MODEL=bge-m3 python scripts/collab_handshake_probe.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

# Add the repo root so `tests.*` modules are importable outside pytest.
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))


def main() -> int:
    os.environ.setdefault("EMBEDDING_MODEL", "bge-m3")
    # Required env that create_app/Settings validates:
    os.environ.setdefault("SESSION_SECRET", "probe-secret-at-least-16-chars!")
    os.environ.setdefault("ALLOWED_EMAIL_DOMAINS", "umu.se")
    os.environ["SPA_MODE"] = "disabled"
    os.environ["APP_BASE_URL"] = "http://127.0.0.1:8199"

    import uvicorn
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16", driver="psycopg") as pg:
        db_url = pg.get_connection_url()  # postgresql+psycopg://...
        workspaces = Path(tempfile.mkdtemp())
        os.environ["DATABASE_URL"] = db_url
        os.environ["WORKSPACES_DIR"] = str(workspaces)

        # --- migrate ---
        from alembic.config import Config

        from alembic import command
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", db_url)
        command.upgrade(cfg, "head")

        # --- build app + seed ---
        from call_manager.main import create_app
        app = create_app()

        # Override the session_factory so the app uses this testcontainers DB
        from call_manager.db.session import make_engine, make_session_factory
        engine = make_engine(db_url)
        Session = make_session_factory(engine)
        app.state.session_factory = Session

        from call_manager.proposal_assist.collab import state as collab_state
        from tests.proposal_assist.conftest import make_user, make_workspace
        from tests.test_editing_save import _init_repo

        with Session() as s:
            owner = make_user(s, email=f"probe-{uuid.uuid4().hex[:8]}@umu.se")
            ws = make_workspace(s, owner=owner, current_phase="active")
            collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
            s.commit()
            wid, oid = ws.id, owner.id

        _init_repo(workspaces, wid, doc="Preamble.\n\n## A\n\nBody a.\n")
        cookie = "session=" + app.state.session_signer.make(user_id=oid)

        # --- uvicorn in a thread ---
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=8199, log_level="warning")
        )
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.1)

        env = {**os.environ,
               "PROBE_WS_URL": f"ws://127.0.0.1:8199/api/ws/workspaces/{wid}/draft",
               "PROBE_COOKIE": cookie,
               "PROBE_ORIGIN": "http://127.0.0.1:8199"}
        rc = subprocess.call(
            ["pnpm", "exec", "vitest", "run",
             "--config", "vitest.probe.config.ts",
             "src/collab/handshake.probe.test.ts"],
            cwd="frontend", env=env)
        server.should_exit = True
        print("HANDSHAKE_PROBE:", "GO" if rc == 0 else "NO-GO")
        return rc


if __name__ == "__main__":
    sys.exit(main())
