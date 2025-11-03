# backend/app/snippets/visitas.py
from __future__ import annotations

import os, datetime as dt, jwt
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field

from sqlalchemy import (
    create_engine, select, and_, or_, func, Float, Integer, String, DateTime, Boolean, UniqueConstraint, Table, MetaData
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session, sessionmaker

router = APIRouter(prefix="/visitas", tags=["visitas"])

# -------- Config & lazy init --------
_DB_URL = os.getenv("DATABASE_URL")
_SECRET = os.getenv("SECRET_KEY", "dev-change-me")
_ALG = "HS256"

_engine = None
_SessionLocal: Optional[sessionmaker] = None
_inited = False

def _init_db():
    global _engine, _SessionLocal, _inited
    if _inited:
        return
    if not _DB_URL:
        raise HTTPException(503, "DB no configurada (falta DATABASE_URL)")
    url = _DB_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    _engine = create_engine(url, pool_pre_ping=True)
    Base.metadata.create_all(bind=_engine)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    _inited = True

def get_db():
    _init_db()
    assert _SessionLocal is not None
    db: Session = _SessionLocal()
    try:
        yield db
    finally:
        db.close()

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/finalize")

def _current_user_id(token: str = Depends(oauth2)) -> int:
    try:
        data = jwt.decode(token, _SECRET, algorithms=[_ALG])
    except Exception:
        raise HTTPException(401, "Token inválido")
    uid = data.get("sub")
    if not uid:
        raise HTTPException(401, "Token inválido")
    return int(uid)

def _ensure_tz(ts: Optional[dt.datetime]) -> Optional[dt.datetime]:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)

# -------- Declarative --------
class Base(DeclarativeBase):
    pass

