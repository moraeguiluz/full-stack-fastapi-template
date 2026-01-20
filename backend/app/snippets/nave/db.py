import os
import datetime as dt

from fastapi import HTTPException
from sqlalchemy import create_engine
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


def new_session() -> Session:
    _init_db()
    if not _SessionLocal:
        raise HTTPException(status_code=503, detail="DB no configurada (falta DATABASE_URL)")
    return _SessionLocal()


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
