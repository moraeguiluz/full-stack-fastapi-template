# backend/app/snippets/profile.py
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field
from typing import Optional, List
import os, datetime as dt, jwt

from sqlalchemy import create_engine, String, Integer, DateTime, func, Text
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column, Session

router = APIRouter(prefix="/profile", tags=["profile"])

# -------------------- Config & lazy init --------------------
_DB_URL = os.getenv("DATABASE_URL")
_SECRET = os.getenv("SECRET_KEY", "dev-change-me")
_ALG = "HS256"

_engine = None
_SessionLocal = None
_inited = False

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/finalize")

class BaseOwn(DeclarativeBase):
    """Solo tablas propias de este snippet (para create_all)."""
    pass

class BaseRO(DeclarativeBase):
    """Tablas existentes (NO se crean aquí)."""
    pass

# ---- Tabla existente (auth) SOLO para leer/actualizar campos de nombre/telefono
class User(BaseRO):
    __tablename__ = "app_user_auth"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nombre: Mapped[Optional[str]] = mapped_column(String(120), default="")
    apellido_paterno: Mapped[Optional[str]] = mapped_column(String(120), default="")
    apellido_materno: Mapped[Optional[str]] = mapped_column(String(120), default="")
    telefono: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)
    # algunos snippets usan is_active; lo dejamos por seguridad
    is_active: Mapped[Optional[bool]] = mapped_column(default=True)

# ---- Tabla nueva (profile)
class UserProfile(BaseOwn):
    __tablename__ = "app_user_profile"
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    photo_url: Mapped[Optional[str]] = mapped_column(Text, default=None)          # URL estable (CDN/pública)
    photo_object_name: Mapped[Optional[str]] = mapped_column(Text, default=None) # opcional (GCS object_name)
    bio: Mapped[Optional[str]] = mapped_column(Text, default=None)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

def _init_db():
    global _engine, _SessionLocal, _inited
    if _inited or not _DB_URL:
        return

    url = _DB_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)

    _engine = create_engine(url, pool_pre_ping=True)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)

    # Creamos SOLO la tabla propia (app_user_profile).
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

def _decode_uid(token: str) -> int:
    try:
        data = jwt.decode(token, _SECRET, algorithms=[_ALG])
        uid = data.get("sub")
        if not uid:
            raise HTTPException(401, "Token inválido (sin sub)")
        return int(uid)
    except jwt.PyJWTError:
        raise HTTPException(401, "Token inválido")

def _full_name(u: User) -> str:
    parts = [u.nombre or "", u.apellido_paterno or "", u.apellido_materno or ""]
    return " ".join([p.strip() for p in parts if p and p.strip()]).strip()

def _touch_updated(p: UserProfile):
    p.updated_at = dt.datetime.now(dt.timezone.utc)

# -------------------- Schemas --------------------
class ProfileOut(BaseModel):
    id: int
    telefono: Optional[str] = None
    nombre: Optional[str] = ""
    apellido_paterno: Optional[str] = ""
    apellido_materno: Optional[str] = ""
    nombre_completo: str = ""

    photo_url: Optional[str] = None
    photo_object_name: Optional[str] = None
    bio: Optional[str] = None

    updated_at: Optional[dt.datetime] = None

class ProfilePatchIn(BaseModel):
    # Campos del usuario (auth)
    nombre: Optional[str] = Field(None, max_length=120)
    apellido_paterno: Optional[str] = Field(None, max_length=120)
    apellido_materno: Optional[str] = Field(None, max_length=120)

    # Campos del perfil
    photo_url: Optional[str] = Field(None, max_length=2048)
    photo_object_name: Optional[str] = Field(None, max_length=2048)
    bio: Optional[str] = Field(None, max_length=2000)

class UsersOut(BaseModel):
    users: List[ProfileOut]

