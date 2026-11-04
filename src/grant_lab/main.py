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

from grant_lab.auth.csrf import OriginCSRFMiddleware
from grant_lab.auth.dependencies import AccountInactive
from grant_lab.auth.email import (
    ConsoleEmailSender,
    EmailSender,
    NoOpEmailSender,
    TransportEmailSender,
)
from grant_lab.auth.oauth_txn import TransactionCipher
from grant_lab.auth.oidc import OIDCClient
from grant_lab.auth.rate_limit import SlidingWindowLimiter
from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.config import Settings
from grant_lab.db.session import make_engine, make_session_factory
from grant_lab.embeddings.client import OllamaEmbedClient
from grant_lab.errors import BUSY_ERROR_MESSAGE, is_lock_timeout
from grant_lab.logging import configure_logging
from grant_lab.web.api import build_api_router
from grant_lab.web.api.errors import api_error, api_request, code_for, json_safe_detail
from grant_lab.web.middleware import (
    MaxBodySizeMiddleware,
    SecurityHeadersMiddleware,
    build_csp,
)
from grant_lab.web.routes import (
    health,
    oauth_google,
)
from grant_lab.web.spa_serving import mount_spa

STATIC_DIR = Path(__file__).parent / "web" / "static"

# Module-level, unlike the deferred `import logging as _logging` inside lifespan
# (those are local to keep the lifespan's import graph lazy). The exception
# handlers below run per REQUEST, so their logger is bound once here, the way
# every sibling module does it (scheduler.py, admin_bootstrap.py).
_logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Single-instance enforcement comes FIRST — before the admin bootstrap below,
    # which mutates shared state a second process would fight over. The scheduler
    # is in-process APScheduler, so two replicas would each fire every job; the
    # login limiters are per-process too. Holding the session-level advisory lock
    # on a dedicated connection for the process lifetime means "I am the instance".
    #
    # Another process holding it is FATAL unless SCHEDULER_STANDBY_OK=true,
    # which boots an HTTP-only standby with no scheduler. A backend that cannot
    # answer the probe is an outage, not a second instance: tolerated with a
    # warning (no scheduler) so /healthz can report the outage itself.
    from grant_lab.scheduler import SingleInstanceViolation, probe_scheduler_lock

    # A second lifespan entered on the SAME app object (a test with two
    # TestClients, or a nested one) is not a second process: the outer lifespan
    # already holds the lock in this process. Reuse it — build an inert
    # scheduler, start nothing, release nothing — and leave app.state pointing
    # at the outer's live lock so /healthz keeps telling the truth. Decided
    # BEFORE probing, because the probe would otherwise overwrite that state.
    outer_lock = getattr(app.state, "scheduler_lock", None)
    if outer_lock is not None and outer_lock.is_held():
        scheduler_lock, lock_state = None, "reentrant"
    else:
        scheduler_lock, lock_state = probe_scheduler_lock(app.state.engine)
        app.state.scheduler_lock = scheduler_lock
    start_scheduler = scheduler_lock is not None
    if not start_scheduler:
        import logging as _logging

        if lock_state == "held_elsewhere" and not app.state.settings.scheduler_standby_ok:
            _logging.getLogger(__name__).error(
                "scheduler.lock_held_elsewhere_fatal",
                extra={
                    "event": "scheduler_lock_held_elsewhere_fatal",
                    "detail": (
                        "another process holds the single-instance advisory lock; "
                        "this app must run as ONE process — refusing to start. Set "
                        "SCHEDULER_STANDBY_OK=true for a deliberate HTTP-only standby"
                    ),
                },
            )
            raise SingleInstanceViolation(
                "another instance holds the scheduler advisory lock; refusing to start "
                "(set SCHEDULER_STANDBY_OK=true to boot as an HTTP-only standby)"
            )
        _logging.getLogger(__name__).warning(
            "scheduler.lock_not_acquired",
            extra={
                "event": "scheduler_lock_not_acquired",
                "lock_state": lock_state,
                "detail": (
                    "NOT starting the in-process scheduler in this process: "
                    + {
                        "held_elsewhere": "another instance holds the lock (standby mode)",
                        "reentrant": "this app already holds the lock (nested lifespan)",
                    }.get(lock_state, "the lock backend could not be probed (outage)")
                ),
            },
        )

    # Everything from here to the yield can raise (a bootstrap failure). The lock
    # is already ours, so give it back on the way out — a leaked lock would refuse
    # the NEXT boot in this process as a second instance. The finally after the
    # yield owns release on the normal path.
    try:
        # Deferred import: keeps `admin_bootstrap` off the import graph until
        # lifespan runs, so test-side `create_app()` calls that never enter
        # lifespan don't pay the import cost or trigger its side-effects.
        from grant_lab.admin_bootstrap import bootstrap_admins

        bootstrap_admins(
            app.state.session_factory,
            emails=app.state.settings.initial_admin_emails,
            allowed_domains=app.state.settings.allowed_email_domains,
            password=app.state.settings.initial_admin_password,
        )

        from grant_lab.ingestion.alerts import ConsecutiveFailureTracker
        from grant_lab.scheduler import build_scheduler

        settings = app.state.settings
        alerter = ConsecutiveFailureTracker(
            threshold=3,
            sender=app.state.email_sender,
            admin_email=settings.admin_alert_email,
            session_factory=app.state.session_factory,
        )
        app.state.ingestion_alerter = alerter

        from grant_lab.email.factory import EmailConfigError, build_email_client

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

        import asyncio as _asyncio

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
            # board_limiter (Plan C Task 7) rides the same sweep: `allow()`
            # never deletes a key on its own, only `sweep()` does, and the
            # public board route is the one anonymous route in the app, so
            # its limiter is the one most exposed to scan traffic filling
            # the dict with distinct IPs. Set on app.state before this
            # lifespan ever runs -- see the `app.state.board_limiter =`
            # assignment below in create_app().
            limiters=[app.state.email_limiter, app.state.ip_limiter, app.state.board_limiter],
            start=start_scheduler,
        )
        app.state.scheduler = scheduler
        # Ownership is checked at startup and then never again: `is_held()` had
        # exactly one caller, the /healthz field, which tells a monitor and stops
        # nothing. A lock lost mid-run therefore left this process firing every
        # tick while another instance could take the lock — the second scheduler
        # the single-instance constraint forbids. The watchdog acts on it.
        if scheduler_lock is not None:
            from grant_lab.scheduler import start_scheduler_lock_watchdog

            _watchdog_thread, _watchdog_stop = start_scheduler_lock_watchdog(
                scheduler_lock, scheduler
            )
        else:
            _watchdog_thread, _watchdog_stop = None, None
    except BaseException:
        if scheduler_lock is not None:
            scheduler_lock.release()
        raise
    try:
        yield
    finally:
        # wait=True so an in-flight rank-drain or ingest job completes against the
        # still-open pool (the rank tick runs every RANKING_TICK_SECONDS).
        #
        # Run the BLOCKING shutdown off the event loop (to_thread) so the loop is
        # not held for as long as the longest in-flight tick.
        #
        # No `running` guard needed: build_scheduler returns a
        # _SafeBackgroundScheduler, whose shutdown() is a documented no-op when the
        # scheduler was never started (the start=False path taken by whichever
        # replica loses the advisory lock).
        # Stop the ownership watchdog BEFORE the scheduler shutdown below, so a
        # tick landing mid-teardown cannot log a spurious "lock lost" for a lock
        # we are about to release on purpose.
        if _watchdog_stop is not None:
            _watchdog_stop.set()
            if _watchdog_thread is not None:
                await _asyncio.to_thread(_watchdog_thread.join, 5.0)
        await _asyncio.to_thread(scheduler.shutdown, wait=True)
        # Release the single-instance advisory lock (closes its dedicated
        # connection). Done after the scheduler stops so no in-flight job is
        # still relying on single-instance semantics. Must precede
        # engine.dispose() below — the lock holds its own connection.
        if scheduler_lock is not None:
            scheduler_lock.release()
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
# whitespace — so a value like "https://app.umu.se; script-src * 'unsafe-inline'"
# parses to a netloc of "app.umu.se; script-src * 'unsafe-inline'" and, absent
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
        raise RuntimeError(f"APP_BASE_URL host is not a valid host[:port]: {_base.netloc!r}")
    return f"{'wss' if _base.scheme == 'https' else 'ws'}://{_base.netloc}"


