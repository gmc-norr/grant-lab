import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import PlainTextResponse

from call_manager.auth.csrf import OriginCSRFMiddleware
from call_manager.auth.dependencies import AccountInactive
from call_manager.auth.email import (
    ConsoleEmailSender,
    NoOpEmailSender,
    ResendEmailSender,
    SMTPEmailSender,
)
from call_manager.auth.oauth_txn import TransactionCipher
from call_manager.auth.oidc import OIDCClient
from call_manager.auth.rate_limit import SlidingWindowLimiter
from call_manager.auth.session_cookie import SessionSigner
from call_manager.config import Settings
from call_manager.db.session import make_engine, make_session_factory
from call_manager.embeddings.client import OllamaEmbedClient
from call_manager.logging import configure_logging
from call_manager.proposal_assist.citations import build_citation_service
from call_manager.proposal_assist.collab import state as collab_state
from call_manager.proposal_assist.drafting.contract import WorkspaceRetired
from call_manager.proposal_assist.drafting.locking import (
    BUSY_ERROR_MESSAGE,
    DELETED_ERROR_MESSAGE,
    WorkspaceDeleted,
    is_lock_timeout,
)
from call_manager.proposal_assist.references.admission import IngestLimiter
from call_manager.proposal_assist.web import collab_checkpoint, collab_ws
from call_manager.proposal_assist.web.activity import (
    router as activity_router,
)
from call_manager.proposal_assist.web.coherence import (
    router as coherence_router,
)
from call_manager.proposal_assist.web.evaluator import (
    router as evaluator_router,
)
from call_manager.proposal_assist.web.problems import (
    router as problems_router,
)
from call_manager.proposal_assist.web.proposals import (
    router as proposals_router,
)
from call_manager.proposal_assist.web.review import (
    router as review_router,
)
from call_manager.proposal_assist.web.uncited_claims import (
    router as uncited_claims_router,
)
from call_manager.web.api import router as api_router
from call_manager.web.api.errors import api_error, api_request, code_for
from call_manager.web.middleware import (
    MaxBodySizeMiddleware,
    SecurityHeadersMiddleware,
    build_csp,
)
from call_manager.web.routes import (
    health,
    oauth_google,
)
from call_manager.web.spa_serving import mount_spa

STATIC_DIR = Path(__file__).parent / "web" / "static"

