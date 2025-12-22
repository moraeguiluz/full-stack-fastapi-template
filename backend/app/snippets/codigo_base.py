# backend/app/snippets/codigo_base.py
from __future__ import annotations

import os, datetime as dt, jwt
from typing import Optional, List, Dict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field

from sqlalchemy import (
    create_engine, select, Integer, String, Boolean, DateTime, Text, func
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, Session, sessionmaker
)
from sqlalchemy.dialects.postgresql import JSONB  # NUEVO: para extra_schema JSONB

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
    # Crea sólo las tablas de este snippet si no existen.
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
    # NUEVO: definición de campos extra para visitas (schema de extra JSONB)
    extra_schema: Mapped[list] = mapped_column(JSONB, default=list)


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


# Mapeo mínimo para nombres de usuario (Sólo lectura)
class AppUser(Base):
    __tablename__ = "app_user_auth"
    __table_args__ = {"extend_existing": True}
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[Optional[str]] = mapped_column(String(120))
    apellido_paterno: Mapped[Optional[str]] = mapped_column(String(120))
    apellido_materno: Mapped[Optional[str]] = mapped_column(String(120))


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


# --- Admin: miembros / solicitudes ---
class CodigoBaseAdminMemberOut(BaseModel):
    membership_id: int
    user_id: int
    nombre: Optional[str] = None
    status: str
    is_active: bool
    is_manager: bool
    joined_at: Optional[dt.datetime] = None
    message: Optional[str] = None

    class Config:
        from_attributes = True


class CodigoBaseAdminMembersOut(BaseModel):
    codigo_base_id: int
    codigo: str
    nombre: str
    miembros: List[CodigoBaseAdminMemberOut]
    pendientes: List[CodigoBaseAdminMemberOut]


# -------- NUEVOS Schemas: definición de campos extra --------

# Tipos de campo permitidos para el schema de extra
_ALLOWED_FIELD_TYPES = {
    "texto_corto",
    "texto_largo",
    "numero",
    "si_no",
    "opcion_unica",
    "opcion_multiple",
    "fecha",
    "imagen_url",
}


