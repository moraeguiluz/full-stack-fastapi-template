# backend/app/snippets/visitas_coordinacion.py
from __future__ import annotations

import os, re, datetime as dt, jwt
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field

from sqlalchemy import (
    create_engine, select, and_, func, UniqueConstraint,
    Integer, String, Boolean, DateTime, ForeignKey, MetaData, Table
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session, sessionmaker

router = APIRouter(prefix="/coordinadores", tags=["coordinadores"])

# -------- Config & lazy init (igual a tus snippets) --------
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
    Base.metadata.create_all(bind=_engine)      # crea solo tablas de este snippet
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

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def _clean_phone(phone: str) -> str:
    s = re.sub(r"[^\d+]", "", (phone or "").strip())
    if not s:
        return ""
    if s.startswith("+"):
        return s
    if len(s) == 10:
        return "+52" + s
    if s.startswith("52") and len(s) == 12:
        return "+" + s
    return "+" + s

# -------- Declarative (solo para este snippet) --------
class Base(DeclarativeBase):
    pass

class AppUser(Base):
    __tablename__ = "app_user_auth"
    __table_args__ = {"extend_existing": True}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nombre: Mapped[Optional[str]] = mapped_column(String(120))
    apellido_paterno: Mapped[Optional[str]] = mapped_column(String(120))
    apellido_materno: Mapped[Optional[str]] = mapped_column(String(120))
    telefono: Mapped[Optional[str]] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

class UserCoord(Base):
    """
    Relación: un MIEMBRO puede registrar 1..N COORDINADORES.
    Un COORDINADOR puede marcar a qué MIEMBROS (`selected`) desea ver en lista/mapa.
    """
    __tablename__ = "app_user_coord"
    __table_args__ = (UniqueConstraint("coordinador_id", "miembro_id", name="uq_app_user_coord_pair"),)
    id: Mapped[int]             = mapped_column(Integer, primary_key=True, autoincrement=True)
    coordinador_id: Mapped[int] = mapped_column(Integer, ForeignKey("app_user_auth.id"), index=True)
    miembro_id: Mapped[int]     = mapped_column(Integer, index=True)
    is_active: Mapped[bool]     = mapped_column(Boolean, default=True)    # controlado por el MIEMBRO
    selected: Mapped[bool]      = mapped_column(Boolean, default=False)   # controlado por el COORDINADOR
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

# -------- Schemas --------
class AddCoordinadorIn(BaseModel):
    telefono: str = Field(..., min_length=7, max_length=32)

class AddCoordinadorOut(BaseModel):
    ok: bool
    coordinador_id: Optional[int] = None
    already_linked: Optional[bool] = None

class CoordinadorOut(BaseModel):
    coordinador_id: int
    nombre: Optional[str] = None
    telefono: Optional[str] = None
    activo: bool

class MiembroOut(BaseModel):
    miembro_id: int
    nombre: Optional[str] = None
    apellido_paterno: Optional[str] = None
    apellido_materno: Optional[str] = None
    telefono: Optional[str] = None
    selected: bool
    activo: bool

class UpdateMemberBody(BaseModel):
    selected: Optional[bool] = None   # coordinador marca/ desmarca visibilidad
    activo:   Optional[bool] = None   # coordinador puede pausar el vínculo si quiere

# -------- Endpoints --------

@router.post("", response_model=AddCoordinadorOut)
def add_coordinador(
    payload: AddCoordinadorIn,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    tel = _clean_phone(payload.telefono)
    if not tel or not tel.startswith("+"):
        raise HTTPException(400, "Teléfono inválido, usa formato internacional ej. +527771234567")

    coor = db.execute(select(AppUser).where(AppUser.telefono == tel, AppUser.is_active == True)).scalars().first()  # noqa: E712
    if not coor:
        raise HTTPException(404, "No se encontró un usuario activo con ese teléfono.")

    if coor.id == uid:
        raise HTTPException(400, "No puedes agregarte como tu propio coordinador.")

    rel = db.execute(select(UserCoord).where(
        UserCoord.coordinador_id == coor.id,
        UserCoord.miembro_id == uid
    )).scalars().first()

    if rel:
        already = rel.is_active
        if not rel.is_active:
            rel.is_active = True
            db.commit()
        return AddCoordinadorOut(ok=True, coordinador_id=coor.id, already_linked=already)

    rel = UserCoord(coordinador_id=coor.id, miembro_id=uid, is_active=True, selected=False)
    db.add(rel)
    db.commit()
    return AddCoordinadorOut(ok=True, coordinador_id=coor.id, already_linked=False)

@router.get("", response_model=List[CoordinadorOut])
def list_mis_coordinadores(
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
    include_inactivos: bool = Query(False),
):
    q = (
        select(
            UserCoord.coordinador_id,
            AppUser.nombre, AppUser.apellido_paterno, AppUser.apellido_materno,
            AppUser.telefono, UserCoord.is_active,
        )
        .select_from(UserCoord)
        .join(AppUser, AppUser.id == UserCoord.coordinador_id)
        .where(UserCoord.miembro_id == uid)
    )
    if not include_inactivos:
        q = q.where(UserCoord.is_active == True)  # noqa: E712

    rows = db.execute(q).all()
    out: List[CoordinadorOut] = []
    for coor_id, nombre, ap_pat, ap_mat, tel, activo in rows:
        full = " ".join(p for p in [nombre, ap_pat, ap_mat] if p)
        out.append(CoordinadorOut(
            coordinador_id=int(coor_id),
            nombre=full or None,
            telefono=tel,
            activo=bool(activo),
        ))
    return out

@router.patch("/{coordinador_id}", response_model=CoordinadorOut)
def activar_desactivar_coordinador(
    coordinador_id: int,
    activo: bool,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    rel = db.execute(select(UserCoord).where(
        UserCoord.coordinador_id == coordinador_id,
        UserCoord.miembro_id == uid
    )).scalars().first()
    if not rel:
        raise HTTPException(404, "Relación no encontrada.")
    rel.is_active = bool(activo)
    db.commit()

    u = db.get(AppUser, coordinador_id)
    full = " ".join(p for p in [u.nombre, u.apellido_paterno, u.apellido_materno] if p) if u else None
    return CoordinadorOut(
        coordinador_id=coordinador_id,
        nombre=full,
        telefono=u.telefono if u else None,
        activo=rel.is_active,
    )

@router.get("/mis-miembros", response_model=List[MiembroOut])
def list_mis_miembros(
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
    include_inactivos: bool = Query(False),
):
    q = (
        select(
            AppUser.id.label("miembro_id"),
            AppUser.nombre, AppUser.apellido_paterno, AppUser.apellido_materno,
            AppUser.telefono, UserCoord.selected, UserCoord.is_active,
        )
        .select_from(UserCoord)
        .join(AppUser, AppUser.id == UserCoord.miembro_id)
        .where(UserCoord.coordinador_id == uid)
    )
    if not include_inactivos:
        q = q.where(UserCoord.is_active == True)  # noqa: E712

    rows = db.execute(q).mappings().all()
    return [
        MiembroOut(
            miembro_id=int(r["miembro_id"]),
            nombre=r["nombre"],
            apellido_paterno=r["apellido_paterno"],
            apellido_materno=r["apellido_materno"],
            telefono=r["telefono"],
            selected=bool(r["selected"]),
            activo=bool(r["is_active"]),
        )
        for r in rows
    ]

@router.patch("/mis-miembros/{miembro_id}", response_model=MiembroOut)
def update_miembro_por_coordinador(
    miembro_id: int,
    body: UpdateMemberBody,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    rel = db.execute(select(UserCoord).where(
        UserCoord.coordinador_id == uid,
        UserCoord.miembro_id == miembro_id
    )).scalars().first()
    if not rel:
        raise HTTPException(404, "No tienes asignado a este miembro.")

    if body.selected is not None:
        rel.selected = bool(body.selected)
    if body.activo is not None:
        rel.is_active = bool(body.activo)
    db.commit()

    u = db.get(AppUser, miembro_id)
    return MiembroOut(
        miembro_id=miembro_id,
        nombre=u.nombre if u else None,
        apellido_paterno=u.apellido_paterno if u else None,
        apellido_materno=u.apellido_materno if u else None,
        telefono=u.telefono if u else None,
        selected=rel.selected,
        activo=rel.is_active,
    )
