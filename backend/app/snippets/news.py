# backend/app/snippets/news.py
from __future__ import annotations

import os
import datetime as dt
from typing import Optional, List

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field

from sqlalchemy import (
    create_engine,
    String,
    Integer,
    DateTime,
    Text,
    func,
    case,
    desc,
    and_,
    Index,
)
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column, Session

from app.seed.news_seed import SeedResult, seed_news

ENABLED = True
router = APIRouter(tags=["news"])

_DB_URL = os.getenv("DATABASE_URL")
_SECRET = os.getenv("SECRET_KEY", "dev-change-me")
_ALG = "HS256"

# Admin: abrir por defecto (mismo patrón que insignias)
_engine = None
_SessionLocal = None
_inited = False

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/finalize")


class Base(DeclarativeBase):
    pass


def _init_db():
    global _engine, _SessionLocal, _inited
    if _inited:
        return
    if not _DB_URL:
        raise HTTPException(status_code=503, detail="DB no configurada (falta DATABASE_URL)")
    url = _DB_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)

    _engine = create_engine(url, pool_pre_ping=True)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=_engine)
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


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _current_user_id(token: str = Depends(oauth2)) -> int:
    try:
        data = jwt.decode(token, _SECRET, algorithms=[_ALG])
    except Exception:
        raise HTTPException(401, "Token inválido")
    uid = data.get("sub")
    if not uid:
        raise HTTPException(401, "Token inválido")
    return int(uid)


def _require_admin(uid: int):
    # Por ahora, cualquier usuario autenticado puede usar admin de noticias.
    # Cuando se quiera restringir, se añade validación real de admin.
    return


class News(Base):
    __tablename__ = "app_news"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    body: Mapped[str] = mapped_column(Text)
    image_object_name: Mapped[Optional[str]] = mapped_column(String(600), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)  # draft|published
    priority: Mapped[int] = mapped_column(Integer, default=50, index=True)
    scope_type: Mapped[str] = mapped_column(String(20), default="global", index=True)  # global|state|city|codigo_base
    scope_value: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, index=True)

    pinned_until: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    published_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


Index("ix_news_status_priority", News.status, News.priority.desc())
Index("ix_news_scope", News.scope_type, News.scope_value, News.id.desc())


def _touch_updated(n: News):
    n.updated_at = _now()


class NewsOut(BaseModel):
    id: int
    title: str
    body: str
    summary: Optional[str] = None
    image_object_name: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[int] = None
    scope_type: Optional[str] = None
    scope_value: Optional[str] = None
    pinned_until: Optional[dt.datetime] = None
    published_at: Optional[dt.datetime] = None
    created_at: Optional[dt.datetime] = None
    updated_at: Optional[dt.datetime] = None

    class Config:
        from_attributes = True


class NewsFeedOut(BaseModel):
    items: List[NewsOut]
    next_before_id: Optional[int] = None


class NewsCreateIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=3)
    summary: Optional[str] = Field(default=None, max_length=4000)
    image_object_name: Optional[str] = Field(default=None, max_length=600)
    status: Optional[str] = Field(default="draft", max_length=20)
    priority: Optional[int] = Field(default=50, ge=0, le=100)
    scope_type: Optional[str] = Field(default="global", max_length=20)
    scope_value: Optional[str] = Field(default=None, max_length=80)
    pinned_until: Optional[dt.datetime] = None


