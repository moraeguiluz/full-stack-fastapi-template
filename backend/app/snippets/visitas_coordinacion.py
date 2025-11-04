# backend/app/snippets/visitas_coordinacion.py
from __future__ import annotations

import os, re, datetime as dt, jwt
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session, sessionmaker
from sqlalchemy import Integer, String, Boolean, DateTime, ForeignKey, UniqueConstraint, func, text

router = APIRouter(prefix="/coordinadores", tags=["coordinadores"])

# ---------------- Config & lazy init ----------------
_DB_URL = os.getenv("DATABASE_URL")
_SECRET = os.getenv("SECRET_KEY", "dev-change-me")
_ALG = "HS256"

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
    if not s:
        return ""
    if s.startswith("+"):
        return s
    if len(s) == 10:
        return "+52" + s
    if s.startswith("52") and len(s) == 12:
        return "+" + s
    return "+" + s

# ---------------- Declarative (SOLO esta tabla) ----------------
class Base(DeclarativeBase):
    pass

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

# ---------------- Helpers SQL (app_user_auth) ----------------
def _get_user_id_by_phone(db: Session, tel: str) -> Optional[int]:
    """Busca en app_user_auth por teléfono y activo; devuelve id o None."""
    sql = text("""
        SELECT id
        FROM app_user_auth
        WHERE telefono = :tel
          AND (is_active = TRUE OR is_active IS NULL)   -- por compatibilidad si is_active no existe
        LIMIT 1
    """)
    row = db.execute(sql, {"tel": tel}).first()
    return int(row[0]) if row else None

def _get_user_name_and_phone(db: Session, user_id: int) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    sql = text("""
        SELECT nombre, apellido_paterno, apellido_materno, telefono
        FROM app_user_auth
        WHERE id = :uid
        LIMIT 1
    """)
    row = db.execute(sql, {"uid": user_id}).first()
    if not row:
        return None, None, None, None
    return row[0], row[1], row[2], row[3]

# ---------------- Endpoints ----------------
@router.post("", response_model=AddCoordinadorOut)
def add_coordinador(
    payload: AddCoordinadorIn,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    tel = _clean_phone(payload.telefono)
    if not tel or not tel.startswith("+"):
        raise HTTPException(status_code=400, detail="Teléfono inválido. Usa formato internacional, ej. +527771234567")

    coor_id = _get_user_id_by_phone(db, tel)
    if not coor_id:
        raise HTTPException(status_code=404, detail="No se encontró un usuario activo con ese teléfono.")
    if coor_id == uid:
        raise HTTPException(status_code=400, detail="No puedes agregarte como tu propio coordinador.")

    # upsert simple
    rel = db.query(UserCoord).filter(
        UserCoord.coordinador_id == coor_id,
        UserCoord.miembro_id == uid
    ).one_or_none()

    if rel:
        already = rel.is_active
        if not rel.is_active:
            rel.is_active = True
            db.commit()
        return AddCoordinadorOut(ok=True, coordinador_id=coor_id, already_linked=already)

    db.add(UserCoord(coordinador_id=coor_id, miembro_id=uid, is_active=True, selected=False))
    db.commit()
    return AddCoordinadorOut(ok=True, coordinador_id=coor_id, already_linked=False)

@router.get("", response_model=List[CoordinadorOut])
def list_mis_coordinadores(
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
    include_inactivos: bool = Query(False),
):
    sql = text(f"""
        SELECT uc.coordinador_id,
               u.nombre, u.apellido_paterno, u.apellido_materno, u.telefono,
               uc.is_active
        FROM app_user_coord uc
        JOIN app_user_auth u ON u.id = uc.coordinador_id
        WHERE uc.miembro_id = :uid
        {"AND uc.is_active = TRUE" if not include_inactivos else ""}
        ORDER BY u.nombre NULLS LAST, u.apellido_paterno NULLS LAST
    """)
    rows = db.execute(sql, {"uid": uid}).mappings().all()
    out: List[CoordinadorOut] = []
    for r in rows:
        full = " ".join([p for p in [r["nombre"], r["apellido_paterno"], r["apellido_materno"]] if p])
        out.append(CoordinadorOut(
            coordinador_id=int(r["coordinador_id"]),
            nombre=full or None,
            telefono=r["telefono"],
            activo=bool(r["is_active"]),
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
        raise HTTPException(status_code=404, detail="Relación no encontrada.")

    rel.is_active = bool(activo)
    db.commit()

    nombre, ap_pat, ap_mat, tel = _get_user_name_and_phone(db, coordinador_id)
    full = " ".join([p for p in [nombre, ap_pat, ap_mat] if p])
    return CoordinadorOut(
        coordinador_id=coordinador_id,
        nombre=full or None,
        telefono=tel,
        activo=rel.is_active,
    )

@router.get("/mis-miembros", response_model=List[MiembroOut])
def list_mis_miembros(
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
    include_inactivos: bool = Query(False),
):
    sql = text(f"""
        SELECT u.id AS miembro_id,
               u.nombre, u.apellido_paterno, u.apellido_materno, u.telefono,
               uc.selected, uc.is_active
        FROM app_user_coord uc
        JOIN app_user_auth u ON u.id = uc.miembro_id
        WHERE uc.coordinador_id = :uid
        {"AND uc.is_active = TRUE" if not include_inactivos else ""}
        ORDER BY u.nombre NULLS LAST, u.apellido_paterno NULLS LAST
    """)
    rows = db.execute(sql, {"uid": uid}).mappings().all()
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
    rel = db.query(UserCoord).filter(
        UserCoord.coordinador_id == uid,
        UserCoord.miembro_id == miembro_id
    ).one_or_none()
    if not rel:
        raise HTTPException(status_code=404, detail="No tienes asignado a este miembro.")

    if body.selected is not None:
        rel.selected = bool(body.selected)
    if body.activo is not None:
        rel.is_active = bool(body.activo)
    db.commit()

    nombre, ap_pat, ap_mat, tel = _get_user_name_and_phone(db, miembro_id)
    return MiembroOut(
        miembro_id=miembro_id,
        nombre=nombre,
        apellido_paterno=ap_pat,
        apellido_materno=ap_mat,
        telefono=tel,
        selected=rel.selected,
        activo=rel.is_active,
    )
