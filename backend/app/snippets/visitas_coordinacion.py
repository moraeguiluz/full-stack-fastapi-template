# backend/app/snippets/visitas_coordinacion.py
from __future__ import annotations

import os, re, datetime as dt, jwt
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session, sessionmaker
from sqlalchemy import Integer, String, Boolean, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.exc import IntegrityError

router = APIRouter(prefix="/coordinadores", tags=["coordinadores"])

# ---------------- Config & lazy init ----------------
_DB_URL   = os.getenv("DATABASE_URL")
_SECRET   = os.getenv("SECRET_KEY", "dev-change-me")
_ALG      = "HS256"
_DEBUG    = os.getenv("COORD_DEBUG", "false").lower() == "true"

_engine: Optional[sa.Engine] = None
_SessionLocal: Optional[sessionmaker] = None
_inited = False

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/finalize")

def _init_db():
    """Inicializa engine y crea SOLO la tabla de este snippet (lazy init)."""
    global _engine, _SessionLocal, _inited
    if _inited:
        return
    if not _DB_URL:
        raise HTTPException(status_code=503, detail="DB no configurada (falta DATABASE_URL)")
    url = _DB_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    _engine = sa.create_engine(url, pool_pre_ping=True)
    Base.metadata.create_all(bind=_engine)  # crea app_user_coord si no existe
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

def _current_user_id(token: str = Depends(oauth2)) -> int:
    try:
        data = jwt.decode(token, _SECRET, algorithms=[_ALG])
    except Exception:
        raise HTTPException(status_code=401, detail="Token inválido")
    uid = data.get("sub")
    if not uid:
        raise HTTPException(status_code=401, detail="Token inválido")
    return int(uid)

def _clean_phone(phone: str) -> str:
    s = re.sub(r"[^\d+]", "", (phone or "").strip())
    if not s: return ""
    if s.startswith("+"): return s
    if len(s) == 10: return "+52" + s
    if s.startswith("52") and len(s) == 12: return "+" + s
    return "+" + s

# ---------------- Declarative (SOLO esta tabla) ----------------
class Base(DeclarativeBase):
    pass

# app_user_auth (REFLEJO mínimo — NO crea tabla)
class AppUser(Base):
    __tablename__ = "app_user_auth"
    __table_args__ = {"extend_existing": True}
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[Optional[str]] = mapped_column(String(120))
    apellido_paterno: Mapped[Optional[str]] = mapped_column(String(120))
    apellido_materno: Mapped[Optional[str]] = mapped_column(String(120))
    telefono: Mapped[Optional[str]] = mapped_column(String(32))

class UserCoord(Base):
    __tablename__ = "app_user_coord"
    __table_args__ = (UniqueConstraint("coordinador_id", "miembro_id", name="uq_app_user_coord_pair"),)
    id: Mapped[int]             = mapped_column(Integer, primary_key=True, autoincrement=True)
    coordinador_id: Mapped[int] = mapped_column(Integer, ForeignKey("app_user_auth.id"), index=True)
    miembro_id: Mapped[int]     = mapped_column(Integer, index=True)
    is_active: Mapped[bool]     = mapped_column(Boolean, default=True)    # Miembro habilita/deshabilita el vínculo
    selected: Mapped[bool]      = mapped_column(Boolean, default=False)   # Coordinador marca visibilidad
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

# ---------------- Pydantic ----------------
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
    selected: Optional[bool] = None
    activo: Optional[bool] = None

