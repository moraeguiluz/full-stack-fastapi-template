import os
import datetime as dt

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Session

_DB_URL = os.getenv("DATABASE_URL")

_engine = None
_SessionLocal = None
_inited = False


class BaseOwn(DeclarativeBase):
    """Tablas propias de este snippet."""


class BaseRO(DeclarativeBase):
    """Tablas existentes (NO se crean aqui)."""


def _init_db() -> None:
    global _engine, _SessionLocal, _inited
    if _inited or not _DB_URL:
        return

    url = _DB_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)

    _engine = create_engine(url, pool_pre_ping=True)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)

    from . import models as _models  # evita import circular

    BaseOwn.metadata.create_all(bind=_engine)
    _ensure_columns(_engine)
    _inited = True


def get_db():
    _init_db()
    if not _SessionLocal:
        raise HTTPException(status_code=503, detail="DB no configurada (falta DATABASE_URL)")
    db: Session = _SessionLocal()
    try:
        yield db
    finally:
        db.close()


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _column_exists(conn, table: str, column: str) -> bool:
    q = text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c LIMIT 1"
    )
    return conn.execute(q, {"t": table, "c": column}).first() is not None


def _ensure_columns(engine) -> None:
    with engine.begin() as conn:
        # app_message new columns
        if not _column_exists(conn, "app_message", "delivered_at"):
            conn.execute(text("ALTER TABLE app_message ADD COLUMN delivered_at TIMESTAMPTZ NULL"))
        if not _column_exists(conn, "app_message", "read_at"):
            conn.execute(text("ALTER TABLE app_message ADD COLUMN read_at TIMESTAMPTZ NULL"))
        if not _column_exists(conn, "app_message", "is_deleted"):
            conn.execute(text("ALTER TABLE app_message ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT FALSE"))
        if not _column_exists(conn, "app_message", "deleted_at"):
            conn.execute(text("ALTER TABLE app_message ADD COLUMN deleted_at TIMESTAMPTZ NULL"))

        # app_message_thread new columns (groups)
        if not _column_exists(conn, "app_message_thread", "is_group"):
            conn.execute(text("ALTER TABLE app_message_thread ADD COLUMN is_group BOOLEAN NOT NULL DEFAULT FALSE"))
        if not _column_exists(conn, "app_message_thread", "group_name"):
            conn.execute(text("ALTER TABLE app_message_thread ADD COLUMN group_name VARCHAR(120) NULL"))