# Module-level, unlike the deferred `import logging as _logging` inside lifespan
# (those are local to keep the lifespan's import graph lazy). The exception
# handlers below run per REQUEST, so their logger is bound once here, the way
# every sibling module does it (workspaces.py, scheduler.py, admin_bootstrap.py).
_logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 3b-5d.D4: kill-switch handover BEFORE serving — drain every collab log to
    # git and truncate it, so the git-first world is coherent from request #1.
    if app.state.settings.collab_kill_switch:
        import logging

        from starlette.concurrency import run_in_threadpool

        from call_manager.proposal_assist.collab.handover import run_kill_switch_handover

        _n = await run_in_threadpool(
            lambda: run_kill_switch_handover(
                app.state.session_factory,
                workspaces_dir=app.state.settings.workspaces_dir,
            )
        )
        logging.getLogger(__name__).warning(
            "collab kill-switch engaged — handover drained %d artifact(s)", _n
        )

    # Deferred import: keeps `admin_bootstrap` off the import graph until
    # lifespan runs, so test-side `create_app()` calls that never enter
    # lifespan don't pay the import cost or trigger its side-effects.
    from call_manager.admin_bootstrap import bootstrap_admins

    bootstrap_admins(
        app.state.session_factory,
        emails=app.state.settings.initial_admin_emails,
        allowed_domains=app.state.settings.allowed_email_domains,
        password=app.state.settings.initial_admin_password,
    )

    # Sweep stranded reference rows from a prior crash: anything in
    # text_status='running' is a Phase 2 ingest that didn't complete
    # (process was killed mid-Stage-1 by SIGTERM, deploy, OOM, etc.).
    # The retry-via-UI flow lands in Slice 5; for now we just reset
    # them to 'failed' so the admin panel reflects the truth and the
    # user can re-upload. Per post-Slice-2 code review.
    from sqlalchemy import update as _sql_update

    from call_manager.db.models import Reference

    with app.state.session_factory() as _bootstrap_s:
        n_reset = _bootstrap_s.execute(
            _sql_update(Reference)
            .where(Reference.text_status == "running")
            .values(
                text_status="failed",
                text_error="ingest interrupted (server restart before Stage 1 completed)",
            )
        ).rowcount
        _bootstrap_s.commit()
        if n_reset:
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "lifespan startup: reset %d stranded reference rows from "
                "text_status='running' to 'failed' (likely killed by prior "
                "restart). Users can re-upload affected refs.",
                n_reset,
            )

    from call_manager.ingestion.alerts import ConsecutiveFailureTracker
    from call_manager.scheduler import build_scheduler

    settings = app.state.settings
    alerter = ConsecutiveFailureTracker(
        threshold=3,
        sender=app.state.email_sender,
        admin_email=settings.admin_alert_email,
        session_factory=app.state.session_factory,
    )
    app.state.ingestion_alerter = alerter

    from call_manager.email.factory import EmailConfigError, build_email_client

    digest_email_client = None
    if settings.resend_api_key or settings.smtp_host:
        try:
            digest_email_client = build_email_client(settings)
            app.state.digest_email_client = digest_email_client
        except EmailConfigError as e:
            import logging

            logging.getLogger(__name__).warning(
                "digest.email_client_unavailable",
                extra={"event": "digest_email_client_unavailable", "error": str(e)},
            )

    from call_manager.proposal_assist.drafting.factory import (
        build_workspace_controller,
    )

    workspace_controller = build_workspace_controller(
        settings=settings,
        session_factory=app.state.session_factory,
        reference_ingest_limiter=app.state.reference_ingest_limiter,
    )
    app.state.workspace_controller = workspace_controller

    # Workspace retirement (Slice B-retire): one coordinator fans a delete out to
    # every subsystem that holds live per-workspace state. Constructed HERE, in the
    # lifespan, so a missing required hook raises MissingRetirementHook and the app
    # fails to boot rather than silently skipping a teardown on every delete (the
    # spec's startup invariant). Local imports follow this module's lifespan
    # convention (see build_workspace_controller above).
    from call_manager.proposal_assist.collab.room import MANAGER as _COLLAB_MANAGER
    from call_manager.proposal_assist.jobs_retirement import build_jobs_retirement_hook
    from call_manager.proposal_assist.lifecycle import LIFECYCLE
    from call_manager.proposal_assist.passes import PASS_REGISTRY
    from call_manager.proposal_assist.retirement import WorkspaceRetirement

    app.state.workspace_lifecycle = LIFECYCLE
    app.state.workspace_retirement = WorkspaceRetirement(
        hooks={
            "turns": workspace_controller.registry.retire_workspace,
            "collab": _COLLAB_MANAGER.retire_workspace,
            "jobs": build_jobs_retirement_hook(app.state.session_factory),
            "passes": PASS_REGISTRY.retire_workspace,
        },
        lifecycle=LIFECYCLE,
    )

    # Claim-analysis embedder: the same construction reference ingest uses
    # (web/references.py wraps the shared embed_client per request). Always
    # constructible — embed_client is built unconditionally in create_app()
    # and only fails at call time if Ollama is unreachable, which the drain
    # tick surfaces as per-job failures (retry → dead-letter), not a crash.
    from call_manager.proposal_assist.references.embed import OllamaEmbedderAdapter

    analysis_embedder = OllamaEmbedderAdapter(app.state.embed_client)

    import asyncio as _asyncio

    # Single-instance enforcement (Defect 1): the scheduler is in-process
    # APScheduler, so two replicas would each fire every job — duplicate
    # ingests, duplicate digests, duplicate ranking work. Acquire a
    # SESSION-level Postgres advisory lock on a dedicated long-lived
    # connection held for the process lifetime. If we get it, start the
    # scheduler; if another instance holds it, build the scheduler but do NOT
    # start it (so trigger_job_now / introspection still work) and log loudly.
    from call_manager.scheduler import try_acquire_scheduler_lock

    scheduler_lock = try_acquire_scheduler_lock(app.state.engine)
    app.state.scheduler_lock = scheduler_lock
    start_scheduler = scheduler_lock is not None
    if not start_scheduler:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "scheduler.lock_not_acquired",
            extra={
                "event": "scheduler_lock_not_acquired",
                "detail": (
                    "another instance holds the scheduler advisory lock (or the "
                    "backend does not support it); NOT starting the in-process "
                    "scheduler in this replica to avoid double-running every job"
                ),
            },
        )

    # Bound to a LOCAL as well as app.state: the teardown below must stop the
    # scheduler THIS lifespan started, not whatever app.state happens to hold by
    # then. A second lifespan entered on the same app object (a nested TestClient)
    # overwrites both attributes — with a scheduler built start=False, since the
    # nested advisory-lock acquire necessarily loses to the outer one — and the
    # outer teardown then shut down that inert object and released a None lock,
    # orphaning the running scheduler and its advisory lock for the rest of the
    # process. Locals make each lifespan tear down exactly what it created.
    scheduler = build_scheduler(
        session_factory=app.state.session_factory,
        settings=settings,
        alerter=alerter,
        ollama_url=settings.ollama_url,
        gemma_model=settings.gemma_model,
        ranking_tick_seconds=settings.ranking_tick_seconds,
        ranking_queue_batch=settings.ranking_queue_batch,
        embedding_pregate_threshold=settings.embedding_pregate_threshold,
        embedding_hard_fail_threshold=settings.embedding_hard_fail_threshold,
        anthropic_api_key=settings.anthropic_api_key,
        remote_rank_model=settings.remote_rank_model,
        ranking_escalation_min_score=settings.ranking_escalation_min_score,
        ranking_remote_daily_cap=settings.ranking_remote_daily_cap,
        embed_client=app.state.embed_client,
        digest_email_client=digest_email_client,
        digest_settings=settings,
        limiters=[app.state.email_limiter, app.state.ip_limiter],
        workspace_controller=workspace_controller,
        # Never None here: build_citation_service raises in create_app() on
        # misconfig, so a started app always has a real service.
        citation_service=app.state.citation_service,
        analysis_embedder=analysis_embedder,
        app_loop=_asyncio.get_running_loop(),
        start=start_scheduler,
    )
    app.state.scheduler = scheduler
    app.state.app_loop = _asyncio.get_running_loop()
    try:
        yield
    finally:
        # wait=True so an in-flight rank-drain or ingest job completes
        # against the still-open pool — Plan 3 made the rank tick 60s, so
        # mid-flight at shutdown is plausible (was daily-only before).
        #
        # Run the BLOCKING shutdown off the event loop (to_thread). The drain/
        # idle-sweep ticks run in APScheduler worker threads and block on
        # run_coroutine_threadsafe(...).result() — they can only finish once the
        # app loop runs their coroutine. Calling shutdown(wait=True) directly on
        # the loop would block the loop waiting for those workers, which are in
        # turn waiting for the loop → self-deadlock until the fut.result timeout.
        #
        # No `running` guard needed: build_scheduler returns a
        # _SafeBackgroundScheduler, whose shutdown() is a documented no-op when the
        # scheduler was never started (the start=False path taken by whichever
        # replica loses the advisory lock).
        await _asyncio.to_thread(scheduler.shutdown, wait=True)
        # Release the single-instance advisory lock (closes its dedicated
        # connection). Done after the scheduler stops so no in-flight job is
        # still relying on single-instance semantics. Must precede
        # engine.dispose() below — the lock holds its own connection.
        if scheduler_lock is not None:
            scheduler_lock.release()
        # B6-residuals: terminate every live drafting subprocess before
        # disposing the engine. Without this, each `lifespan` exit leaks
        # SDK subprocesses + the per-workspace pg advisory-lock sessions
        # the registry holds open for handle lifetime (see
        # proposal_assist/drafting/registry.py — `_Entry.lock_session`).
        # Run sequentially to avoid hammering the SDK during shutdown.
        ctrl = getattr(app.state, "workspace_controller", None)
        if ctrl is not None:
            live_ids = list(ctrl._registry._entries.keys())
            for wsid in live_ids:
                try:
                    await ctrl.terminate(wsid)
                except Exception:
                    import logging

                    logging.getLogger(__name__).warning(
                        "lifespan teardown: controller.terminate failed for %s",
                        wsid,
                        exc_info=True,
                    )
        # Foundation v1 review-fixes added engine.dispose() here. Preserve it:
        # without disposal, repeated test-side create_app() calls leak DBAPI
        # connections to the GC, which psycopg surfaces as ResourceWarning
        # (and pyproject.toml has filterwarnings=["error::ResourceWarning"]).
        app.state.engine.dispose()


