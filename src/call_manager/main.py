from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
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
from call_manager.proposal_assist.references.admission import IngestLimiter
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


@asynccontextmanager
async def lifespan(app: FastAPI):
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

    app.state.scheduler = build_scheduler(
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
        await _asyncio.to_thread(app.state.scheduler.shutdown, wait=True)
        # Release the single-instance advisory lock (closes its dedicated
        # connection). Done after the scheduler stops so no in-flight job is
        # still relying on single-instance semantics. Must precede
        # engine.dispose() below — the lock holds its own connection.
        _lock = getattr(app.state, "scheduler_lock", None)
        if _lock is not None:
            _lock.release()
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


def create_app() -> FastAPI:
    configure_logging()
    settings = Settings()

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
    _csp = build_csp(dev=settings.spa_mode == "dev")
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
