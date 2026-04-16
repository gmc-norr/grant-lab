# Importing models here registers every model with Base.metadata as a side
# effect. Anything that imports from `call_monitor.db.*` (notably alembic/env.py
# via `from call_monitor.db.base import Base`) triggers this package init,
# which means `Base.metadata.tables` is populated when Alembic resolves
# `target_metadata`. Without this, `alembic revision --autogenerate` produces
# empty diffs.
from call_monitor.db import models  # noqa: F401
