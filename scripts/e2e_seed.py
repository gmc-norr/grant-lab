"""E2E seed helper for the collab Playwright round-trip (Slice 3b-3, Task 6).

Boots a testcontainers Postgres, runs Alembic migrations, seeds an ACTIVE
workspace with collab enabled on the draft artifact, starts uvicorn in
SPA_MODE=dev (APP_BASE_URL=http://localhost:5173 so the CSRF Origin check
passes against the Vite proxy origin), then prints the env vars needed to
run `pnpm exec playwright test e2e/collab.spec.ts`. Set E2E_SPA_MODE=prod
(3b-5d.D4) to instead serve the BUILT bundle from FastAPI on :8000 under the
strict CSP — see scripts/collab_e2e_run.sh, which builds+syncs the bundle
and drives this mode end to end:

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

# Add the repo root so `tests.*` and `grant_lab.*` are importable.
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))


# The password login frontend/e2e/dashboard.spec.ts performs. Fixed, because the
# spec's login() helper hard-codes both values.
E2E_PASSWORD_EMAIL = "e2e@umu.se"
E2E_PASSWORD = "correct-horse-1"


def main() -> int:
    os.environ.setdefault("EMBEDDING_MODEL", "bge-m3")
    # Minimal env that create_app/Settings validates.
    os.environ.setdefault("SESSION_SECRET", "e2e-seed-secret-at-least-16-chars!")
    os.environ.setdefault("ALLOWED_EMAIL_DOMAINS", "umu.se")
    # SPA_MODE: "dev" (default) uses the Vite server + dev CSP's broad ws: token.
    # "prod" (E2E_SPA_MODE=prod) serves the BUILT bundle from FastAPI under the
    # strict CSP with ONLY the same-origin ws token derived from APP_BASE_URL —
    # the 3b-5d.D4 flip gate. prod REQUIRES a built SPA synced to
    # src/grant_lab/web/spa/ (the runner does this).
    os.environ["SPA_MODE"] = os.environ.get("E2E_SPA_MODE", "dev")
    # APP_BASE_URL must match the origin the browser POSTs from (CSRF Origin
    # check + the CSP's derived ws token). In dev the browser talks to the Vite
    # proxy on :5173; in prod FastAPI serves the bundle directly on :8000, so
    # the browser (and the WS token) must agree on :8000 there instead.
    os.environ["APP_BASE_URL"] = (
        "http://localhost:8000" if os.environ["SPA_MODE"] == "prod" else "http://localhost:5173"
    )

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
        from grant_lab.main import create_app

        app = create_app()

        # Override session_factory so the app uses the testcontainers DB.
        from grant_lab.db.session import make_engine, make_session_factory

        engine = make_engine(db_url)
        Session = make_session_factory(engine)
        app.state.session_factory = Session

        # --- seed: two users, two collab artifacts ---
        from grant_lab.proposal_assist.citations.gitio import _git_env
        from grant_lab.proposal_assist.collab import state as collab_state
        from tests.proposal_assist.conftest import make_user, make_workspace
        from tests.test_editing_save import _init_repo

        from grant_lab.auth.passwords import hash_password
        from grant_lab.db.models import (
            Call,
            Reference,
            ReferenceChunk,
            Source,
            User,
            WorkspaceViewer,
        )

        suffix = uuid.uuid4().hex[:8]
        with Session() as s:
            owner = make_user(s, email=f"e2e-owner-{suffix}@umu.se")
            member = make_user(s, email=f"e2e-member-{suffix}@umu.se")
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
            # --- dashboard e2e prerequisites (review 2026-09-02 A6 finding 6) ---
            # frontend/e2e/dashboard.spec.ts logs in through the REAL password
            # form as e2e@umu.se / correct-horse-1, and this seeder created no
            # password user at all — so that spec could not pass for anyone who
            # had not hand-seeded one, and the whole file sat unrunnable (a HEAD
            # commit even "fixed" its password selector). Fixed email on purpose:
            # the spec's login() helper hard-codes it.
            dash_user = User(
                email=E2E_PASSWORD_EMAIL,
                status="active",
                password_hash=hash_password(E2E_PASSWORD),
            )
            s.add(dash_user)
            # ... and at least one Call, or the call-detail navigation test
            # skips itself on an empty feed (dashboard.spec.ts) — a green run
            # that exercised nothing.
            src = s.query(Source).filter_by(slug="vinnova").one_or_none()
            if src is None:
                src = Source(slug="vinnova", name="Vinnova", kind="api")
                s.add(src)
                s.flush()
            s.add(
                Call(
                    source_id=src.id,
                    external_id=f"E2E-{suffix}",
                    title="Precision medicine programme",
                    url=f"https://example.org/calls/E2E-{suffix}",
                    summary="Funding for AI-based diagnostics.",
                    full_text=(
                        "Body about eligibility, scope, budget, expected "
                        "outcomes, restrictions, mandatory."
                    ),
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
        if not server.started:
            # uvicorn never bound (e.g. :8000 already in use by an orphaned run).
            # Fail LOUD and refuse to print env vars — otherwise a runner's readiness
            # probe would validate the STALE server/DB while our fresh workspace is
            # unreachable. The `with PostgresContainer` context stops the PG on return.
            print(
                "ERROR: uvicorn did not start within 10s (is port 8000 already in "
                "use?). Refusing to print stale env vars.",
                file=sys.stderr,
            )
            sys.stderr.flush()
            return 1

        print()
        print("=== collab e2e seed ready (2 users) ===")
        print()
        print(f"E2E_COLLAB_WS_ID={wid}")
        print(f"E2E_COOKIE_OWNER={owner_cookie}")
        print(f"E2E_COOKIE_MEMBER={member_cookie}")
        print(f"E2E_SESSION_COOKIE={owner_cookie}")  # back-compat with the pre-D0 tests
        print(f"E2E_OWNER_LABEL={owner_label}")
        print(f"E2E_MEMBER_LABEL={member_label}")
        print(f"E2E_PASSWORD_EMAIL={E2E_PASSWORD_EMAIL}")
        print(f"E2E_PASSWORD={E2E_PASSWORD}")
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