def _derive_form_action_origin(discovery_url: str) -> str:
    """The identity provider's origin, for the CSP form-action allowance.

    Taken from GOOGLE_OIDC_DISCOVERY_URL: Google serves discovery and the
    authorization endpoint from the same origin (accounts.google.com), and the
    endpoint itself is only known after a network fetch, which startup does not
    make. Validated like _derive_ws_origin -- the value is interpolated into the
    CSP header, so anything but a plain http(s)://host[:port] fails at startup.
    """
    u = urlsplit(discovery_url)
    if u.scheme not in ("https", "http") or not u.netloc or not _HOST_PORT_RE.match(u.netloc):
        raise RuntimeError(
            f"GOOGLE_OIDC_DISCOVERY_URL is not a plain http(s)://host[:port] URL: {discovery_url!r}"
        )
    return f"{u.scheme}://{u.netloc}"


def create_app() -> FastAPI:
    configure_logging()  # JSON from the first line, even if Settings() refuses to load
    settings = Settings()
    configure_logging(settings.log_level)  # idempotent: re-levels, keeps the handler

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
    # - A transport builds (RESEND_API_KEY, or SMTP_HOST, or EMAIL_TRANSPORT with
    #   its credential) → TransportEmailSender over the digest's own client
    #   (any environment). SMTP is an accepted production transport for sites
    #   that run their own relay (e.g. Postfix/SendGrid SMTP).
    # - No key, no SMTP, cookie_secure=False → ConsoleEmailSender (dev over plain HTTP).
    # - No key, no SMTP, cookie_secure=True  → NoOpEmailSender (prod). The email
    #   sender is now best-effort only (ingestion/digest alerts); login no longer
    #   depends on it (magic-link auth was removed). We drop alerts silently
    #   rather than console-log their bodies in prod — ConsoleEmailSender stays
    #   dev-only because it logs full message contents.
    # Tests override this with InMemoryEmailSender via app.state after creation.
    # ONE transport decision, shared with the digest client's factory. These used
    # to be two independent selectors over one setting: this one ignored
    # EMAIL_TRANSPORT while `build_email_client` read it (defaulting to
    # "resend"), so an SMTP-only deploy sent its alerts and silently never
    # scheduled the weekly digest. `resolve_email_transport` is now the only
    # place that answers "which transport".
    # Alerts and the digest now also share the delivery CODE: one client
    # builder, wrapped for plain-text alerts.
    from grant_lab.email.factory import EmailConfigError, build_email_client

    email_sender: EmailSender
    try:
        email_sender = TransportEmailSender(build_email_client(settings))
    except EmailConfigError:
        # No transport: console in dev over plain HTTP; in prod drop best-effort
        # alerts rather than refuse to start or console-log alert bodies.
        # (Magic-link login — the original reason for the hard requirement —
        # has been removed.)
        email_sender = ConsoleEmailSender() if not settings.cookie_secure else NoOpEmailSender()

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
    # The non-dev CSP adds one same-origin ws(s) token derived from APP_BASE_URL;
    # deriving it also validates the APP_BASE_URL host at startup (_derive_ws_origin).
    _ws_origin = _derive_ws_origin(settings.app_base_url)
    _csp = build_csp(
        dev=settings.spa_mode == "dev",
        ws_origin=_ws_origin,
        form_action_origin=(
            _derive_form_action_origin(settings.google_oidc_discovery_url)
            if settings.google_oidc_enabled
            else None
        ),
    )
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
            if (
                isinstance(exc.detail, dict)
                and isinstance(exc.detail.get("code"), str)
                and isinstance(exc.detail.get("message"), str)
            ):
                return api_error(
                    exc.status_code,
                    exc.detail["code"],
                    exc.detail["message"],
                    detail=exc.detail.get("detail"),
                    headers=hdrs,
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

    @app.exception_handler(OperationalError)
    async def _lock_timeout(request: Request, exc: OperationalError):
        # SQLSTATE 55P03 = lock_not_available: a `lock_timeout` fired. That is routine
        # contention, not a server fault, so it answers 429 + Retry-After instead of
        # 500. No route sets `lock_timeout` today (db/session.py does not either).
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
        # Routine contention: WARNING, not ERROR, but logged so it is discoverable.
        # Path only — never the query string or the request body.
        _logger.warning(
            "mutex_tail.lock_timeout",
            extra={
                "event": "mutex_tail_lock_timeout",
                "path": request.url.path,
            },
        )
        if api_request(request):
            return api_error(429, code_for(429), BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"})
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
        # json_safe_detail closes the one case it does NOT cover: a non-finite
        # float. `errors()` echoes the rejected `input`, and JSONResponse dumps
        # with allow_nan=False, so a NaN there raised INSIDE this handler — which
        # is exactly the 500 the line above exists to prevent. See its docstring
        # for the measurement.
        detail = json_safe_detail(jsonable_encoder(exc.errors()))
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

    from grant_lab.embeddings.calibration import check_threshold_calibration

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
    app.state.email_sender = email_sender
    app.state.email_limiter = SlidingWindowLimiter(
        max_requests=5, window_seconds=15 * 60, name="email"
    )
    app.state.ip_limiter = SlidingWindowLimiter(max_requests=30, window_seconds=15 * 60, name="ip")
    # The public corridor-board route has no session to key a limiter off, so
    # it buckets by IP like `ip_limiter` above but keeps its OWN counter:
    # sharing `ip_limiter` would let a burst of board views lock a hospital's
    # shared egress IP out of login. 60/15min is 20x headroom over one
    # legitimate screen's expected rate (Plan C D6: a 15-minute window over a
    # ~5-minute client cache is ~3 requests) while still bounding a scraper
    # that has no other rate-limit surface to hit.
    app.state.board_limiter = SlidingWindowLimiter(
        max_requests=60, window_seconds=15 * 60, name="board"
    )
    app.state.session_signer = SessionSigner(
        secret=settings.session_secret,
        max_age_seconds=settings.session_max_age_seconds,
    )
    app.state.oauth_txn_cipher = TransactionCipher(secret=settings.session_secret)
    google_oidc_client: OIDCClient | None = None
    if settings.google_oidc_enabled:
        # Settings validation already refuses an enabled Google OIDC without
        # both credentials; this narrows the Optional fields for the type checker.
        if not settings.google_client_id or not settings.google_client_secret:
            raise RuntimeError("GOOGLE_OIDC_ENABLED requires GOOGLE_CLIENT_ID and SECRET")
        google_oidc_client = OIDCClient(
            discovery_url=settings.google_oidc_discovery_url,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
        )
    app.state.google_oidc_client = google_oidc_client
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

    # Vendored static assets (only favicon.svg after the legacy UI deletion). Serving
    # locally avoids an unreviewed third-party CDN dependency on every page load,
    # which in a hospital-internal context is unacceptable — a CDN compromise would
    # execute attacker JS on every logged-in request.
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(health.router)
    app.include_router(oauth_google.router)
    app.include_router(build_api_router())

    # MUST be the LAST route registration: mount_spa (when SPA_MODE=prod)
    # installs a catch-all GET /{full_path:path} that returns the SPA index
    # only for manifest-owned routes (is_spa_route) and a real 404 otherwise.
    # Every real router above is registered first, so concrete routes always
    # win; an unmatched /api/* still raises HTTPException(404) and flows
    # through the Plan-1 JSON envelope handler.
    mount_spa(app, spa_mode=settings.spa_mode)
    return app
