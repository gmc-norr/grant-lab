from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from call_monitor.config import Settings
from call_monitor.db.session import make_engine, make_session_factory
from call_monitor.logging import configure_logging
from call_monitor.web.routes import health

TEMPLATES_DIR = Path(__file__).parent / "web" / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup tasks belong here (admin bootstrap lands in Task 17).
    yield


def create_app() -> FastAPI:
    configure_logging()
    settings = Settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    app.include_router(health.router)
    return app