class Visit(Base):
    __tablename__ = "app_visita"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    nombre: Mapped[str] = mapped_column(String(120), default="")
    apellido_paterno: Mapped[str] = mapped_column(String(120), default="")
    apellido_materno: Mapped[str] = mapped_column(String(120), default="")
    telefono: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True, index=True)
    lng: Mapped[Optional[float]] = mapped_column(Float, nullable=True, index=True)
    hora: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    adultos: Mapped[int] = mapped_column(Integer, default=0)
    notas: Mapped[str] = mapped_column(String(2000), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

# Mapeo mínimo para joins (no crea tabla, se usa para filtro de equipo)
class UserCoord(Base):
    __tablename__ = "app_user_coord"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    coordinador_id: Mapped[int] = mapped_column(Integer, index=True)
    miembro_id: Mapped[int] = mapped_column(Integer, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)

class AppUser(Base):
    __tablename__ = "app_user_auth"
    __table_args__ = {"extend_existing": True}
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[Optional[str]] = mapped_column(String(120))
    apellido_paterno: Mapped[Optional[str]] = mapped_column(String(120))
    apellido_materno: Mapped[Optional[str]] = mapped_column(String(120))

# -------- Pydantic Schemas --------
class VisitCreate(BaseModel):
    nombre: str = Field(min_length=1, max_length=120)
    apellido_paterno: Optional[str] = Field(default=None, max_length=120)
    apellido_materno: Optional[str] = Field(default=None, max_length=120)
    telefono: Optional[str] = Field(default=None, max_length=32)
    lat: Optional[float] = None
    lng: Optional[float] = None
    hora: Optional[dt.datetime] = None
    adultos: Optional[int] = Field(default=None, ge=0, le=50)
    notas: Optional[str] = Field(default=None, max_length=2000)

class VisitOut(BaseModel):
    id: int
    user_id: int
    nombre: str
    apellido_paterno: str
    apellido_materno: str
    telefono: Optional[str]
    lat: Optional[float]
    lng: Optional[float]
    hora: dt.datetime
    adultos: int
    notas: str
    created_at: Optional[dt.datetime] = None
    updated_at: Optional[dt.datetime] = None
    registrador_id: Optional[int] = None
    registrador_nombre: Optional[str] = None

    class Config:
        from_attributes = True

class VisitListOut(BaseModel):
    items: List[VisitOut]
    total: int

class VisitPatch(BaseModel):
    nombre: Optional[str] = Field(default=None, min_length=1, max_length=120)
    apellido_paterno: Optional[str] = Field(default=None, max_length=120)
    apellido_materno: Optional[str] = Field(default=None, max_length=120)
    telefono: Optional[str] = Field(default=None, max_length=32)
    lat: Optional[float] = None
    lng: Optional[float] = None
    hora: Optional[dt.datetime] = None
    adultos: Optional[int] = Field(default=None, ge=0, le=50)
    notas: Optional[str] = Field(default=None, max_length=2000)

# -------- Endpoints --------
@router.post("", response_model=VisitOut)
def crear_visita(
    payload: VisitCreate,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    v = Visit(
        user_id=uid,
        nombre=payload.nombre.strip(),
        apellido_paterno=(payload.apellido_paterno or "").strip(),
        apellido_materno=(payload.apellido_materno or "").strip(),
        telefono=(payload.telefono or None),
        lat=payload.lat,
        lng=payload.lng,
        hora=_ensure_tz(payload.hora) or dt.datetime.now(dt.timezone.utc),
        adultos=payload.adultos if payload.adultos is not None else 0,
        notas=(payload.notas or "").strip(),
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    # registrar campos extra del registrador en la salida
    reg = db.get(AppUser, uid)
    return VisitOut(
        **{c: getattr(v, c) for c in [
            "id","user_id","nombre","apellido_paterno","apellido_materno",
            "telefono","lat","lng","hora","adultos","notas","created_at","updated_at"
        ]},
        registrador_id=uid,
        registrador_nombre=" ".join(p for p in [reg.nombre if reg else None, reg.apellido_paterno if reg else None, reg.apellido_materno if reg else None] if p),
    )

@router.get("", response_model=VisitListOut)
def listar_visitas(
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    from_dt: Optional[dt.datetime] = Query(None, alias="from"),
    to_dt: Optional[dt.datetime] = Query(None, alias="to"),
    with_location: Optional[bool] = Query(None, description="true=solo con lat/lng; false=solo sin; null=todos"),
    include_team: bool = Query(False, description="Incluir visitas de miembros a quienes coordino"),
    team_selected_only: bool = Query(True, description="Si include_team, incluir solo miembros marcados selected=true"),
):
    from_dt = _ensure_tz(from_dt)
    to_dt = _ensure_tz(to_dt)

    # Base: mis propias visitas
    conds = [Visit.user_id == uid]
    if from_dt:
        conds.append(Visit.hora >= from_dt)
    if to_dt:
        conds.append(Visit.hora < to_dt)
    if with_location is True:
        conds += [Visit.lat.isnot(None), Visit.lng.isnot(None)]
    elif with_location is False:
        conds += [Visit.lat.is_(None), Visit.lng.is_(None)]

    if include_team:
        # miembros a los que coordino y (opcional) selected
        q_team = select(UserCoord.miembro_id).where(
            UserCoord.coordinador_id == uid,
            UserCoord.is_active == True  # noqa: E712
        )
        if team_selected_only:
            q_team = q_team.where(UserCoord.selected == True)  # noqa: E712
        member_ids = [r[0] for r in db.execute(q_team).all()]
        if member_ids:
            conds = [or_(Visit.user_id == uid, Visit.user_id.in_(member_ids))]

            # respetar filtros de tiempo/ubicación también para el equipo
            if from_dt:
                conds.append(Visit.hora >= from_dt)
            if to_dt:
                conds.append(Visit.hora < to_dt)
            if with_location is True:
                conds += [Visit.lat.isnot(None), Visit.lng.isnot(None)]
            elif with_location is False:
                conds += [Visit.lat.is_(None), Visit.lng.is_(None)]

    base_q = select(Visit).where(and_(*conds)).order_by(Visit.hora.desc())

    total = db.execute(
        select(func.count()).select_from(base_q.subquery())
    ).scalar_one()

    items = db.execute(base_q.limit(limit).offset(offset)).scalars().all()

    # Adjuntar registrador_* en la salida
    user_cache: dict[int, str] = {}
    out_items: List[VisitOut] = []
    for v in items:
        nm = user_cache.get(v.user_id)
        if nm is None:
            u = db.get(AppUser, v.user_id)
            nm = " ".join(p for p in [u.nombre if u else None, u.apellido_paterno if u else None, u.apellido_materno if u else None] if p)
            user_cache[v.user_id] = nm
        out_items.append(VisitOut(
            **{c: getattr(v, c) for c in [
                "id","user_id","nombre","apellido_paterno","apellido_materno",
                "telefono","lat","lng","hora","adultos","notas","created_at","updated_at"
            ]},
            registrador_id=v.user_id,
            registrador_nombre=nm or None,
        ))

    return VisitListOut(items=out_items, total=total)

@router.get("/{visita_id:int}", response_model=VisitOut)
def obtener_visita(
    visita_id: int,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    v = db.get(Visit, visita_id)
    if not v:
        raise HTTPException(404, "Visita no encontrada")

    if v.user_id != uid:
        # Permitir a coordinador activo ver visita de su miembro (no forzamos selected aquí)
        rel = db.execute(
            select(UserCoord).where(
                UserCoord.coordinador_id == uid,
                UserCoord.is_active == True,  # noqa: E712
                UserCoord.miembro_id == v.user_id,
            )
        ).scalars().first()
        if not rel:
            raise HTTPException(403, "No autorizado")

    u = db.get(AppUser, v.user_id)
    nm = " ".join(p for p in [u.nombre if u else None, u.apellido_paterno if u else None, u.apellido_materno if u else None] if p)
    return VisitOut(
        **{c: getattr(v, c) for c in [
            "id","user_id","nombre","apellido_paterno","apellido_materno",
            "telefono","lat","lng","hora","adultos","notas","created_at","updated_at"
        ]},
        registrador_id=v.user_id,
        registrador_nombre=nm or None,
    )

@router.patch("/{visita_id:int}", response_model=VisitOut)
def actualizar_visita(
    visita_id: int,
    payload: VisitPatch,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    v = db.get(Visit, visita_id)
    if not v:
        raise HTTPException(404, "Visita no encontrada")
    if v.user_id != uid:
        raise HTTPException(403, "No autorizado")

    # aplicar cambios
    if payload.nombre is not None: v.nombre = payload.nombre.strip()
    if payload.apellido_paterno is not None: v.apellido_paterno = payload.apellido_paterno.strip()
    if payload.apellido_materno is not None: v.apellido_materno = payload.apellido_materno.strip()
    if payload.telefono is not None: v.telefono = payload.telefono
    if payload.lat is not None: v.lat = payload.lat
    if payload.lng is not None: v.lng = payload.lng
    if payload.hora is not None: v.hora = _ensure_tz(payload.hora) or v.hora
    if payload.adultos is not None: v.adultos = payload.adultos
    if payload.notas is not None: v.notas = payload.notas.strip()

    db.commit()
    db.refresh(v)

    u = db.get(AppUser, v.user_id)
    nm = " ".join(p for p in [u.nombre if u else None, u.apellido_paterno if u else None, u.apellido_materno if u else None] if p)
    return VisitOut(
        **{c: getattr(v, c) for c in [
            "id","user_id","nombre","apellido_paterno","apellido_materno",
            "telefono","lat","lng","hora","adultos","notas","created_at","updated_at"
        ]},
        registrador_id=v.user_id,
        registrador_nombre=nm or None,
    )
