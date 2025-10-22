# backend/app/snippets/auth_basic.py
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field
from typing import Optional
import os, datetime, traceback, jwt

from sqlalchemy import create_engine, String, Integer, DateTime, Boolean, UniqueConstraint, func
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column, Session
from passlib.context import CryptContext

router = APIRouter(prefix="/auth", tags=["auth"])

# -------------------- Config & Lazy-Init --------------------
_DATABASE_URL = os.getenv("DATABASE_URL")
_SECRET_KEY = os.getenv("SECRET_KEY", "dev-change-me")
_ALG = "HS256"
_EXPIRE_MIN = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "10080"))  # 7 días por defecto

_engine = None
_SessionLocal = None
_inited = False
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "app_user_auth"
    __table_args__ = (UniqueConstraint("telefono", name="uq_user_tel"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nombre: Mapped[str] = mapped_column(String(120))
    apellido_paterno: Mapped[str] = mapped_column(String(120))
    apellido_materno: Mapped[str] = mapped_column(String(120))
    telefono: Mapped[str] = mapped_column(String(32), index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped = mapped_column(DateTime(timezone=True), server_default=func.now())

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

# -------------------- Schemas --------------------
class RegisterIn(BaseModel):
    nombre: str = Field(min_length=1, max_length=120)
    apellido_paterno: str = Field(min_length=1, max_length=120)
    apellido_materno: str = Field(min_length=1, max_length=120)
    telefono: str = Field(min_length=7, max_length=32)
    password: str = Field(min_length=6, max_length=128)

class LoginIn(BaseModel):
    telefono: str
    password: str

class UserOut(BaseModel):
    id: int
    nombre: str
    apellido_paterno: str
    apellido_materno: str
    telefono: str
    is_active: bool

class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"

# -------------------- Helpers --------------------
def _clean_phone(phone: str) -> str:
    return "".join(ch for ch in phone.strip() if ch.isdigit() or ch == "+")

def _hash(pw: str) -> str:
    return pwd.hash(pw)

def _verify(pw: str, h: str) -> bool:
    return pwd.verify(pw, h)

def _create_access_token(sub: str) -> str:
    exp = datetime.datetime.utcnow() + datetime.timedelta(minutes=_EXPIRE_MIN)
    return jwt.encode({"sub": sub, "exp": exp}, _SECRET_KEY, algorithm=_ALG)

def _decode(token: str) -> dict:
    try:
        return jwt.decode(token, _SECRET_KEY, algorithms=[_ALG])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Token inválido")

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

def _get_current_user(token: str = Depends(oauth2), db: Session = Depends(get_db)) -> User:
    uid = _decode(token).get("sub")
    if not uid: raise HTTPException(401, "Token inválido")
    u = db.query(User).filter(User.id == int(uid)).first()
    if not u or not u.is_active: raise HTTPException(401, "Usuario no encontrado o inactivo")
    return u

# -------------------- Endpoints --------------------
@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterIn, db: Session = Depends(get_db)):
    tel = _clean_phone(payload.telefono)
    if db.query(User).filter(User.telefono == tel).first():
        raise HTTPException(status_code=409, detail="Teléfono ya registrado")
    u = User(
        nombre=payload.nombre.strip(),
        apellido_paterno=payload.apellido_paterno.strip(),
        apellido_materno=payload.apellido_materno.strip(),
        telefono=tel,
        password_hash=_hash(payload.password),
        is_active=True,
    )
    db.add(u); db.commit(); db.refresh(u)
    return UserOut(
        id=u.id, nombre=u.nombre, apellido_paterno=u.apellido_paterno,
        apellido_materno=u.apellido_materno, telefono=u.telefono, is_active=u.is_active
    )

@router.post("/login", response_model=TokenOut)
def login(payload: LoginIn, db: Session = Depends(get_db)):
    tel = _clean_phone(payload.telefono)
    u = db.query(User).filter(User.telefono == tel).first()
    if not u or not _verify(payload.password, u.password_hash):
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    token = _create_access_token(sub=str(u.id))
    return TokenOut(access_token=token)

@router.get("/me", response_model=UserOut)
def me(current: User = Depends(_get_current_user)):
    return UserOut(
        id=current.id, nombre=current.nombre, apellido_paterno=current.apellido_paterno,
        apellido_materno=current.apellido_materno, telefono=current.telefono, is_active=current.is_active
    )
