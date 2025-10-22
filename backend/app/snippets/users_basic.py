from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from typing import List
import os

from sqlalchemy import create_engine, String, Integer, UniqueConstraint
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column, Session

router = APIRouter(prefix="/users", tags=["users"])

_DATABASE_URL = os.getenv("DATABASE_URL")
_engine = None
_SessionLocal = None
_inited = False

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "app_user_basic"
    __table_args__ = (UniqueConstraint("telefono", name="uq_user_telefono"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nombre: Mapped[str] = mapped_column(String(120))
    apellido: Mapped[str] = mapped_column(String(120))
    telefono: Mapped[str] = mapped_column(String(32), index=True)

def _init_db():
    global _engine, _SessionLocal, _inited
    if _inited: return
    if not _DATABASE_URL: return
    url = _DATABASE_URL
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

class UserCreate(BaseModel):
    nombre: str = Field(min_length=1, max_length=120)
    apellido: str = Field(min_length=1, max_length=120)
    telefono: str = Field(min_length=7, max_length=32)

class UserOut(BaseModel):
    id: int
    nombre: str
    apellido: str
    telefono: str

def _clean_phone(phone: str) -> str:
    return "".join(ch for ch in phone.strip() if ch.isdigit() or ch == "+")

@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, db: Session = Depends(get_db)):
    tel = _clean_phone(payload.telefono)
    exists = db.query(User).filter(User.telefono == tel).first()
    if exists:
        raise HTTPException(status_code=409, detail="Teléfono ya registrado")
    u = User(nombre=payload.nombre.strip(), apellido=payload.apellido.strip(), telefono=tel)
    db.add(u); db.commit(); db.refresh(u)
    return UserOut(id=u.id, nombre=u.nombre, apellido=u.apellido, telefono=u.telefono)

@router.get("", response_model=List[UserOut])
def list_users(db: Session = Depends(get_db)):
    rows = db.query(User).order_by(User.id.desc()).all()
    return [UserOut(id=r.id, nombre=r.nombre, apellido=r.apellido, telefono=r.telefono) for r in rows]