# ---------------- Endpoints ----------------
@router.post("", response_model=AddCoordinadorOut)
def add_coordinador(
    payload: AddCoordinadorIn,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    tel = _clean_phone(payload.telefono)
    if not tel or not tel.startswith("+"):
        raise HTTPException(400, "Teléfono inválido. Usa formato internacional, ej. +527771234567")

    try:
        # busca usuario por teléfono (no asume columna is_active)
        coor = db.query(AppUser).filter(AppUser.telefono == tel).first()
        if not coor:
            raise HTTPException(404, "No se encontró un usuario con ese teléfono.")
        if int(coor.id) == uid:
            raise HTTPException(400, "No puedes agregarte como tu propio coordinador.")

        # intenta crear vínculo
        rel = UserCoord(coordinador_id=int(coor.id), miembro_id=uid, is_active=True, selected=False)
        db.add(rel)
        try:
            db.commit()
            return AddCoordinadorOut(ok=True, coordinador_id=int(coor.id), already_linked=False)
        except IntegrityError:
            db.rollback()
            # Ya existe el par, re-activa si estaba desactivado
            existing = db.query(UserCoord).filter(
                UserCoord.coordinador_id == int(coor.id),
                UserCoord.miembro_id == uid
            ).with_for_update().one()
            already = existing.is_active
            if not existing.is_active:
                existing.is_active = True
                db.commit()
            return AddCoordinadorOut(ok=True, coordinador_id=int(coor.id), already_linked=already)
    except HTTPException:
        raise
    except Exception as ex:
        if _DEBUG:
            raise HTTPException(500, f"Internal error: {ex}")
        raise HTTPException(500, "Internal error")

@router.get("", response_model=List[CoordinadorOut])
def list_mis_coordinadores(
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
    include_inactivos: bool = Query(False),
):
    q = db.query(
        UserCoord.coordinador_id,
        AppUser.nombre, AppUser.apellido_paterno, AppUser.apellido_materno,
        AppUser.telefono, UserCoord.is_active
    ).join(AppUser, AppUser.id == UserCoord.coordinador_id).filter(UserCoord.miembro_id == uid)
    if not include_inactivos:
        q = q.filter(UserCoord.is_active == True)  # noqa: E712

    out: List[CoordinadorOut] = []
    for coor_id, n, ap, am, tel, activo in q.all():
        full = " ".join(p for p in [n, ap, am] if p)
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
    activo: bool = Query(..., description="true/false"),
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    rel = db.query(UserCoord).filter(
        UserCoord.coordinador_id == coordinador_id,
        UserCoord.miembro_id == uid
    ).one_or_none()
    if not rel:
        raise HTTPException(404, "Relación no encontrada.")
    rel.is_active = bool(activo)
    db.commit()

    u = db.query(AppUser).get(coordinador_id)
    full = " ".join(p for p in [u.nombre if u else None, u.apellido_paterno if u else None, u.apellido_materno if u else None] if p)
    return CoordinadorOut(
        coordinador_id=coordinador_id,
        nombre=full or None,
        telefono=u.telefono if u else None,
        activo=rel.is_active,
    )

@router.get("/mis-miembros", response_model=List[MiembroOut])
def list_mis_miembros(
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
    include_inactivos: bool = Query(False),
):
    q = db.query(
        AppUser.id.label("miembro_id"),
        AppUser.nombre, AppUser.apellido_paterno, AppUser.apellido_materno,
        AppUser.telefono, UserCoord.selected, UserCoord.is_active
    ).join(AppUser, AppUser.id == UserCoord.miembro_id).filter(UserCoord.coordinador_id == uid)
    if not include_inactivos:
        q = q.filter(UserCoord.is_active == True)  # noqa: E712

    return [
        MiembroOut(
            miembro_id=int(r.miembro_id),
            nombre=r.nombre,
            apellido_paterno=r.apellido_paterno,
            apellido_materno=r.apellido_materno,
            telefono=r.telefono,
            selected=bool(r.selected),
            activo=bool(r.is_active),
        )
        for r in q.all()
    ]

@router.patch("/mis-miembros/{miembro_id}", response_model=MiembroOut)
def update_miembro_por_coordinador(
    miembro_id: int,
    body: UpdateMemberBody,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    rel = db.query(UserCoord).filter(
        UserCoord.coordinador_id == uid,
        UserCoord.miembro_id == miembro_id
    ).one_or_none()
    if not rel:
        raise HTTPException(404, "No tienes asignado a este miembro.")

    if body.selected is not None:
        rel.selected = bool(body.selected)
    if body.activo is not None:
        rel.is_active = bool(body.activo)
    db.commit()

    u = db.query(AppUser).get(miembro_id)
    return MiembroOut(
        miembro_id=miembro_id,
        nombre=u.nombre if u else None,
        apellido_paterno=u.apellido_paterno if u else None,
        apellido_materno=u.apellido_materno if u else None,
        telefono=u.telefono if u else None,
        selected=rel.selected,
        activo=rel.is_active,
    )