class NewsPatchIn(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    body: Optional[str] = Field(default=None)
    summary: Optional[str] = Field(default=None, max_length=4000)
    image_object_name: Optional[str] = Field(default=None, max_length=600)
    status: Optional[str] = Field(default=None, max_length=20)
    priority: Optional[int] = Field(default=None, ge=0, le=100)
    scope_type: Optional[str] = Field(default=None, max_length=20)
    scope_value: Optional[str] = Field(default=None, max_length=80)
    pinned_until: Optional[dt.datetime] = None
    published_at: Optional[dt.datetime] = None


class SeedNewsOut(BaseModel):
    created: int
    skipped: int
    total: int


@router.get("/news", response_model=NewsFeedOut)
def get_news_feed(
    limit: int = Query(default=20, ge=1, le=100),
    before_id: Optional[int] = Query(default=None, ge=1),
    codigo_base: Optional[str] = Query(default=None),
    token: str = Depends(oauth2),
    db: Session = Depends(get_db),
):
    _ = _current_user_id(token)
    now = _now()

    q = db.query(News).filter(News.status == "published")

    if before_id is not None:
        q = q.filter(News.id < before_id)

    # Alcance: global siempre entra; codigo_base debe coincidir con scope_value.
    if codigo_base and codigo_base.strip():
        cb = codigo_base.strip()
        q = q.filter(
            (News.scope_type == "global") |
            ((News.scope_type == "codigo_base") & (News.scope_value == cb))
        )
    else:
        q = q.filter(News.scope_type == "global")

    # Score: prioridad + bonus por scope + bonus por frescura
    scope_bonus = case(
        (and_(News.scope_type == "codigo_base", News.scope_value == (codigo_base or "")), 30),
        else_=0,
    )
    # Frescura: hasta +20 en primeras 20 horas
    ref_dt = func.coalesce(News.published_at, News.created_at)
    hours_since = func.extract("epoch", now - ref_dt) / 3600.0
    recency_bonus = func.greatest(0, 20 - hours_since)

    score = (func.coalesce(News.priority, 0) + scope_bonus + recency_bonus).label("score")
    pinned = case((News.pinned_until.is_not(None) & (News.pinned_until > now), 1), else_=0).label("pinned")

    rows = q.add_columns(score, pinned).order_by(desc(pinned), desc(score), News.id.desc()).limit(limit).all()
    items = [r[0] for r in rows]
    next_before = items[-1].id if items else None
    return NewsFeedOut(items=items, next_before_id=next_before)


@router.get("/news/{news_id:int}", response_model=NewsOut)
def get_news(news_id: int, token: str = Depends(oauth2), db: Session = Depends(get_db)):
    _ = _current_user_id(token)
    item = db.query(News).filter(News.id == news_id, News.status == "published").first()
    if not item:
        raise HTTPException(404, "Noticia no encontrada")
    return item


@router.get("/news/admin", response_model=List[NewsOut])
def admin_list_news(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    token: str = Depends(oauth2),
    db: Session = Depends(get_db),
):
    uid = _current_user_id(token)
    _require_admin(uid)

    items = db.query(News).order_by(News.id.desc()).offset(offset).limit(limit).all()
    return items


@router.get("/news/admin/{news_id:int}", response_model=NewsOut)
def admin_get_news(news_id: int, token: str = Depends(oauth2), db: Session = Depends(get_db)):
    uid = _current_user_id(token)
    _require_admin(uid)

    item = db.query(News).filter(News.id == news_id).first()
    if not item:
        raise HTTPException(404, "Noticia no encontrada")
    return item


@router.post("/news/admin", response_model=NewsOut)
def admin_create_news(
    body: NewsCreateIn,
    token: str = Depends(oauth2),
    db: Session = Depends(get_db),
):
    uid = _current_user_id(token)
    _require_admin(uid)

    item = News(
        title=body.title.strip(),
        body=body.body.strip(),
        summary=body.summary.strip() if body.summary else None,
        image_object_name=body.image_object_name.strip() if body.image_object_name else None,
        status=(body.status or "draft"),
        priority=body.priority if body.priority is not None else 50,
        scope_type=(body.scope_type or "global"),
        scope_value=body.scope_value.strip() if body.scope_value else None,
        pinned_until=body.pinned_until,
    )
    if item.status == "published":
        item.published_at = _now()
    _touch_updated(item)

    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.patch("/news/admin/{news_id:int}", response_model=NewsOut)
def admin_patch_news(
    news_id: int,
    body: NewsPatchIn,
    token: str = Depends(oauth2),
    db: Session = Depends(get_db),
):
    uid = _current_user_id(token)
    _require_admin(uid)

    item = db.query(News).filter(News.id == news_id).first()
    if not item:
        raise HTTPException(404, "Noticia no encontrada")

    if body.title is not None:
        item.title = body.title.strip()
    if body.body is not None:
        item.body = body.body.strip()
    if body.summary is not None:
        item.summary = body.summary.strip() or None
    if body.image_object_name is not None:
        item.image_object_name = body.image_object_name.strip() or None
    if body.status is not None:
        item.status = body.status
    if body.priority is not None:
        item.priority = body.priority
    if body.scope_type is not None:
        item.scope_type = body.scope_type
    if body.scope_value is not None:
        item.scope_value = body.scope_value.strip() or None
    if body.pinned_until is not None:
        item.pinned_until = body.pinned_until
    if body.published_at is not None:
        item.published_at = body.published_at
    if item.status == "published" and item.published_at is None:
        item.published_at = _now()

    _touch_updated(item)
    db.commit()
    db.refresh(item)
    return item


@router.post("/news/admin/seed", response_model=SeedNewsOut)
def admin_seed_news(
    token: str = Depends(oauth2),
    db: Session = Depends(get_db),
) -> SeedNewsOut:
    uid = _current_user_id(token)
    _require_admin(uid)

    try:
        result: SeedResult = seed_news(db)
    except Exception as exc:
        raise HTTPException(500, f"No se pudo generar noticias demo: {exc}")

    return SeedNewsOut(created=result.created, skipped=result.skipped, total=result.total)
