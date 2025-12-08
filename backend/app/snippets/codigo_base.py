# backend/app/snippets/codigo_base.py
from __future__ import annotations

import os, datetime as dt, jwt
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field

from sqlalchemy import (
    create_engine, select, Integer, String, Boolean, DateTime, Text, func
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, Session, sessionmaker
)

router = APIRouter(prefix="/codigo-base", tags=["codigo_base"])

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
    # Sólo crea tablas si no existen según estos modelos
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


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# -------- Declarative models --------
class Base(DeclarativeBase):
    pass


class CodigoBase(Base):
    __tablename__ = "app_codigo_base"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    codigo: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    nombre: Mapped[str] = mapped_column(String(120))
    descripcion: Mapped[str] = mapped_column(Text, default="")
    creado_por: Mapped[int] = mapped_column(Integer, index=True)
    admin_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    allow_any: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )


class CodigoBaseUser(Base):
    __tablename__ = "app_codigo_base_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    codigo_base_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_manager: Mapped[bool] = mapped_column(Boolean, default=False)
    joined_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
    # Unificado: membresía + solicitudes
    status: Mapped[str] = mapped_column(String(16), default="approved")  # 'pending' | 'approved' | 'rejected'
    message: Mapped[Optional[str]] = mapped_column(Text, default=None)
    decided_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)
    decided_by: Mapped[Optional[int]] = mapped_column(Integer, default=None)


# -------- Schemas --------
class CodigoBaseVerifyIn(BaseModel):
    codigo: str = Field(min_length=3, max_length=64)


class CodigoBaseVerifyResult(BaseModel):
    id: int
    codigo: str
    nombre: str
    descripcion: Optional[str] = None
    admin_id: Optional[int] = None
    allow_any: bool
    is_active: bool
    es_admin: bool
    es_miembro: bool

    class Config:
        from_attributes = True


class CodigoBaseRequestJoinIn(BaseModel):
    codigo: str = Field(min_length=3, max_length=64)
    message: Optional[str] = Field(default=None, max_length=500)


class CodigoBaseRequestJoinOut(BaseModel):
    codigo_base_id: int
    codigo: str
    nombre: str
    status: str  # 'already_member' | 'pending' | 'auto_approved'
    request_id: Optional[int] = None
    admin_id: Optional[int] = None
    allow_any: bool

    class Config:
        from_attributes = True


# -------- Endpoint: verificar código base --------
@router.post("/verify", response_model=CodigoBaseVerifyResult)
def verify_codigo_base(
    payload: CodigoBaseVerifyIn,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    codigo = payload.codigo.strip()
    if not codigo:
        raise HTTPException(400, "Código base vacío")

    cb = db.execute(
        select(CodigoBase).where(CodigoBase.codigo == codigo)
    ).scalars().first()

    # No creamos nuevos códigos aquí: sólo validamos los existentes
    if cb is None or not cb.is_active:
        raise HTTPException(404, "Código base no válido o inactivo")

    es_admin = (cb.admin_id == uid)

    # Buscar membresía aprobada/activa
    memb = db.execute(
        select(CodigoBaseUser).where(
            CodigoBaseUser.codigo_base_id == cb.id,
            CodigoBaseUser.user_id == uid,
            CodigoBaseUser.status == "approved",
            CodigoBaseUser.is_active == True,  # noqa: E712
        )
    ).scalars().first()
    es_miembro = memb is not None

    # Si no permite a cualquiera y no es admin ni miembro → prohibido
    if not cb.allow_any and not es_admin and not es_miembro:
        raise HTTPException(403, "No estás autorizado para este código base")

    # Si permite a cualquiera y aún no es miembro aprovado, lo agregamos
    if cb.allow_any and not es_miembro:
        new_m = CodigoBaseUser(
            codigo_base_id=cb.id,
            user_id=uid,
            is_active=True,
            is_manager=False,
            status="approved",
            joined_at=_now_utc(),
        )
        db.add(new_m)
        db.commit()
        es_miembro = True

    return CodigoBaseVerifyResult(
        id=cb.id,
        codigo=cb.codigo,
        nombre=cb.nombre,
        descripcion=cb.descripcion or None,
        admin_id=cb.admin_id,
        allow_any=cb.allow_any,
        is_active=cb.is_active,
        es_admin=es_admin,
        es_miembro=es_miembro,
    )


# -------- Endpoint: solicitar unirse a código base --------
@router.post("/request-join", response_model=CodigoBaseRequestJoinOut)
def request_join_codigo_base(
    payload: CodigoBaseRequestJoinIn,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    codigo = payload.codigo.strip()
    if not codigo:
        raise HTTPException(400, "Código base vacío")

    cb = db.execute(
        select(CodigoBase).where(CodigoBase.codigo == codigo)
    ).scalars().first()

    if cb is None or not cb.is_active:
        raise HTTPException(404, "Código base no válido o inactivo")

    es_admin = (cb.admin_id == uid)

    # ¿Ya es miembro aprobado?
    approved = db.execute(
        select(CodigoBaseUser).where(
            CodigoBaseUser.codigo_base_id == cb.id,
            CodigoBaseUser.user_id == uid,
            CodigoBaseUser.status == "approved",
            CodigoBaseUser.is_active == True,  # noqa: E712
        )
    ).scalars().first()

    if es_admin or approved is not None:
        # Ya es miembro / admin, no necesita solicitud
        return CodigoBaseRequestJoinOut(
            codigo_base_id=cb.id,
            codigo=cb.codigo,
            nombre=cb.nombre,
            status="already_member",
            request_id=None,
            admin_id=cb.admin_id,
            allow_any=cb.allow_any,
        )

    # Si el código permite a cualquiera, no tiene sentido solicitar: se auto-aprueba
    if cb.allow_any:
        new_m = CodigoBaseUser(
            codigo_base_id=cb.id,
            user_id=uid,
            is_active=True,
            is_manager=False,
            status="approved",
            joined_at=_now_utc(),
        )
        db.add(new_m)
        db.commit()
        db.refresh(new_m)
        return CodigoBaseRequestJoinOut(
            codigo_base_id=cb.id,
            codigo=cb.codigo,
            nombre=cb.nombre,
            status="auto_approved",
            request_id=new_m.id,
            admin_id=cb.admin_id,
            allow_any=cb.allow_any,
        )

    # Si es restringido (allow_any = False):
    # ¿Ya tiene solicitud pendiente?
    pending = db.execute(
        select(CodigoBaseUser).where(
            CodigoBaseUser.codigo_base_id == cb.id,
            CodigoBaseUser.user_id == uid,
            CodigoBaseUser.status == "pending",
        )
    ).scalars().first()

    if pending is not None:
        # Ya había solicitud pendiente
        return CodigoBaseRequestJoinOut(
            codigo_base_id=cb.id,
            codigo=cb.codigo,
            nombre=cb.nombre,
            status="pending",
            request_id=pending.id,
            admin_id=cb.admin_id,
            allow_any=cb.allow_any,
        )

    # Crear nueva solicitud pendiente
    req = CodigoBaseUser(
        codigo_base_id=cb.id,
        user_id=uid,
        is_active=False,  # aún no es miembro
        is_manager=False,
        status="pending",
        message=(payload.message or "").strip() or None,
        joined_at=_now_utc(),
    )
    db.add(req)
    db.commit()
    db.refresh(req)

    return CodigoBaseRequestJoinOut(
        codigo_base_id=cb.id,
        codigo=cb.codigo,
        nombre=cb.nombre,
        status="pending",
        request_id=req.id,
        admin_id=cb.admin_id,
        allow_any=cb.allow_any,
    )
