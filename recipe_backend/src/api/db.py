import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Generator, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


@dataclass(frozen=True)
class DatabaseConfig:
    """Database configuration.

    Contract:
      - db_url: SQLAlchemy database URL (postgresql://...)
    """

    db_url: str


def _normalize_psql_connection_string(raw: str) -> str:
    """Convert db_connection.txt content into a SQLAlchemy connection URL.

    The db_connection.txt often contains: "psql postgresql://user:pass@host:port/db"
    We normalize by removing leading 'psql ' if present.

    Raises:
      ValueError: if the string doesn't look like a PostgreSQL URL.
    """
    s = raw.strip()
    if s.startswith("psql "):
        s = s[len("psql ") :].strip()

    if not (s.startswith("postgresql://") or s.startswith("postgres://")):
        raise ValueError(
            "db_connection.txt must contain a PostgreSQL URL (optionally prefixed with 'psql ')."
        )
    # SQLAlchemy prefers postgresql://; both work with psycopg2.
    return s.replace("postgres://", "postgresql://", 1)


# PUBLIC_INTERFACE
def load_database_config() -> DatabaseConfig:
    """Load DB config by reading the cross-container db_connection.txt.

    Contract:
      Inputs:
        - env var RECIPE_DB_CONNECTION_FILE (optional): path to db_connection.txt
      Outputs:
        - DatabaseConfig with db_url
      Errors:
        - FileNotFoundError, ValueError on missing/invalid content

    Side effects:
      - Reads a local file from disk.
    """
    # NOTE: this backend container must read the connection string from recipe_database/db_connection.txt
    # per project rule. We allow overriding path for deployments/tests.
    default_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "recipe-hub-335233-335248", "recipe_database", "db_connection.txt")
    )
    path = os.environ.get("RECIPE_DB_CONNECTION_FILE", default_path)

    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    return DatabaseConfig(db_url=_normalize_psql_connection_string(raw))


_engine = None
_SessionLocal: Optional[sessionmaker] = None


# PUBLIC_INTERFACE
def init_engine(db_url: str) -> None:
    """Initialize SQLAlchemy engine + session factory.

    Contract:
      - Must be called once on app startup before using get_db().
      - Safe to call multiple times with same URL (idempotent).

    Side effects:
      - Creates DB engine and connection pool.
    """
    global _engine, _SessionLocal
    if _engine is not None and _SessionLocal is not None:
        return

    _engine = create_engine(
        db_url,
        pool_pre_ping=True,
    )
    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


# PUBLIC_INTERFACE
def get_engine():
    """Return the initialized engine.

    Raises:
      RuntimeError: if init_engine was not called.
    """
    if _engine is None:
        raise RuntimeError("Database engine is not initialized. Call init_engine() at startup.")
    return _engine


# PUBLIC_INTERFACE
def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a DB session.

    Contract:
      - Yields an active Session.
      - Commits are the responsibility of the caller/service layer.
      - Always closes the session.

    Raises:
      RuntimeError: if init_engine was not called.
    """
    if _SessionLocal is None:
        raise RuntimeError("DB session factory not initialized. Call init_engine() at startup.")

    db = _SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def db_session() -> Generator[Session, None, None]:
    """Context manager to use DB session outside dependency injection."""
    if _SessionLocal is None:
        raise RuntimeError("DB session factory not initialized. Call init_engine() at startup.")
    db = _SessionLocal()
    try:
        yield db
    finally:
        db.close()
