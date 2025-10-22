# backend/app/snippets/users_create.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import List
import os
from sqlalchemy import create_engine, String, Integer
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column, Session

# --------- DB mínima (usa tu DATABASE_URL) ---------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Falta DATABASE_URL en ENV para users_create.py")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "app_user_min"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nombre: Mapped[str] = mapped_column(String(120))
    apellido: Mapped[str] = mapped_column(String(120))

Base.metadata.create_all(bind=engine)

def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --------- Esquemas ---------
class UserCreate(BaseModel):
    nombre: str = Field(min_length=1, max_length=120)
    apellido: str = Field(min_length=1, max_length=120)

class UserOut(BaseModel):
    id: int
    nombre: str
    apellido: str

# --------- Router (se monta bajo /api/v1) ---------
router = APIRouter(prefix="/users", tags=["users"])

@router.post("", response_model=UserOut, status_code=201)
def create_user(payload: UserCreate, db: Session = Depends(get_db)):
    user = User(nombre=payload.nombre.strip(), apellido=payload.apellido.strip())
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut(id=user.id, nombre=user.nombre, apellido=user.apellido)

@router.get("", response_model=List[UserOut])
def list_users(db: Session = Depends(get_db)):
    rows = db.query(User).order_by(User.id.desc()).all()
    return [UserOut(id=r.id, nombre=r.nombre, apellido=r.apellido) for r in rows]
