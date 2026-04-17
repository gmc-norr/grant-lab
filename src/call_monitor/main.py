from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from call_monitor.auth.email import ConsoleEmailSender, ResendEmailSender
from call_monitor.auth.rate_limit import SlidingWindowLimiter
from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.config import Settings
from call_monitor.db.session import make_engine, make_session_factory
from call_monitor.logging import configure_logging
from call_monitor.web.routes import auth, health, me

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
    app.state.scheduler = build_scheduler(
        session_factory=app.state.session_factory,
        eu_portal_base_url=settings.eu_portal_base_url,
        vinnova_base_url=settings.vinnova_base_url,
        vinnova_api_key=settings.vinnova_api_key,
        alerter=alerter,
        start=True,
    )
    try:
        yield
    finally:
        # Stop the scheduler's background threads before disposing the engine
        # — a job firing during teardown could otherwise see a closed pool.
        # `wait=False` lets in-flight ranking-job churn drain async-style;
        # at our cron cadence (daily) there's almost never anything in flight.
        app.state.scheduler.shutdown(wait=False)
        # Preserve foundation review-fixes' engine disposal: without it,
        # repeated test-side create_app() calls leak DBAPI connections to
        # the GC, surfaced as ResourceWarning under filterwarnings=error.
        app.state.engine.dispose()


def create_app() -> FastAPI:
    configure_logging()
    settings = Settings()

    # Email sender selection is environment-gated, checked BEFORE we
    # allocate a DB engine so the refuse-to-start path in production
    # doesn't leak a connection pool to the GC:
    # - RESEND_API_KEY set                → real ResendEmailSender (any environment).
    # - No key, but cookie_secure=False   → ConsoleEmailSender (dev over plain HTTP).
    # - No key AND cookie_secure=True     → REFUSE to start. Falling back to
    #   ConsoleEmailSender in prod would (a) silently break login delivery and
    #   (b) write the full magic link to stderr/log aggregator — a 15-minute
    #   takeover window for anyone with log access. Loud failure only.
    # Tests override this with InMemoryEmailSender via app.state after creation.
    if settings.resend_api_key:
        email_sender = ResendEmailSender(
            api_key=settings.resend_api_key, from_address=settings.email_from
        )
    elif not settings.cookie_secure:
        email_sender = ConsoleEmailSender()
    else:
        raise RuntimeError(
            "RESEND_API_KEY is required when APP_BASE_URL is https://. "
            "Refusing to fall back to ConsoleEmailSender in production "
            "(would log magic links to stderr — see auth/email.py)."
        )

    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
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

    # Vendored static assets (htmx.min.js lives here). Serving locally
    # avoids an unreviewed third-party CDN dependency on every page load,
    # which in a hospital-internal context is unacceptable — a CDN
    # compromise would execute attacker JS on every logged-in request.
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(me.router)
    return app
