from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from call_monitor.config import Settings
from call_monitor.db.session import make_engine, make_session_factory
from call_monitor.logging import configure_logging

TEMPLATES_DIR = Path(__file__).parent / "web" / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    from call_monitor.admin_bootstrap import bootstrap_admins
    bootstrap_admins(
        app.state.session_factory,
        emails=app.state.settings.initial_admin_emails,
    )
    yield


def create_app() -> FastAPI:
    configure_logging()
    settings = Settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    from call_monitor.auth.email import ConsoleEmailSender, ResendEmailSender
    from call_monitor.auth.rate_limit import SlidingWindowLimiter
    from call_monitor.auth.session_cookie import SessionSigner
    from call_monitor.web.routes import auth, health, me

    # Email sender selection is environment-gated:
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

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.email_sender = email_sender
    app.state.email_limiter = SlidingWindowLimiter(max_requests=5, window_seconds=15 * 60)
    app.state.ip_limiter = SlidingWindowLimiter(max_requests=30, window_seconds=15 * 60)
    app.state.session_signer = SessionSigner(
        secret=settings.session_secret, max_age_seconds=30 * 24 * 3600
    )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(me.router)
    return app
