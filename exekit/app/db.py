"""Database setup: SQLModel engine, session dependency, table creation."""

from sqlmodel import Session as DBSession  # renamed to avoid clashing with our session concept
from sqlmodel import SQLModel, create_engine
from sqlalchemy.event import listens_for

from app.config import get_settings

_settings = get_settings()

# check_same_thread=False is required for SQLite + FastAPI's threadpool.
connect_args = {"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {}
engine = create_engine(_settings.database_url, echo=False, connect_args=connect_args)


@listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _record):
    # The credit debit does a read-modify-write; without WAL, concurrent
    # executions on separate connections can both pass the credits>=1 check.
    if _settings.database_url.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def create_all() -> None:
    """Create all tables. Called from the lifespan handler (no Alembic in v1)."""
    # Import models so SQLModel sees them before create_all runs.
    import app.models  # noqa: F401

    SQLModel.metadata.create_all(engine)


def get_session():
    with DBSession(engine) as session:
        yield session
