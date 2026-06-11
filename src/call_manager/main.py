from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from call_manager.auth.csrf import OriginCSRFMiddleware
from call_manager.auth.email import ConsoleEmailSender, ResendEmailSender, SMTPEmailSender
from call_manager.auth.rate_limit import SlidingWindowLimiter
from call_manager.auth.session_cookie import SessionSigner
from call_manager.config import Settings
from call_manager.db.session import make_engine, make_session_factory
from call_manager.embeddings.client import OllamaEmbedClient
from call_manager.logging import configure_logging
from call_manager.proposal_assist.citations import build_citation_service
from call_manager.proposal_assist.web.annotations import (
    router as annotations_router,
)
from call_manager.proposal_assist.web.citations import (
    router as citations_router,
)
from call_manager.proposal_assist.web.files import (
    router as files_router,
)
from call_manager.proposal_assist.web.references import (
    router as references_router,
)
from call_manager.proposal_assist.web.rubric_review import (
    router as rubric_review_router,
)
from call_manager.proposal_assist.web.sections import (
    router as sections_router,
)
from call_manager.proposal_assist.web.workspaces import (
    router as workspaces_router,
)
from call_manager.web.filters import (
    criterion_label,
    display_date,
    display_dt,
    model_label,
    note_category_heading,
)
from call_manager.web.middleware import (
    MaxBodySizeMiddleware,
    SecurityHeadersMiddleware,
    build_csp,
)
from call_manager.web.routes import admin, auth, calls, health, me, profiles

TEMPLATES_DIR = Path(__file__).parent / "web" / "templates"
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
        remote_rank_cosine_threshold=settings.remote_rank_cosine_threshold,
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
        start=True,
    )
    try:
        yield
    finally:
        # wait=True so an in-flight rank-drain or ingest job completes
        # against the still-open pool — Plan 3 made the rank tick 60s, so
        # mid-flight at shutdown is plausible (was daily-only before).
        app.state.scheduler.shutdown(wait=True)
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


def create_app() -> FastAPI:
    configure_logging()
    settings = Settings()

    # Email sender selection is environment-gated, checked BEFORE we
    # allocate a DB engine so the refuse-to-start path in production
    # doesn't leak a connection pool to the GC:
    # - RESEND_API_KEY set                → real ResendEmailSender (any environment).
    # - SMTP_HOST set (no RESEND key)     → SMTPEmailSender — accepted production transport
    #   for sites that run their own SMTP relay (e.g. Postfix/SendGrid SMTP).
    # - No key, no SMTP, cookie_secure=False → ConsoleEmailSender (dev over plain HTTP).
    # - No key, no SMTP, cookie_secure=True  → REFUSE to start. Falling back to
    #   ConsoleEmailSender in prod would (a) silently break login delivery and
    #   (b) write the full magic link to stderr/log aggregator — a 15-minute
    #   takeover window for anyone with log access. Loud failure only.
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
        email_sender = ConsoleEmailSender()
    else:
        raise RuntimeError(
            "RESEND_API_KEY or SMTP_HOST is required when APP_BASE_URL is https://. "
            "Refusing to fall back to ConsoleEmailSender in production "
            "(would log magic links to stderr — see auth/email.py)."
        )

    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    app = FastAPI(lifespan=lifespan)
    # CSRF defense: Origin/Referer enforcement for state-changing requests.
    # Sits below FastAPI's routing but above handlers — see auth/csrf.py
    # for the design rationale. Layers on top of the session cookie's
    # SameSite=lax (set in routes/auth.py).
    app.add_middleware(OriginCSRFMiddleware, expected_base_url=settings.app_base_url)

    # Defense-in-depth response headers (CSP, HSTS, nosniff, frame-options,
    # referrer-policy). HSTS only when the deployment is https (cookie_secure).
    app.add_middleware(SecurityHeadersMiddleware, csp=build_csp(), hsts=settings.cookie_secure)
    # Body-size cap. add_middleware prepends, so adding this LAST makes it the
    # outermost layer — an oversized body is rejected (413) before routing,
    # CSRF, or any handler reads it. Skipped entirely when set to 0.
    if settings.max_request_body_bytes:
        app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.max_request_body_bytes)

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

    # Redirect unauthenticated HTML requests to /login instead of returning
    # the raw JSON 401 from get_current_user. The auth dependency raises
    # HTTPException(401) for both API-ish and HTML routes; only the browser
    # case should redirect. Detect via the Accept header — curl / fetch
    # without Accept: text/html still gets the JSON 401, which keeps
    # api-ish callers and the test suite happy.
    # Statuses for which a browser (Accept: text/html) gets a styled HTML
    # error page instead of a raw JSON body. 401 is handled separately
    # (redirect to /login). API/non-HTML callers always get JSON.
    _HTML_ERROR_STATUSES = {400, 403, 404, 409, 413}

    @app.exception_handler(HTTPException)
    async def _redirect_html_401(request: Request, exc: HTTPException):
        wants_html = "text/html" in request.headers.get("accept", "")
        if exc.status_code == 401 and wants_html:
            # Preserve the originally-requested path so the user lands back
            # there post-login. /login itself doesn't read this yet — but
            # encoding it now means the next-URL feature is a one-line
            # change in the login_verify handler.
            return RedirectResponse(url="/login", status_code=303)
        if wants_html and exc.status_code in _HTML_ERROR_STATUSES:
            # A browser hitting a 403/404/etc. should see a styled page, not
            # a bare {"detail": ...} JSON blob. JSON is reserved for API
            # callers (no text/html in Accept).
            return app.state.templates.TemplateResponse(
                request=request,
                name="error.html",
                context={"status_code": exc.status_code, "message": exc.detail},
                status_code=exc.status_code,
                headers=getattr(exc, "headers", None) or None,
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None) or {},
        )

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
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # Per design spec: web layer renders all timestamps in Europe/Stockholm.
    # Templates use `{{ dt | display_dt }}` / `{{ dt | display_date }}` —
    # NEVER call .strftime() on a raw datetime in templates (it would
    # render UTC and display 1–2 hours wrong half the year).
    templates.env.filters["display_dt"] = display_dt
    templates.env.filters["display_date"] = display_date
    templates.env.filters["criterion_label"] = criterion_label
    templates.env.filters["model_label"] = model_label
    templates.env.filters["note_category_heading"] = note_category_heading
    app.state.templates = templates
    app.state.email_sender = email_sender
    app.state.email_limiter = SlidingWindowLimiter(
        max_requests=5, window_seconds=15 * 60, name="email"
    )
    app.state.ip_limiter = SlidingWindowLimiter(max_requests=30, window_seconds=15 * 60, name="ip")
    app.state.session_signer = SessionSigner(
        secret=settings.session_secret,
        max_age_seconds=settings.session_max_age_seconds,
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

    # Vendored static assets (htmx.min.js lives here). Serving locally
    # avoids an unreviewed third-party CDN dependency on every page load,
    # which in a hospital-internal context is unacceptable — a CDN
    # compromise would execute attacker JS on every logged-in request.
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(me.router)
    app.include_router(calls.router)
    app.include_router(admin.router)
    app.include_router(profiles.router)
    app.include_router(rubric_review_router)
    app.include_router(workspaces_router)
    app.include_router(sections_router)
    app.include_router(annotations_router)
    app.include_router(files_router)
    app.include_router(references_router)
    app.include_router(citations_router)
    return app
