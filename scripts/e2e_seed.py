"""Seed and serve the backend for the real-backend browser smoke.

Boots a testcontainers Postgres, runs the Alembic migrations, seeds the rows
frontend/e2e/smoke.spec.ts needs, starts uvicorn on :8000, and prints the E2E_*
lines scripts/e2e_run.sh waits for. The server stays up until Ctrl-C.

SPA_MODE follows E2E_SPA_MODE. "dev" (default): the browser talks to the Vite
proxy on :5173, so APP_BASE_URL is that origin and the CSRF Origin check passes.
"prod": FastAPI itself serves the packaged bundle on :8000 under the strict CSP;
scripts/e2e_run.sh builds and packages it first.

What it seeds, and why each row exists:
  * an ACTIVE password user, e2e@umu.se / correct-horse-1. The spec's login()
    helper hard-codes both values.
  * a profile that user owns, stored as their default profile. Without a
    resolved profile, / renders the "No research profile yet." screen
    (TriageEmptyProfile) and there is no call card to open.
  * one open call with no deadline and no decision. The inbox lists every open,
    undecided call for the profile whether or not it has been ranked, so this is
    the card the spec opens.

Usage (scripts/e2e_run.sh does all of this):
    EMBEDDING_MODEL=bge-m3 uv run python -u scripts/e2e_seed.py
then, in dev mode, `cd frontend && pnpm dev` in a second terminal, then
    cd frontend && E2E_MODE=dev pnpm exec playwright test e2e/smoke.spec.ts
"""

from __future__ import annotations

import os
import secrets
import sys
import threading
import time
import uuid
from pathlib import Path

# The source tree, so `grant_lab` imports without relying on the editable install.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Fixed, because frontend/e2e/smoke.spec.ts hard-codes all three. Change both or neither.
E2E_PASSWORD_EMAIL = "e2e@umu.se"
E2E_PASSWORD = "correct-horse-1"
E2E_CALL_TITLE = "Precision medicine programme"


def main() -> int:
    os.environ.setdefault("EMBEDDING_MODEL", "bge-m3")
    # Minimal env that create_app/Settings validates.
    # A fresh random key per run, not a fixed one: E2E_SPA_MODE=prod is a
    # production configuration as far as Settings.is_production is concerned,
    # and production refuses every key this repository has ever written down.
    os.environ.setdefault("SESSION_SECRET", secrets.token_urlsafe(48))
    os.environ.setdefault("ALLOWED_EMAIL_DOMAINS", "umu.se")
    os.environ["SPA_MODE"] = os.environ.get("E2E_SPA_MODE", "dev")
    # APP_BASE_URL must match the origin the browser POSTs from (CSRF Origin check
    # and the CSP): the Vite proxy on :5173 in dev, FastAPI itself on :8000 in prod.
    os.environ["APP_BASE_URL"] = (
        "http://localhost:8000" if os.environ["SPA_MODE"] == "prod" else "http://localhost:5173"
    )

    import uvicorn
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    with PostgresContainer("pgvector/pgvector:pg16", driver="psycopg") as pg:
        db_url = pg.get_connection_url()  # postgresql+psycopg://...
        os.environ["DATABASE_URL"] = db_url

        # --- migrate ---
        from alembic.config import Config

        from alembic import command

        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", db_url)
        command.upgrade(cfg, "head")

        # --- build app ---
        from grant_lab.main import create_app

        app = create_app()

        # Override session_factory so the app uses the testcontainers DB.
        from grant_lab.db.session import make_engine, make_session_factory

        Session = make_session_factory(make_engine(db_url))
        app.state.session_factory = Session

        # --- seed ---
        from grant_lab.auth.passwords import hash_password
        from grant_lab.db.models import Call, Profile, Source, User

        suffix = uuid.uuid4().hex[:8]
        with Session() as s:
            user = User(
                email=E2E_PASSWORD_EMAIL,
                status="active",
                password_hash=hash_password(E2E_PASSWORD),
            )
            s.add(user)
            s.flush()
            profile = Profile(
                name="E2E research group", kind="organizational_unit", created_by=user.id
            )
            s.add(profile)
            s.flush()
            user.default_profile_id = profile.id
            src = s.query(Source).filter_by(slug="vinnova").one_or_none()
            if src is None:
                src = Source(slug="vinnova", name="Vinnova")
                s.add(src)
                s.flush()
            call = Call(
                source_id=src.id,
                external_id=f"E2E-{suffix}",
                title=E2E_CALL_TITLE,
                url=f"https://example.org/calls/E2E-{suffix}",
                summary="Funding for AI-based diagnostics.",
                full_text=(
                    "Body about eligibility, scope, budget, expected "
                    "outcomes, restrictions, mandatory."
                ),
            )
            s.add(call)
            s.commit()
            profile_id, call_id = profile.id, call.id

        # --- start uvicorn on :8000 (the Vite proxy target in dev) ---
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="warning")
        )
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.1)
        if not server.started:
            # uvicorn never bound (e.g. :8000 already in use by an orphaned run).
            # Refuse to print vars, or the runner would validate a STALE server.
            print(
                "ERROR: uvicorn did not start within 10s (is port 8000 already in "
                "use?). Refusing to print stale env vars.",
                file=sys.stderr,
            )
            sys.stderr.flush()
            return 1

        print()
        print("=== e2e seed ready ===")
        print()
        print(f"E2E_PASSWORD_EMAIL={E2E_PASSWORD_EMAIL}")
        print(f"E2E_PASSWORD={E2E_PASSWORD}")
        print(f"E2E_PROFILE_ID={profile_id}")
        # LAST: scripts/e2e_run.sh waits for this line, so every line above is
        # already in its log when it reads them.
        print(f"E2E_CALL_ID={call_id}")
        print()
        print("Dev mode: start Vite in another terminal (cd frontend && pnpm dev),")
        print("or use the one-command runner: scripts/e2e_run.sh")
        print("Press Ctrl-C to stop uvicorn (the testcontainers Postgres stops too).")
        sys.stdout.flush()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

        server.should_exit = True
        return 0


if __name__ == "__main__":
    sys.exit(main())
