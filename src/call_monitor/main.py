from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from call_monitor.auth.email import ConsoleEmailSender, ResendEmailSender, SMTPEmailSender
from call_monitor.auth.rate_limit import SlidingWindowLimiter
from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.config import Settings
from call_monitor.db.session import make_engine, make_session_factory
from call_monitor.embeddings.client import OllamaEmbedClient
from call_monitor.logging import configure_logging
from call_monitor.web.filters import display_date, display_dt
from call_monitor.web.routes import admin, auth, calls, health, me, profiles

TEMPLATES_DIR = Path(__file__).parent / "web" / "templates"
STATIC_DIR = Path(__file__).parent / "web" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Deferred import: keeps `admin_bootstrap` off the import graph until
    # lifespan runs, so test-side `create_app()` calls that never enter
    # lifespan don't pay the import cost or trigger its side-effects.
    from call_monitor.admin_bootstrap import bootstrap_admins
    bootstrap_admins(
        app.state.session_factory,
        emails=app.state.settings.initial_admin_emails,
        allowed_domains=app.state.settings.allowed_email_domains,
    )

    from call_monitor.ingestion.alerts import ConsecutiveFailureTracker
    from call_monitor.scheduler import build_scheduler

    settings = app.state.settings
    alerter = ConsecutiveFailureTracker(
        threshold=3,
        sender=app.state.email_sender,
        admin_email=settings.admin_alert_email,
    )
    app.state.ingestion_alerter = alerter

    from call_monitor.email.factory import EmailConfigError, build_email_client

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

    app.state.scheduler = build_scheduler(
        session_factory=app.state.session_factory,
        eu_portal_base_url=settings.eu_portal_base_url,
        vinnova_base_url=settings.vinnova_base_url,
        alerter=alerter,
        ollama_url=settings.ollama_url,
        gemma_model=settings.gemma_model,
        ranking_tick_seconds=settings.ranking_tick_seconds,
        ranking_queue_batch=settings.ranking_queue_batch,
        embed_client=app.state.embed_client,
        digest_email_client=digest_email_client,
        digest_settings=settings,
        start=True,
    )
    try:
        yield
    finally:
        # wait=True so an in-flight rank-drain or ingest job completes
        # against the still-open pool — Plan 3 made the rank tick 60s, so
        # mid-flight at shutdown is plausible (was daily-only before).
        app.state.scheduler.shutdown(wait=True)
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
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # Per design spec: web layer renders all timestamps in Europe/Stockholm.
    # Templates use `{{ dt | display_dt }}` / `{{ dt | display_date }}` —
    # NEVER call .strftime() on a raw datetime in templates (it would
    # render UTC and display 1–2 hours wrong half the year).
    templates.env.filters["display_dt"] = display_dt
    templates.env.filters["display_date"] = display_date
    app.state.templates = templates
    app.state.email_sender = email_sender
    app.state.email_limiter = SlidingWindowLimiter(
        max_requests=5, window_seconds=15 * 60, name="email"
    )
    app.state.ip_limiter = SlidingWindowLimiter(
        max_requests=30, window_seconds=15 * 60, name="ip"
    )
    app.state.session_signer = SessionSigner(
        secret=settings.session_secret, max_age_seconds=30 * 24 * 3600
    )
    # Single shared embed client: handed to the scheduler (ingest tick uses it
    # to embed touched calls before enqueueing ranking jobs — Codex H1) and to
    # the profile-edit handler (re-embed on description change). One instance
    # so connection pooling on httpx.Client is reused across calls.
    app.state.embed_client = OllamaEmbedClient(
        base_url=settings.ollama_url, model=settings.embedding_model
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
    return app