# -------------------- Endpoints --------------------
@router.get("/me", response_model=ProfileOut)
def get_me(token: str = Depends(oauth2), db: Session = Depends(get_db)):
    uid = _decode_uid(token)

    u = db.query(User).filter(User.id == uid).first()
    if not u or (hasattr(u, "is_active") and u.is_active is False):
        raise HTTPException(401, "Usuario no encontrado o inactivo")

    p = db.query(UserProfile).filter(UserProfile.user_id == uid).first()
    if not p:
        p = UserProfile(user_id=uid)
        _touch_updated(p)
        db.add(p)
        db.commit()
        db.refresh(p)

    return ProfileOut(
        id=u.id,
        telefono=u.telefono,
        nombre=u.nombre or "",
        apellido_paterno=u.apellido_paterno or "",
        apellido_materno=u.apellido_materno or "",
        nombre_completo=_full_name(u),
        photo_url=p.photo_url,
        photo_object_name=p.photo_object_name,
        bio=p.bio,
        updated_at=p.updated_at,
    )

@router.patch("/me", response_model=ProfileOut)
def patch_me(body: ProfilePatchIn, token: str = Depends(oauth2), db: Session = Depends(get_db)):
    uid = _decode_uid(token)

    u = db.query(User).filter(User.id == uid).first()
    if not u or (hasattr(u, "is_active") and u.is_active is False):
        raise HTTPException(401, "Usuario no encontrado o inactivo")

    p = db.query(UserProfile).filter(UserProfile.user_id == uid).first()
    if not p:
        p = UserProfile(user_id=uid)
        db.add(p)

    # Actualizar usuario
    if body.nombre is not None:
        u.nombre = body.nombre.strip()
    if body.apellido_paterno is not None:
        u.apellido_paterno = body.apellido_paterno.strip()
    if body.apellido_materno is not None:
        u.apellido_materno = body.apellido_materno.strip()

    # Actualizar perfil
    if body.photo_url is not None:
        p.photo_url = body.photo_url.strip() or None
    if body.photo_object_name is not None:
        p.photo_object_name = body.photo_object_name.strip() or None
    if body.bio is not None:
        p.bio = body.bio.strip() or None

    _touch_updated(p)
    db.commit()
    db.refresh(p)

    return ProfileOut(
        id=u.id,
        telefono=u.telefono,
        nombre=u.nombre or "",
        apellido_paterno=u.apellido_paterno or "",
        apellido_materno=u.apellido_materno or "",
        nombre_completo=_full_name(u),
        photo_url=p.photo_url,
        photo_object_name=p.photo_object_name,
        bio=p.bio,
        updated_at=p.updated_at,
    )

@router.get("/users", response_model=UsersOut)
def get_users(ids: str, token: str = Depends(oauth2), db: Session = Depends(get_db)):
    """
    Ej: /api/v1/profile/users?ids=12,44,90
    Útil para cachear perfiles de chats/contacts.
    """
    _ = _decode_uid(token)  # solo validar token

    try:
        id_list = [int(x) for x in ids.split(",") if x.strip()]
        id_list = list(dict.fromkeys(id_list))[:200]  # dedupe + límite
    except ValueError:
        raise HTTPException(400, "ids inválidos")

    if not id_list:
        return UsersOut(users=[])

    users = db.query(User).filter(User.id.in_(id_list)).all()
    profs = db.query(UserProfile).filter(UserProfile.user_id.in_(id_list)).all()
    prof_map = {p.user_id: p for p in profs}

    out = []
    for u in users:
        p = prof_map.get(u.id)
        out.append(ProfileOut(
            id=u.id,
            telefono=u.telefono,
            nombre=u.nombre or "",
            apellido_paterno=u.apellido_paterno or "",
            apellido_materno=u.apellido_materno or "",
            nombre_completo=_full_name(u),
            photo_url=(p.photo_url if p else None),
            photo_object_name=(p.photo_object_name if p else None),
            bio=(p.bio if p else None),
            updated_at=(p.updated_at if p else None),
        ))

    return UsersOut(users=out)