class CodigoBaseFieldSchema(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    type: str = Field(min_length=1, max_length=32)
    required: bool = False
    order: int = 0
    options: Optional[List[str]] = None


class CodigoBaseSchemaOut(BaseModel):
    id: int
    codigo: str
    nombre: str
    descripcion: Optional[str] = None
    admin_id: Optional[int] = None
    allow_any: bool
    is_active: bool
    fields: List[CodigoBaseFieldSchema]


class CodigoBaseSchemaIn(BaseModel):
    codigo: str = Field(min_length=3, max_length=64)
    fields: List[CodigoBaseFieldSchema]


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

    # Si permite a cualquiera y aún no es miembro aprobado, lo agregamos
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

    # Si el código permite a cualquiera, se auto-aprueba sin solicitud pendiente
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


# -------- Endpoint: listar códigos base del usuario --------
@router.get("/mis-codigos", response_model=List[CodigoBaseVerifyResult])
def mis_codigos_base(
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    # Códigos donde es admin
    admin_cbs = db.execute(
        select(CodigoBase).where(
            CodigoBase.admin_id == uid,
            CodigoBase.is_active == True,  # noqa: E712
        )
    ).scalars().all()

    # Códigos donde es miembro aprobado
    member_rows = db.execute(
        select(CodigoBase, CodigoBaseUser).join(
            CodigoBaseUser,
            CodigoBaseUser.codigo_base_id == CodigoBase.id,
        ).where(
            CodigoBaseUser.user_id == uid,
            CodigoBaseUser.status == "approved",
            CodigoBaseUser.is_active == True,  # noqa: E712
            CodigoBase.is_active == True,      # noqa: E712
        )
    ).all()

    # Combinar resultados sin duplicar
    by_id: dict[int, dict] = {}

    for cb in admin_cbs:
        by_id[cb.id] = dict(
            cb=cb,
            es_admin=True,
            es_miembro=True,  # admin también cuenta como miembro
        )

    for cb, cu in member_rows:
        existing = by_id.get(cb.id)
        if existing is None:
            by_id[cb.id] = dict(
                cb=cb,
                es_admin=(cb.admin_id == uid),
                es_miembro=True,
            )
        else:
            existing["es_miembro"] = True
            existing["es_admin"] = existing["es_admin"] or (cb.admin_id == uid)

    out: list[CodigoBaseVerifyResult] = []
    for entry in by_id.values():
        cb = entry["cb"]
        es_admin = bool(entry["es_admin"])
        es_miembro = bool(entry["es_miembro"])
        out.append(
            CodigoBaseVerifyResult(
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
        )

    return out


# ================= ADMIN: miembros y solicitudes =================

def _require_admin_for_codigo(
    db: Session,
    uid: int,
    codigo: str,
) -> CodigoBase:
    codigo = codigo.strip()
    if not codigo:
        raise HTTPException(400, "Código base vacío")

    cb = db.execute(
        select(CodigoBase).where(CodigoBase.codigo == codigo)
    ).scalars().first()
    if cb is None or not cb.is_active:
        raise HTTPException(404, "Código base no válido o inactivo")

    if cb.admin_id != uid:
        raise HTTPException(403, "No eres administrador de este código base")

    return cb


def _full_name(u: Optional[AppUser]) -> Optional[str]:
    if not u:
        return None
    parts = [u.nombre, u.apellido_paterno, u.apellido_materno]
    name = " ".join(p for p in parts if p)
    return name or None


@router.get("/admin/members", response_model=CodigoBaseAdminMembersOut)
def admin_list_members(
    codigo: str,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    cb = _require_admin_for_codigo(db, uid, codigo)

    rows = db.execute(
        select(CodigoBaseUser, AppUser).join(
            AppUser,
            AppUser.id == CodigoBaseUser.user_id,
            isouter=True,
        ).where(
            CodigoBaseUser.codigo_base_id == cb.id
        )
    ).all()

    miembros: List[CodigoBaseAdminMemberOut] = []
    pendientes: List[CodigoBaseAdminMemberOut] = []

    for cu, u in rows:
        item = CodigoBaseAdminMemberOut(
            membership_id=cu.id,
            user_id=cu.user_id,
            nombre=_full_name(u),
            status=cu.status,
            is_active=cu.is_active,
            is_manager=cu.is_manager,
            joined_at=cu.joined_at,
            message=cu.message,
        )
        if cu.status == "pending":
            pendientes.append(item)
        elif cu.status == "approved":
            miembros.append(item)
        # status 'rejected' no lo mostramos en la UI normal

    return CodigoBaseAdminMembersOut(
        codigo_base_id=cb.id,
        codigo=cb.codigo,
        nombre=cb.nombre,
        miembros=miembros,
        pendientes=pendientes,
    )


@router.post("/admin/membership/{membership_id}/approve", response_model=CodigoBaseAdminMemberOut)
def admin_approve_membership(
    membership_id: int,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    cu = db.get(CodigoBaseUser, membership_id)
    if not cu:
        raise HTTPException(404, "Membresía no encontrada")

    cb = db.execute(
        select(CodigoBase).where(CodigoBase.id == cu.codigo_base_id)
    ).scalars().first()
    if cb is None or not cb.is_active:
        raise HTTPException(404, "Código base no válido o inactivo")
    if cb.admin_id != uid:
        raise HTTPException(403, "No eres administrador de este código base")

    cu.status = "approved"
    cu.is_active = True
    cu.decided_at = _now_utc()
    cu.decided_by = uid
    if cu.joined_at is None:
        cu.joined_at = _now_utc()

    db.commit()
    db.refresh(cu)

    u = db.get(AppUser, cu.user_id)
    return CodigoBaseAdminMemberOut(
        membership_id=cu.id,
        user_id=cu.user_id,
        nombre=_full_name(u),
        status=cu.status,
        is_active=cu.is_active,
        is_manager=cu.is_manager,
        joined_at=cu.joined_at,
        message=cu.message,
    )


@router.post("/admin/membership/{membership_id}/reject", response_model=CodigoBaseAdminMemberOut)
def admin_reject_membership(
    membership_id: int,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    cu = db.get(CodigoBaseUser, membership_id)
    if not cu:
        raise HTTPException(404, "Membresía no encontrada")

    cb = db.execute(
        select(CodigoBase).where(CodigoBase.id == cu.codigo_base_id)
    ).scalars().first()
    if cb is None or not cb.is_active:
        raise HTTPException(404, "Código base no válido o inactivo")
    if cb.admin_id != uid:
        raise HTTPException(403, "No eres administrador de este código base")

    cu.status = "rejected"
    cu.is_active = False
    cu.decided_at = _now_utc()
    cu.decided_by = uid

    db.commit()
    db.refresh(cu)

    u = db.get(AppUser, cu.user_id)
    return CodigoBaseAdminMemberOut(
        membership_id=cu.id,
        user_id=cu.user_id,
        nombre=_full_name(u),
        status=cu.status,
        is_active=cu.is_active,
        is_manager=cu.is_manager,
        joined_at=cu.joined_at,
        message=cu.message,
    )


@router.post("/admin/membership/{membership_id}/remove")
def admin_remove_member(
    membership_id: int,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    cu = db.get(CodigoBaseUser, membership_id)
    if not cu:
        raise HTTPException(404, "Membresía no encontrada")

    cb = db.execute(
        select(CodigoBase).where(CodigoBase.id == cu.codigo_base_id)
    ).scalars().first()
    if cb is None or not cb.is_active:
        raise HTTPException(404, "Código base no válido o inactivo")
    if cb.admin_id != uid:
        raise HTTPException(403, "No eres administrador de este código base")

    # Para simplificar: eliminamos el registro.
    db.delete(cu)
    db.commit()
    return {"ok": True}


# ================= ADMIN: schema de campos extra (extra_schema) =================

def _normalize_extra_schema(raw: Optional[object]) -> List[Dict]:
    """
    Normaliza extra_schema a una lista de diccionarios.
    - Si es None -> []
    - Si es lista -> sólo diccionarios válidos
    - Si es dict -> lo envuelve en lista
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [d for d in raw if isinstance(d, dict)]
    if isinstance(raw, dict):
        return [raw]
    return []


def _require_member_or_public_for_codigo(
    db: Session,
    uid: int,
    codigo: str,
) -> CodigoBase:
    """
    Permite acceso al Código Base si:
    - el usuario es admin del código, o
    - el código tiene allow_any = True, o
    - el usuario es miembro aprobado y activo.
    """
    codigo = codigo.strip()
    if not codigo:
        raise HTTPException(400, "Código base vacío")

    cb = db.execute(
        select(CodigoBase).where(CodigoBase.codigo == codigo)
    ).scalars().first()

    if cb is None or not cb.is_active:
        raise HTTPException(404, "Código base no válido o inactivo")

    # Admin siempre tiene acceso
    if cb.admin_id == uid:
        return cb

    # Si allow_any, cualquiera autenticado puede leer el schema
    if cb.allow_any:
        return cb

    # Caso normal: sólo miembros aprobados y activos
    memb = db.execute(
        select(CodigoBaseUser).where(
            CodigoBaseUser.codigo_base_id == cb.id,
            CodigoBaseUser.user_id == uid,
            CodigoBaseUser.status == "approved",
            CodigoBaseUser.is_active == True,  # noqa: E712
        )
    ).scalars().first()

    if memb is None:
        raise HTTPException(403, "No estás autorizado para este código base")

    return cb


@router.get("/schema", response_model=CodigoBaseSchemaOut)
def get_schema_para_app(
    codigo: str,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    """
    Endpoint para la APP (no admin) que devuelve el schema de campos extra.

    Acceso:
    - Admin del código base, o
    - Miembro aprobado y activo, o
    - Cualquier usuario si allow_any = True.
    """
    cb = _require_member_or_public_for_codigo(db, uid, codigo)

    raw_fields = _normalize_extra_schema(cb.extra_schema)
    fields: List[CodigoBaseFieldSchema] = []
    for item in raw_fields:
        try:
            fields.append(CodigoBaseFieldSchema(**item))
        except Exception:
            # Ignoramos entradas inválidas para no romper la UI
            continue

    return CodigoBaseSchemaOut(
        id=cb.id,
        codigo=cb.codigo,
        nombre=cb.nombre,
        descripcion=cb.descripcion or None,
        admin_id=cb.admin_id,
        allow_any=cb.allow_any,
        is_active=cb.is_active,
        fields=fields,
    )


@router.get("/admin/schema", response_model=CodigoBaseSchemaOut)
def admin_get_schema(
    codigo: str,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    cb = _require_admin_for_codigo(db, uid, codigo)

    raw_fields = _normalize_extra_schema(cb.extra_schema)
    fields: List[CodigoBaseFieldSchema] = []
    for item in raw_fields:
        try:
            fields.append(CodigoBaseFieldSchema(**item))
        except Exception:
            # Ignoramos entradas inválidas para no romper la UI
            continue

    return CodigoBaseSchemaOut(
        id=cb.id,
        codigo=cb.codigo,
        nombre=cb.nombre,
        descripcion=cb.descripcion or None,
        admin_id=cb.admin_id,
        allow_any=cb.allow_any,
        is_active=cb.is_active,
        fields=fields,
    )


@router.post("/admin/schema", response_model=CodigoBaseSchemaOut)
def admin_set_schema(
    payload: CodigoBaseSchemaIn,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    cb = _require_admin_for_codigo(db, uid, payload.codigo)

    # Validar tipos permitidos y claves únicas
    seen_keys: set[str] = set()
    clean_fields: List[Dict] = []

    for f in payload.fields:
        key = f.key.strip()
        type_ = f.type.strip()
        if not key:
            raise HTTPException(400, "Todos los campos deben tener 'key'")
        if key in seen_keys:
            raise HTTPException(400, f"Clave de campo duplicada: {key}")
        seen_keys.add(key)

        if type_ not in _ALLOWED_FIELD_TYPES:
            raise HTTPException(400, f"Tipo de campo no permitido: {type_}")

        data = f.model_dump(exclude_none=True)
        data["key"] = key
        data["type"] = type_
        clean_fields.append(data)

    cb.extra_schema = clean_fields
    db.commit()
    db.refresh(cb)

    # Responder usando el mismo formato que GET
    raw_fields = _normalize_extra_schema(cb.extra_schema)
    fields: List[CodigoBaseFieldSchema] = []
    for item in raw_fields:
        try:
            fields.append(CodigoBaseFieldSchema(**item))
        except Exception:
            continue

    return CodigoBaseSchemaOut(
        id=cb.id,
        codigo=cb.codigo,
        nombre=cb.nombre,
        descripcion=cb.descripcion or None,
        admin_id=cb.admin_id,
        allow_any=cb.allow_any,
        is_active=cb.is_active,
        fields=fields,
    )
