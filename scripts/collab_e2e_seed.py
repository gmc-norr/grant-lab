"""E2E seed helper for the collab Playwright round-trip (Slice 3b-3, Task 6).

Boots a testcontainers Postgres, runs Alembic migrations, seeds an ACTIVE
workspace with collab enabled on the draft artifact, starts uvicorn in
SPA_MODE=dev (APP_BASE_URL=http://localhost:5173 so the CSRF Origin check
passes against the Vite proxy origin), then prints the env vars needed to
run `pnpm exec playwright test e2e/collab.spec.ts`:

    E2E_COLLAB_WS_ID=<workspace-uuid>
    E2E_SESSION_COOKIE=<signed-cookie-value>

Mirrors scripts/collab_handshake_probe.py — reuses the same seed machinery
(make_user / make_workspace / collab_state.enable_collab / _init_repo /
app.state.session_signer.make). The server stays running until Ctrl-C or
the caller terminates the process.

Usage:
    source .venv/bin/activate
    EMBEDDING_MODEL=bge-m3 python scripts/collab_e2e_seed.py

Then in a second terminal:
    cd frontend
    pnpm dev  # Vite dev server on :5173

Then run the e2e suite:
    cd frontend
    E2E_COLLAB_WS_ID=<from above> E2E_SESSION_COOKIE=<from above> \\
        pnpm exec playwright test e2e/collab.spec.ts
"""
from __future__ import annotations

import os
import signal
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

# Add the repo root so `tests.*` and `call_manager.*` are importable.
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))


def main() -> int:
    os.environ.setdefault("EMBEDDING_MODEL", "bge-m3")
    # Minimal env that create_app/Settings validates.
    os.environ.setdefault("SESSION_SECRET", "e2e-seed-secret-at-least-16-chars!")
    os.environ.setdefault("ALLOWED_EMAIL_DOMAINS", "ki.se")
    # SPA_MODE=dev is required: it makes the dev CSP emit
    # "connect-src 'self' ws: http://localhost:5173" so the Yjs WebSocket is
    # allowed by the browser.  Production/disabled CSP is strict (connect-src
    # 'self' only) — do NOT use "prod" or "disabled" here.
    os.environ["SPA_MODE"] = "dev"
    # APP_BASE_URL must match the origin the browser POSTs from.  In dev the
    # browser talks to the Vite proxy on :5173, not uvicorn on :8000.
    os.environ["APP_BASE_URL"] = "http://localhost:5173"

    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]
    import uvicorn
    from sqlalchemy import create_engine  # noqa: F401 (imported for clarity)

    with PostgresContainer("pgvector/pgvector:pg16", driver="psycopg") as pg:
        db_url = pg.get_connection_url()  # postgresql+psycopg://...
        workspaces = Path(tempfile.mkdtemp())
        os.environ["DATABASE_URL"] = db_url
        os.environ["WORKSPACES_DIR"] = str(workspaces)

        # --- migrate ---
        from alembic import command
        from alembic.config import Config

        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", db_url)
        command.upgrade(cfg, "head")

        # --- build app ---
        from call_manager.main import create_app

        app = create_app()

        # Override session_factory so the app uses the testcontainers DB.
        from call_manager.db.session import make_engine, make_session_factory

        engine = make_engine(db_url)
        Session = make_session_factory(engine)
        app.state.session_factory = Session

        # --- seed ---
        from call_manager.proposal_assist.collab import state as collab_state
        from tests.proposal_assist.conftest import make_user, make_workspace
        from tests.test_editing_save import _init_repo

        with Session() as s:
            owner = make_user(s, email=f"e2e-collab-{uuid.uuid4().hex[:8]}@ki.se")
            ws = make_workspace(s, owner=owner, current_phase="active")
            collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
            s.commit()
            wid, oid = ws.id, owner.id

        _init_repo(workspaces, wid, doc="Preamble.\n\n## A\n\nBody a.\n")

        # Mint a signed session cookie (no "session=" prefix — that's added by
        # the browser/Playwright context).
        cookie_value = app.state.session_signer.make(user_id=oid)

        # --- start uvicorn on :8000 (the Vite proxy target) ---
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="warning")
        )
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.1)

        print()
        print("=== collab e2e seed ready ===")
        print()
        print(f"E2E_COLLAB_WS_ID={wid}")
        print(f"E2E_SESSION_COOKIE={cookie_value}")
        print()
        print("Now start Vite in another terminal:")
        print("  cd frontend && pnpm dev")
        print()
        print("Then run the e2e suite:")
        print(
            f"  cd frontend && E2E_COLLAB_WS_ID={wid} E2E_SESSION_COOKIE={cookie_value} "
            "pnpm exec playwright test e2e/collab.spec.ts"
        )
        print()
        print("Press Ctrl-C to stop uvicorn (testcontainers Postgres also stops).")
        print()

        # Block until interrupted.
        try:
            signal.pause()  # Linux only; blocks until any signal
        except (AttributeError, KeyboardInterrupt):
            pass

        server.should_exit = True
        return 0


if __name__ == "__main__":
    sys.exit(main())
