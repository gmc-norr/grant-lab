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
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
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

    import uvicorn
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

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

        # --- build app ---
        from call_manager.main import create_app

        app = create_app()

        # Override session_factory so the app uses the testcontainers DB.
        from call_manager.db.session import make_engine, make_session_factory

        engine = make_engine(db_url)
        Session = make_session_factory(engine)
        app.state.session_factory = Session

        # --- seed: two users, two collab artifacts ---
        from call_manager.db.models import Reference, ReferenceChunk, WorkspaceViewer
        from call_manager.proposal_assist.citations.gitio import _git_env
        from call_manager.proposal_assist.collab import state as collab_state
        from tests.proposal_assist.conftest import make_user, make_workspace
        from tests.test_editing_save import _init_repo

        suffix = uuid.uuid4().hex[:8]
        with Session() as s:
            owner = make_user(s, email=f"e2e-owner-{suffix}@ki.se")
            member = make_user(s, email=f"e2e-member-{suffix}@ki.se")
            ws = make_workspace(s, owner=owner, current_phase="active")
            # Membership row: a WorkspaceViewer is a *writer* on the active draft
            # and *read-only* on the locked spec (resolve_access, ws_auth.py:118-126).
            s.add(WorkspaceViewer(workspace_id=ws.id, user_id=member.id))
            # Collab enabled on BOTH artifacts; spec locked so the member is read-only there.
            collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
            collab_state.enable_collab(s, workspace_id=ws.id, artifact="spec")
            ws.spec_locked_at = datetime.now(UTC)
            # One citeable reference (indexed) so the select->Cite flow has a source.
            ref = Reference(
                workspace_id=ws.id,
                kind="paper",
                source_type="uploaded",
                filename="seed-ref.pdf",
                storage_path=f"seed/{suffix}/seed-ref.pdf",
                mime_type="application/pdf",
                title="Seed Reference on Precision Medicine",
                citekey=f"seed{suffix}",
                text_status="done",
                metadata_status="approved",
            )
            s.add(ref)
            s.flush()
            s.add(
                ReferenceChunk(
                    reference_id=ref.id,
                    workspace_id=ws.id,
                    chunk_index=0,
                    text=(
                        "Precision medicine improves diagnostic yield in "
                        "oncology cohorts and shortens time to targeted therapy."
                    ),
                    embedding=[0.0] * 1024,
                    embedding_model="bge-m3",
                )
            )
            s.commit()
            wid = ws.id
            owner_label = owner.email.split("@")[0]
            member_label = member.email.split("@")[0]
            owner_cookie = app.state.session_signer.make(user_id=owner.id)
            member_cookie = app.state.session_signer.make(user_id=member.id)

        # Repo: draft (proposal.md) + a NON-EMPTY spec (proposal-spec.md), both committed.
        # Empty spec sections would trigger SpecPanel's "Start spec from skeleton" CTA
        # instead of the read-only CollabEditor the read-only scenario needs.
        draft_doc = (
            "Preamble.\n\n## Aims\n\n"
            "Precision medicine improves diagnostic outcomes for patients.\n"
        )
        repo = _init_repo(workspaces, wid, doc=draft_doc)
        (repo / "proposal-spec.md").write_text(
            "# Spec\n\n## Aim\n\nThe aim of this proposal is precision oncology.\n"
        )
        env = _git_env()
        subprocess.run(["git", "add", "proposal-spec.md"], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-qm", "seed spec"], cwd=repo, check=True, env=env)

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
        print("=== collab e2e seed ready (2 users) ===")
        print()
        print(f"E2E_COLLAB_WS_ID={wid}")
        print(f"E2E_COOKIE_OWNER={owner_cookie}")
        print(f"E2E_COOKIE_MEMBER={member_cookie}")
        print(f"E2E_SESSION_COOKIE={owner_cookie}")  # back-compat with the pre-D0 tests
        print(f"E2E_OWNER_LABEL={owner_label}")
        print(f"E2E_MEMBER_LABEL={member_label}")
        print()
        print("Owner is a writer on draft + spec; member is a writer on the")
        print("active draft and READ-ONLY on the locked spec (?tab=spec).")
        print()
        print("Now start Vite in another terminal:  cd frontend && pnpm dev")
        print("Or use the one-command runner:        scripts/collab_e2e_run.sh")
        print()
        print("Press Ctrl-C to stop uvicorn (testcontainers Postgres also stops).")
        print()
        sys.stdout.flush()  # Codex High: flush before the blocking sleep loop so the
        #                     runner (which reads a redirected stdout file) sees the vars.

        # Block until interrupted (portable — signal.pause() is Linux-only).
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

        server.should_exit = True
        return 0


if __name__ == "__main__":
    sys.exit(main())