# The pgvector columns in db/models.py are hard-coded `Vector(1024)` (×3).
# EMBEDDING_DIM is configurable, but a value other than this would only fail
# LATE, at the first INSERT, with an opaque pgvector dimension error. Assert the
# two agree at boot so a config-only model swap (e.g. to a 768-dim model) fails
# FAST with a clear pointer to the required column migration.
EMBEDDING_COLUMN_DIM = 1024

# Strict host[:port] shape for a CSP token derived from user-supplied config.
# `urlsplit().netloc` only terminates at `/`, `?` or `#` — NOT `;` or
# whitespace — so a value like "https://app.ki.se; script-src * 'unsafe-inline'"
# parses to a netloc of "app.ki.se; script-src * 'unsafe-inline'" and, absent
# this check, would be interpolated verbatim into the CSP header, injecting
# arbitrary directives. config.py's APP_BASE_URL validator only checks the
# scheme prefix, so this is the last line of defense before the value reaches
# the header.
_HOST_PORT_RE = re.compile(r"^[A-Za-z0-9.-]+(:\d{1,5})?$")


def _derive_ws_origin(app_base_url: str) -> str:
    """Derive the same-origin `ws(s)://host[:port]` CSP token from APP_BASE_URL.

    Raises RuntimeError (fail loudly at startup, matching the sibling
    scheme/netloc check) if the netloc isn't a plain host[:port] — this is
    what stops CSP directive injection via a malicious/malformed APP_BASE_URL.
    """
    _base = urlsplit(app_base_url)
    if not _base.scheme or not _base.netloc:
        raise RuntimeError(f"APP_BASE_URL is not a valid URL: {app_base_url!r}")
    if not _HOST_PORT_RE.match(_base.netloc):
        raise RuntimeError(
            f"APP_BASE_URL host is not a valid host[:port]: {_base.netloc!r}"
        )
    return f"{'wss' if _base.scheme == 'https' else 'ws'}://{_base.netloc}"


