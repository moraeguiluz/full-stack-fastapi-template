# backend/app/snippets/visitas.py
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field, validator
from typing import Optional, List
import os, datetime as dt, re, jwt

from sqlalchemy import (
    create_engine, String, Integer, DateTime, Float, func, select, desc
)
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column, Session

router = APIRouter(prefix="/visitas", tags=["visitas"])

# -------------------- Config & lazy init --------------------
_DB_URL = os.getenv("DATABASE_URL")
_SECRET = os.getenv("SECRET_KEY", "dev-change-me")
_ALG = "HS256"

_engine = None
_SessionLocal = None
_inited = False

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/finalize")

class Base(DeclarativeBase):
    pass

def _init_db():
    """Inicializa conexión y tablas al primer uso; no rompe el arranque si falta la env."""
    global _engine, _SessionLocal, _inited
    if _inited or not _DB_URL:
        return
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

# -------------------- Helpers --------------------
def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)  # aware UTC

def _decode(token: str) -> dict:
    return jwt.decode(token, _SECRET, algorithms=[_ALG])

def _clean_phone(phone: Optional[str]) -> Optional[str]:
    if phone is None:
        return None
    s = re.sub(r"[^\d+]", "", phone.strip())  # deja + y dígitos
    return s or None

async def _current_user_id(token: str = Depends(oauth2)) -> int:
    try:
        data = _decode(token)
        sub = data.get("sub")
        uid = int(sub) if sub is not None else None
    except jwt.PyJWTError:
        raise HTTPException(401, "Token inválido")
    if not uid:
        raise HTTPException(401, "Usuario no autenticado")
    return uid

def _ensure_tz(dtvalue: Optional[dt.datetime]) -> Optional[dt.datetime]:
    if dtvalue is None:
        return None
    return dtvalue if dtvalue.tzinfo else dtvalue.replace(tzinfo=dt.timezone.utc)

# -------------------- Modelo SQLAlchemy --------------------
class Visita(Base):
    __tablename__ = "app_visita"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Usuario que registra (del JWT sub)
    user_id: Mapped[int] = mapped_column(Integer, index=True)

    # Persona visitada (único obligatorio en API: nombre)
    nombre: Mapped[str] = mapped_column(String(120), default="")

    apellido_paterno: Mapped[str] = mapped_column(String(120), default="")
    apellido_materno: Mapped[str] = mapped_column(String(120), default="")
    telefono: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)

    # Ubicación (opcionales)
    lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True, index=True)
    lng: Mapped[Optional[float]] = mapped_column(Float, nullable=True, index=True)

    # Hora de la visita (si no se manda, se pone now())
    hora: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)

    # Número de personas mayores de edad en casa (si no se manda, 0)
    adultos: Mapped[int] = mapped_column(Integer, default=0)

    # Notas (opcional; por defecto vacío)
    notas: Mapped[str] = mapped_column(String(2000), default="")

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now()
    )

