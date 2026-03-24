from __future__ import annotations

import os
import datetime as dt
from typing import Optional, List, Dict, Any, Literal

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field

from sqlalchemy import (
    create_engine,
    String,
    Integer,
    Float,
    DateTime,
    Text,
    func,
    select,
    and_,
    UniqueConstraint,
    Index,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column, Session

ENABLED = True
router = APIRouter(prefix="/lonas", tags=["lonas"])

_DB_URL = os.getenv("DATABASE_URL")
_SECRET = os.getenv("SECRET_KEY", "dev-change-me")
_ALG = "HS256"

_engine = None
_SessionLocal = None
_inited = False

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/finalize")


class BaseOwn(DeclarativeBase):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _ensure_tz(value: Optional[dt.datetime]) -> Optional[dt.datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _init_db():
    global _engine, _SessionLocal, _inited
    if _inited or not _DB_URL:
        return

    url = _DB_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)

    _engine = create_engine(url, pool_pre_ping=True)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    BaseOwn.metadata.create_all(bind=_engine)

    try:
        with _engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
            conn.exec_driver_sql(
                """
                CREATE INDEX IF NOT EXISTS ix_lona_geo_gix
                ON app_lona
                USING GIST ((ST_SetSRID(ST_MakePoint(lng, lat), 4326)))
                WHERE lat IS NOT NULL AND lng IS NOT NULL
                """
            )
    except Exception:
        pass

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


def _decode_uid(token: str) -> int:
    try:
        data = jwt.decode(token, _SECRET, algorithms=[_ALG])
        uid = data.get("sub")
        if not uid:
            raise HTTPException(401, "Token inválido")
        return int(uid)
    except jwt.PyJWTError:
        raise HTTPException(401, "Token inválido")


def _current_user_id(token: str = Depends(oauth2)) -> int:
    return _decode_uid(token)


LonaTipo = Literal["lona", "anuncio", "otro"]
LonaStatus = Literal["placed", "removed"]


class Lona(BaseOwn):
    __tablename__ = "app_lona"
    __table_args__ = (
        UniqueConstraint("user_id", "client_ref", name="uq_lona_user_client_ref"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    client_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    codigo_base: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    tipo: Mapped[str] = mapped_column(String(24), default="lona", index=True)
    titulo: Mapped[str] = mapped_column(String(180), default="")
    descripcion: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notas: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_object_name: Mapped[str] = mapped_column(String(600))
    lat: Mapped[float] = mapped_column(Float, index=True)
    lng: Mapped[float] = mapped_column(Float, index=True)
    accuracy_m: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="placed", index=True)
    placed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    extra: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


Index("ix_lona_user_placed", Lona.user_id, Lona.placed_at.desc())
Index("ix_lona_codigo_placed", Lona.codigo_base, Lona.placed_at.desc())
Index("ix_lona_tipo_placed", Lona.tipo, Lona.placed_at.desc())


class LonaCreateIn(BaseModel):
    client_ref: Optional[str] = Field(default=None, max_length=64)
    codigo_base: Optional[str] = Field(default=None, max_length=64)
    tipo: LonaTipo = "lona"
    titulo: str = Field(min_length=1, max_length=180)
    descripcion: Optional[str] = None
    notas: Optional[str] = None
    image_object_name: str = Field(min_length=1, max_length=600)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy_m: Optional[int] = Field(default=None, ge=0, le=100000)
    placed_at: Optional[dt.datetime] = None
    status: LonaStatus = "placed"
    extra: Dict[str, Any] = Field(default_factory=dict)


class LonaOut(BaseModel):
    id: int
    user_id: int
    client_ref: Optional[str] = None
    codigo_base: Optional[str] = None
    tipo: str
    titulo: str
    descripcion: Optional[str] = None
    notas: Optional[str] = None
    image_object_name: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    accuracy_m: Optional[int] = None
    placed_at: Optional[dt.datetime] = None
    status: Optional[str] = None
    extra: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[dt.datetime] = None
    updated_at: Optional[dt.datetime] = None

    class Config:
        from_attributes = True


class LonaListOut(BaseModel):
    items: List[LonaOut]
    total: int


class GeoPoint(BaseModel):
    type: Literal["Point"] = "Point"
    coordinates: List[float]


class Feature(BaseModel):
    type: Literal["Feature"] = "Feature"
    geometry: GeoPoint
    properties: Dict[str, Any] = Field(default_factory=dict)


class FeatureCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: List[Feature]


def _base_stmt(
    uid: int,
    *,
    from_dt: Optional[dt.datetime],
    to_dt: Optional[dt.datetime],
    tipo: Optional[str],
    codigo_base: Optional[str],
):
    conds = [Lona.user_id == uid]
    if from_dt is not None:
        conds.append(Lona.placed_at >= from_dt)
    if to_dt is not None:
        conds.append(Lona.placed_at < to_dt)
    if tipo:
        conds.append(Lona.tipo == tipo)
    if codigo_base:
        conds.append(Lona.codigo_base == codigo_base)
    return select(Lona).where(and_(*conds))


@router.post("", response_model=LonaOut)
def create_lona(
    payload: LonaCreateIn,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    if payload.client_ref:
        existing = db.execute(
            select(Lona).where(
                Lona.user_id == uid,
                Lona.client_ref == payload.client_ref,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    item = Lona(
        user_id=uid,
        client_ref=(payload.client_ref or None),
        codigo_base=(payload.codigo_base or None),
        tipo=payload.tipo,
        titulo=payload.titulo.strip(),
        descripcion=(payload.descripcion or None),
        notas=(payload.notas or None),
        image_object_name=payload.image_object_name.strip(),
        lat=payload.lat,
        lng=payload.lng,
        accuracy_m=payload.accuracy_m,
        placed_at=_ensure_tz(payload.placed_at) or _now(),
        status=payload.status,
        extra=payload.extra or {},
    )
    db.add(item)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if payload.client_ref:
            existing = db.execute(
                select(Lona).where(
                    Lona.user_id == uid,
                    Lona.client_ref == payload.client_ref,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
        raise HTTPException(409, "No fue posible crear la lona")
    db.refresh(item)
    return item


@router.get("", response_model=LonaListOut)
def list_lonas(
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    from_dt: Optional[dt.datetime] = Query(None, alias="from"),
    to_dt: Optional[dt.datetime] = Query(None, alias="to"),
    tipo: Optional[str] = Query(None),
    codigo_base: Optional[str] = Query(None),
):
    stmt = _base_stmt(
        uid,
        from_dt=_ensure_tz(from_dt),
        to_dt=_ensure_tz(to_dt),
        tipo=(tipo.strip() if tipo else None),
        codigo_base=(codigo_base.strip() if codigo_base else None),
    )
    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    items = db.execute(
        stmt.order_by(Lona.placed_at.desc(), Lona.id.desc()).limit(limit).offset(offset)
    ).scalars().all()
    return LonaListOut(items=list(items), total=total)


@router.get("/geo/points", response_model=FeatureCollection)
def list_lonas_geo_points(
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
    limit: int = Query(10000, ge=1, le=50000),
    from_dt: Optional[dt.datetime] = Query(None, alias="from"),
    to_dt: Optional[dt.datetime] = Query(None, alias="to"),
    tipo: Optional[str] = Query(None),
    codigo_base: Optional[str] = Query(None),
):
    stmt = _base_stmt(
        uid,
        from_dt=_ensure_tz(from_dt),
        to_dt=_ensure_tz(to_dt),
        tipo=(tipo.strip() if tipo else None),
        codigo_base=(codigo_base.strip() if codigo_base else None),
    )
    items = db.execute(
        stmt.order_by(Lona.placed_at.desc(), Lona.id.desc()).limit(limit)
    ).scalars().all()

    features = [
        Feature(
            geometry=GeoPoint(coordinates=[float(item.lng), float(item.lat)]),
            properties={
                "id": item.id,
                "user_id": item.user_id,
                "tipo": item.tipo,
                "titulo": item.titulo,
                "status": item.status,
                "placed_at": item.placed_at.isoformat() if item.placed_at else None,
                "image_object_name": item.image_object_name,
            },
        )
        for item in items
    ]
    return FeatureCollection(features=features)


@router.get("/{lona_id:int}", response_model=LonaOut)
def get_lona(
    lona_id: int,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    item = db.execute(
        select(Lona).where(
            Lona.id == lona_id,
            Lona.user_id == uid,
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "Lona no encontrada")
    return item