def create_app() -> FastAPI:
    configure_logging()
    settings = Settings()

    # 3b-5d.D4: arm the collab kill-switch BEFORE any request can consult the
    # flag. Module-level on purpose — see collab/state.py.
    collab_state.set_kill_switch(settings.collab_kill_switch)

    if settings.embedding_dim != EMBEDDING_COLUMN_DIM:
        raise RuntimeError(
            f"EMBEDDING_DIM={settings.embedding_dim} does not match the hard-coded "
            f"pgvector column width Vector({EMBEDDING_COLUMN_DIM}) in db/models.py. "
            "Changing the embedding dimension requires BOTH updating EMBEDDING_DIM "
            "AND an Alembic migration that ALTERs the embedding columns to the new "
            f"width; the columns are immovably Vector({EMBEDDING_COLUMN_DIM}) today. "
            "Set EMBEDDING_DIM=1024 or migrate the columns first."
        )

    # Email sender selection is environment-gated, checked BEFORE we
    # allocate a DB engine so the refuse-to-start path in production
    # doesn't leak a connection pool to the GC:
    # - RESEND_API_KEY set                → real ResendEmailSender (any environment).
    # - SMTP_HOST set (no RESEND key)     → SMTPEmailSender — accepted production transport
    #   for sites that run their own SMTP relay (e.g. Postfix/SendGrid SMTP).
    # - No key, no SMTP, cookie_secure=False → ConsoleEmailSender (dev over plain HTTP).
    # - No key, no SMTP, cookie_secure=True  → NoOpEmailSender (prod). The email
    #   sender is now best-effort only (ingestion/digest alerts); login no longer
    #   depends on it (magic-link auth was removed). We drop alerts silently
    #   rather than console-log their bodies in prod — ConsoleEmailSender stays
    #   dev-only because it logs full message contents.
    # Tests override this with InMemoryEmailSender via app.state after creation.
    if settings.resend_api_key:
        email_sender = ResendEmailSender(
            api_key=settings.resend_api_key, from_address=settings.email_from
        )
    elif settings.smtp_host:
        email_sender = SMTPEmailSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            user=settings.smtp_user,
            password=settings.smtp_password,
            from_address=settings.email_from,
        )
    elif not settings.cookie_secure:
        email_sender = ConsoleEmailSender()  # dev over plain HTTP
    else:
        # Prod with no transport: drop best-effort alerts rather than refuse to
        # start or console-log alert bodies. (Magic-link login — the original
        # reason for the hard requirement — has been removed.)
        email_sender = NoOpEmailSender()

    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    # Disable the interactive API docs + raw schema URL in every non-dev mode.
    # In dev (spa_mode='dev') the schema is useful for local DX; in prod/disabled
    # it leaks the full API surface unauthenticated. The offline codegen script
    # (scripts/gen_openapi_types.sh) calls create_app().openapi() directly — it
    # does NOT fetch /openapi.json over HTTP — so disabling the route is safe.
    _dev = settings.spa_mode == "dev"
    app = FastAPI(
        title="Grant Lab",
        lifespan=lifespan,
        docs_url="/docs" if _dev else None,
        redoc_url="/redoc" if _dev else None,
        openapi_url="/openapi.json" if _dev else None,
    )
    # CSRF defense: Origin/Referer enforcement for state-changing requests.
    # Sits below FastAPI's routing but above handlers — see auth/csrf.py
    # for the design rationale. Layers on top of the session cookie's
    # SameSite=lax (set in routes/auth.py).
    app.add_middleware(OriginCSRFMiddleware, expected_base_url=settings.app_base_url)

    # Defense-in-depth response headers (CSP, HSTS, nosniff, frame-options,
    # referrer-policy). HSTS only when the deployment is https (cookie_secure).
    #
    # 3b-5d.D4: the Yjs WS needs an explicit same-origin token in the non-dev
    # CSP. Derived from APP_BASE_URL — which also drives check_origin, cookie
    # security and HSTS, so scheme misalignment (and a malformed/malicious
    # netloc — see _derive_ws_origin) fails loudly here at startup.
    _ws_origin = _derive_ws_origin(settings.app_base_url)
    _csp = build_csp(dev=settings.spa_mode == "dev", ws_origin=_ws_origin)
    _hsts = settings.cookie_secure
    app.add_middleware(
        SecurityHeadersMiddleware,
        csp=_csp,
        hsts=_hsts,
    )
    # Body-size cap. add_middleware prepends, so adding this LAST makes it the
    # outermost layer — an oversized body is rejected (413) before routing,
    # CSRF, or any handler reads it. Skipped entirely when set to 0.
    #
    # Pass the SAME csp/hsts used above: the body-cap's early-reject responses
    # are emitted before SecurityHeadersMiddleware runs, so it stamps the
    # canonical security header set itself (else 413/400 would omit CSP/HSTS).
    if settings.max_request_body_bytes:
        app.add_middleware(
            MaxBodySizeMiddleware,
            max_bytes=settings.max_request_body_bytes,
            csp=_csp,
            hsts=_hsts,
        )
    # Gzip compression for responses >= 1 kB. Placed after other middlewares
    # so it compresses the final response body (outermost execution order is
    # add_middleware-last = first to execute in the response direction).
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    # Deployment-topology warning: when running https (i.e. production, behind
    # a TLS-terminating proxy) without TRUSTED_PROXY, the rate limiter keys off
    # request.client.host — which is the PROXY's IP for every request, so all
    # clients collapse into one bucket. Set TRUSTED_PROXY=true (and
    # TRUSTED_PROXY_HOPS) AND ensure the edge proxy OVERWRITES inbound
    # X-Forwarded-For. (If you genuinely terminate TLS in-process with no proxy,
    # leaving TRUSTED_PROXY=false is correct and this warning is expected.)
    if settings.cookie_secure and not settings.trusted_proxy:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "startup: APP_BASE_URL is https but TRUSTED_PROXY=false. If the app "
            "sits behind a reverse proxy, per-IP rate limiting will bucket ALL "
            "clients under the proxy's IP. Set TRUSTED_PROXY=true + "
            "TRUSTED_PROXY_HOPS and have the proxy overwrite X-Forwarded-For. "
            "Ignore this only if TLS terminates in-process with no proxy.",
            extra={"event": "startup_trusted_proxy_unset"},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException):
        if api_request(request):
            hdrs = getattr(exc, "headers", None)
            if exc.status_code >= 500:
                return api_error(
                    exc.status_code, "internal_error", "internal server error", headers=hdrs
                )
            return api_error(exc.status_code, code_for(exc.status_code), exc.detail, headers=hdrs)
        # Non-/api: every surviving non-/api route is public (oauth/health) or an
        # SPA path served at 200; none can 401 a browser any more, so the backend
        # never redirects browsers. A rare non-/api exception gets a minimal JSON body.
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None) or {},
        )

    @app.exception_handler(WorkspaceDeleted)
    async def _workspace_deleted(request: Request, exc: WorkspaceDeleted):
        # A writer that won the per-workspace commit mutex only to find the
        # workspace deleted. It mutated nothing. 404 matches what every reader
        # already sees — the workspace is gone — and keeps the SPA on its existing
        # not-found path. Registered explicitly so it resolves ahead of the
        # catch-all Exception handler below.
        if api_request(request):
            return api_error(404, "workspace_deleted", DELETED_ERROR_MESSAGE)
        return JSONResponse(status_code=404, content={"detail": DELETED_ERROR_MESSAGE})

    @app.exception_handler(WorkspaceRetired)
    async def _workspace_retired(request: Request, exc: WorkspaceRetired):
        # Same fact as WorkspaceDeleted, observed one layer earlier: the in-process
        # retirement tombstone. B-retire made RoomManager.acquire/operate raise this,
        # and operate raises from __aenter__ — i.e. BEFORE the `try` each kernel
        # writer opens inside its `async with`, so the writers' own clean-error
        # handling never sees it and it lands in the calling route's except chain,
        # which lists only cite/git/mutex errors. The collab writer routes (kernel
        # accept, direct cite, spec init, annotation insert) would otherwise 500 on
        # a delete race that used to yield a clean 404 from the mutex's
        # WorkspaceDeleted.
        #
        # Deliberately reuses the "workspace_deleted" code and message rather than
        # minting a "workspace_retired" one: to a client these are one event (the
        # workspace is gone), and reusing them makes the raced response BYTE-
        # IDENTICAL to the pre-B-retire behaviour. WorkspaceRetired is NOT a
        # subclass of WorkspaceDeleted (both are bare RuntimeErrors), so Starlette
        # cannot reach the handler above — this registration is required, not
        # redundant.
        if api_request(request):
            return api_error(404, "workspace_deleted", DELETED_ERROR_MESSAGE)
        return JSONResponse(status_code=404, content={"detail": DELETED_ERROR_MESSAGE})

    @app.exception_handler(OperationalError)
    async def _lock_timeout(request: Request, exc: OperationalError):
        # SQLSTATE 55P03 = lock_not_available: a `SET LOCAL lock_timeout` fired.
        # Since Slice C3, `acquire_commit_mutex` leaves a 2s cap in force for the
        # WHOLE mutex-held transaction (drafting/locking.py), so an untranslated
        # 55P03 reaching here is by definition a mutex-held tail giving up rather
        # than pinning the mutex against every other writer. It maps to the same
        # 429 the six `workspace_write` routes already emit, byte for byte.
        #
        # Registered centrally rather than per-route because a local `except` only
        # covers what its `try` encloses, and two sites (`turns.py:197`,
        # `proposals.py:145`) already commit OUTSIDE the relevant try — a local
        # clause would miss commit-time timeouts there. See spec section 5.
        #
        # `collab_checkpoint.py:57` answers 503 for the ACQUISITION failure and
        # keeps its own catch; that is CommitMutexTimeout, a different exception,
        # so it resolves before this handler ever sees anything.
        if not is_lock_timeout(exc):
            # A real connection/server failure. Same shape as the catch-all below;
            # deliberately NOT re-raised (see that handler's note on empty bodies).
            #
            # Logged HERE, with the traceback, because registering this handler is
            # what took the whole OperationalError class OFF the logging path.
            # Starlette routes a handler for `Exception` through
            # ServerErrorMiddleware, which sends the response and then RE-RAISES —
            # and that re-raise is precisely how uvicorn emits "Exception in ASGI
            # application" with a full traceback. A handler for any other class goes
            # to ExceptionMiddleware, which does not re-raise. So without this line a
            # connection loss, a server restart, a 40P01 deadlock, a 40001 or ENOSPC
            # would 500 with no traceback anywhere: this app registers no logging
            # middleware, and nothing else on the path logs.
            _logger.error(
                "db.operational_error",
                extra={
                    "event": "db_operational_error",
                    # Path only — never the query string or the request body.
                    "path": request.url.path,
                    "sqlstate": getattr(getattr(exc, "orig", None), "sqlstate", None),
                },
                exc_info=True,
            )
            if api_request(request):
                return api_error(500, "internal_error", "internal server error")
            return PlainTextResponse("Internal Server Error", status_code=500)
        # Routine contention, so WARNING rather than ERROR — but it must be
        # DISCOVERABLE. This 429 is the only signal that the 2s mutex-held tail cap
        # gave up, and every other site that meets the same timeout is silent about
        # it (flip.py defers, collab_ws closes 1013, the routes 429). If the cap
        # proves too tight in production, this line is the server-side way to find
        # out. Path only — never the query string or the request body.
        _logger.warning(
            "mutex_tail.lock_timeout",
            extra={
                "event": "mutex_tail_lock_timeout",
                "path": request.url.path,
            },
        )
        if api_request(request):
            return api_error(
                429, code_for(429), BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            )
        return JSONResponse(
            status_code=429,
            content={"detail": BUSY_ERROR_MESSAGE},
            headers={"Retry-After": "2"},
        )

    @app.exception_handler(AccountInactive)
    async def _inactive_account(request: Request, exc: AccountInactive):
        if api_request(request):
            # The SPA's unwrap routes off this 403: pending -> /auth/pending, any
            # other non-active status -> /auth/no-access (catch-all), mirroring the
            # deleted Jinja redirect split. Clear the now-inert session cookie so a
            # later re-login starts clean (status is gated before the epoch check, so
            # the cookie already grants zero access — this is hygiene, not security).
            target = "/auth/pending" if exc.status == "pending" else "/auth/no-access"
            resp = api_error(
                403, "account_inactive", "account not active", detail={"redirect": target}
            )
            resp.delete_cookie(
                "session",
                path="/",
                httponly=True,
                secure=settings.cookie_secure,
                samesite="lax",
            )
            return resp
        return JSONResponse(status_code=403, content={"detail": "account not active"})

    from fastapi.encoders import jsonable_encoder
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def _api_validation(request: Request, exc: RequestValidationError):
        # jsonable_encoder coerces any non-JSON-serializable Pydantic ctx (a
        # ValueError/Decimal/bytes raised by a future validator) so json.dumps
        # inside the handler can't throw and degrade this 422 to a 500.
        detail = jsonable_encoder(exc.errors())
        if api_request(request):
            return api_error(422, "validation_error", "request validation failed", detail)
        return JSONResponse(status_code=422, content={"detail": detail})

    @app.exception_handler(Exception)
    async def _api_500(request: Request, exc: Exception):
        if api_request(request):
            return api_error(500, "internal_error", "internal server error")
        # Non-/api: Starlette's normal plain 500. Do NOT `raise exc` (yields an EMPTY
        # body under raise_server_exceptions=False, and a global re-raise is the
        # "global catch-all" the spec warns against — it would reshape HTML 500s).
        return PlainTextResponse("Internal Server Error", status_code=500)

    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory

    from call_manager.embeddings.calibration import check_threshold_calibration

    calibration_warning = check_threshold_calibration(
        model=settings.embedding_model,
        pregate=settings.embedding_pregate_threshold,
        hard_fail=settings.embedding_hard_fail_threshold,
    )
    app.state.embedding_calibration_warning = calibration_warning
    if calibration_warning:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "embedding.threshold_calibration_mismatch",
            extra={
                "event": "embedding_threshold_calibration_mismatch",
                "model": settings.embedding_model,
                "detail": calibration_warning,
            },
        )
    # Phase 3 metadata extraction service. build_citation_service constructs a
    # StructuredLLM but does NOT open any connection at this point — the LLM
    # client is lazy/per-call, so this is safe at startup. Routes pass this
    # through to ingest_bytes so extraction runs in production.
    app.state.citation_service = build_citation_service(
        settings=settings, session_factory=session_factory
    )
    app.state.email_sender = email_sender
    app.state.email_limiter = SlidingWindowLimiter(
        max_requests=5, window_seconds=15 * 60, name="email"
    )
    app.state.ip_limiter = SlidingWindowLimiter(max_requests=30, window_seconds=15 * 60, name="ip")
    app.state.session_signer = SessionSigner(
        secret=settings.session_secret,
        max_age_seconds=settings.session_max_age_seconds,
    )
    app.state.oauth_txn_cipher = TransactionCipher(secret=settings.session_secret)
    app.state.google_oidc_client = (
        OIDCClient(
            discovery_url=settings.google_oidc_discovery_url,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
        )
        if settings.google_oidc_enabled
        else None
    )
    # Single shared embed client: handed to the scheduler (ingest tick uses it
    # to embed touched calls before enqueueing ranking jobs — Codex H1) and to
    # the profile-edit handler (re-embed on description change). Each
    # .embed() call opens its own httpx.Client (matches OllamaClient's
    # per-call pattern); for localhost Ollama the TCP cost is negligible.
    # Worth revisiting if we ever serve embeddings from a remote endpoint.
    app.state.embed_client = OllamaEmbedClient(
        base_url=settings.ollama_url,
        model=settings.embedding_model,
        expected_dim=settings.embedding_dim,
    )
    app.state.reference_ingest_limiter = IngestLimiter(
        capacity=settings.reference_ingest_concurrency
    )

    # Vendored static assets (only favicon.svg after htmx/Pico deletion). Serving
    # locally avoids an unreviewed third-party CDN dependency on every page load,
    # which in a hospital-internal context is unacceptable — a CDN compromise would
    # execute attacker JS on every logged-in request.
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(health.router)
    app.include_router(oauth_google.router)
    app.include_router(proposals_router)
    app.include_router(coherence_router)
    app.include_router(evaluator_router)
    app.include_router(review_router)
    app.include_router(problems_router)
    app.include_router(uncited_claims_router)
    app.include_router(activity_router)
    app.include_router(collab_ws.router)
    app.include_router(collab_checkpoint.router)
    app.include_router(api_router)

    # Dev-only SSE validation endpoint (/events/ping). Mounted ONLY in dev so it
    # never exists in prod/disabled (a test asserts 404 there). Registered before
    # mount_spa so it is a real route, not swallowed by the catch-all.
    if settings.spa_mode == "dev":
        from call_manager.web.api import events as _events

        app.include_router(_events.router)

    # MUST be the LAST route registration: mount_spa (when SPA_MODE=prod)
    # installs a catch-all GET /{full_path:path} that returns the SPA index
    # only for manifest-owned routes (is_spa_route) and a real 404 otherwise.
    # Every real router above is registered first, so concrete routes always
    # win; an unmatched /api/* still raises HTTPException(404) and flows
    # through the Plan-1 JSON envelope handler.
    mount_spa(app, spa_mode=settings.spa_mode)
    return app