# -------------------- Schemas Pydantic --------------------
class VisitaCreate(BaseModel):
    # Único obligatorio:
    nombre: str = Field(min_length=1, max_length=120)

    # Todo lo demás opcional
    apellido_paterno: Optional[str] = Field(default=None, max_length=120)
    apellido_materno: Optional[str] = Field(default=None, max_length=120)
    telefono: Optional[str] = Field(default=None, max_length=32)

    lat: Optional[float] = Field(default=None, description="Latitud WGS84")
    lng: Optional[float] = Field(default=None, description="Longitud WGS84")

    hora: Optional[dt.datetime] = Field(default=None, description="Fecha/hora de la visita; si no, now()")
    adultos: Optional[int] = Field(default=None, ge=0, le=50)
    notas: Optional[str] = Field(default=None, max_length=2000)

    @validator("telefono")
    def _v_tel(cls, v):
        return _clean_phone(v)

    @validator("hora", pre=True)
    def _v_hora(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            try:
                parsed = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("hora debe ser ISO 8601")
        else:
            parsed = v
        return _ensure_tz(parsed)

class VisitaOut(BaseModel):
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

    class Config:
        from_attributes = True

class VisitaListOut(BaseModel):
    items: List[VisitaOut]
    total: int

class VisitaPatch(BaseModel):
    # Todos opcionales en patch
    nombre: Optional[str] = Field(default=None, min_length=1, max_length=120)
    apellido_paterno: Optional[str] = Field(default=None, max_length=120)
    apellido_materno: Optional[str] = Field(default=None, max_length=120)
    telefono: Optional[str] = Field(default=None, max_length=32)

    lat: Optional[float] = None
    lng: Optional[float] = None
    hora: Optional[dt.datetime] = None
    adultos: Optional[int] = Field(default=None, ge=0, le=50)
    notas: Optional[str] = Field(default=None, max_length=2000)

    @validator("telefono")
    def _vp_tel(cls, v):
        return _clean_phone(v)

    @validator("hora", pre=True)
    def _vp_hora(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            try:
                parsed = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("hora debe ser ISO 8601")
        else:
            parsed = v
        return _ensure_tz(parsed)

# -------------------- Endpoints --------------------
@router.post("", response_model=VisitaOut)
def crear_visita(
    payload: VisitaCreate,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    # lat/lng: siguen siendo opcionales; si viene solo uno y no el otro, lo aceptamos (no obligatorio).
    visita = Visita(
        user_id=uid,
        nombre=payload.nombre.strip(),
        apellido_paterno=(payload.apellido_paterno or "").strip(),
        apellido_materno=(payload.apellido_materno or "").strip(),
        telefono=payload.telefono,
        lat=payload.lat,
        lng=payload.lng,
        hora=payload.hora or _now(),
        adultos=payload.adultos if payload.adultos is not None else 0,
        notas=(payload.notas or "").strip(),
    )
    db.add(visita)
    db.commit()
    db.refresh(visita)
    return visita

@router.get("", response_model=VisitaListOut)
def listar_visitas(
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),

    # Filtros/paginación (opcionales)
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    from_dt: Optional[dt.datetime] = Query(None, alias="from"),
    to_dt: Optional[dt.datetime] = Query(None, alias="to"),
    with_location: Optional[bool] = Query(None, description="true=solo con lat/lng; false=solo sin; null=todos"),
):
    def _tz(v: Optional[dt.datetime]) -> Optional[dt.datetime]:
        return _ensure_tz(v) if v else None

    from_dt = _tz(from_dt)
    to_dt = _tz(to_dt)

    q = select(Visita).where(Visita.user_id == uid)

    if from_dt:
        q = q.where(Visita.hora >= from_dt)
    if to_dt:
        q = q.where(Visita.hora < to_dt)

    if with_location is True:
        q = q.where(Visita.lat.isnot(None), Visita.lng.isnot(None))
    elif with_location is False:
        q = q.where(Visita.lat.is_(None), Visita.lng.is_(None))

    total = db.execute(q.with_only_columns(func.count()).order_by(None)).scalar_one()
    items = db.execute(q.order_by(desc(Visita.hora)).limit(limit).offset(offset)).scalars().all()

    return VisitaListOut(items=items, total=total)

@router.get("/{visita_id}", response_model=VisitaOut)
def obtener_visita(
    visita_id: int,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    v = db.get(Visita, visita_id)
    if not v or v.user_id != uid:
        raise HTTPException(404, "Visita no encontrada")
    return v

@router.patch("/{visita_id}", response_model=VisitaOut)
def actualizar_visita(
    visita_id: int,
    payload: VisitaPatch,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    v = db.get(Visita, visita_id)
    if not v or v.user_id != uid:
        raise HTTPException(404, "Visita no encontrada")

    if payload.nombre is not None:
        v.nombre = payload.nombre.strip()
    if payload.apellido_paterno is not None:
        v.apellido_paterno = payload.apellido_paterno.strip()
    if payload.apellido_materno is not None:
        v.apellido_materno = payload.apellido_materno.strip()
    if payload.telefono is not None:
        v.telefono = payload.telefono

    if payload.lat is not None:
        v.lat = payload.lat
    if payload.lng is not None:
        v.lng = payload.lng
    if payload.hora is not None:
        v.hora = payload.hora
    if payload.adultos is not None:
        v.adultos = payload.adultos
    if payload.notas is not None:
        v.notas = payload.notas.strip()

    db.commit()
    db.refresh(v)
    return v
