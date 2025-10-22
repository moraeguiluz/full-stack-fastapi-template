# backend/app/snippets/auth_otp_altiria.py
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field
from typing import Optional
import os, datetime as dt, random, re, requests, jwt
from passlib.context import CryptContext

from sqlalchemy import (
    create_engine, String, Integer, DateTime, Boolean, UniqueConstraint, func
)
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column, Session

router = APIRouter(prefix="/auth", tags=["auth-otp"])

# -------------------- Config & lazy init --------------------
_DB_URL = os.getenv("DATABASE_URL")
_SECRET = os.getenv("SECRET_KEY", "dev-change-me")
_ALG = "HS256"
_EXPIRE_MIN = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "10080"))  # 7 días

_ALT_HTTP_URL = os.getenv("ALTIRIA_HTTP_URL", "https://www.altiria.net:8443/api/http")
_ALT_KEY = os.getenv("ALTIRIA_API_KEY", "")
_ALT_SECRET = os.getenv("ALTIRIA_API_SECRET", "")
_ALT_SENDER = os.getenv("ALTIRIA_SENDER", "")

_OTP_TTL = int(os.getenv("OTP_CODE_TTL_SECONDS", "300"))
_OTP_RESEND = int(os.getenv("OTP_RESEND_SECONDS", "60"))

_engine = None
_SessionLocal = None
_inited = False
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/verify-otp")

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "app_user_auth"
    __table_args__ = (UniqueConstraint("telefono", name="uq_user_tel"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nombre: Mapped[Optional[str]] = mapped_column(String(120), default="")
    apellido_paterno: Mapped[Optional[str]] = mapped_column(String(120), default="")
    apellido_materno: Mapped[Optional[str]] = mapped_column(String(120), default="")
    telefono: Mapped[str] = mapped_column(String(32), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class OTP(Base):
    __tablename__ = "app_user_otp"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(32), index=True)
    code_hash: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    last_sent_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

def _init_db():
    global _engine, _SessionLocal, _inited
    if _inited: return
    if not _DB_URL: return
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

# -------------------- Schemas --------------------
class SendOtpIn(BaseModel):
    telefono: str = Field(min_length=7, max_length=32)
    nombre: Optional[str] = None
    apellido_paterno: Optional[str] = None
    apellido_materno: Optional[str] = None

class VerifyOtpIn(BaseModel):
    telefono: str
    code: str = Field(min_length=4, max_length=8)

class UserOut(BaseModel):
    id: int
    telefono: str
    nombre: Optional[str] = ""
    apellido_paterno: Optional[str] = ""
    apellido_materno: Optional[str] = ""
    is_active: bool

class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"

# -------------------- Helpers --------------------
def _now() -> dt.datetime:
    return dt.datetime.utcnow()

def _clean_phone(phone: str) -> str:
    return re.sub(r"[^\d+]", "", phone.strip())

def _alt_dest(phone_e164: str) -> str:
    return phone_e164.replace("+", "")

def _create_access_token(sub: str) -> str:
    exp = _now() + dt.timedelta(minutes=_EXPIRE_MIN)
    return jwt.encode({"sub": sub, "exp": exp}, _SECRET, algorithm=_ALG)

def _hash_code(code: str) -> str:
    return pwd.hash(code)

def _verify_code(code: str, h: str) -> bool:
    return pwd.verify(code, h)

def _must_wait(last_sent: dt.datetime) -> bool:
    return (_now() - last_sent).total_seconds() < _OTP_RESEND

def _gen_code() -> str:
    return f"{random.randint(0, 999999):06d}"

def _send_sms_altiria(dest: str, message: str) -> None:
    if not (_ALT_KEY and _ALT_SECRET):
        raise HTTPException(500, "Altiria no configurado (ALTIRIA_API_KEY/SECRET)")
    data = {"apikey": _ALT_KEY, "apisecret": _ALT_SECRET, "dest": dest, "msg": message}
    if _ALT_SENDER:
        data["senderId"] = _ALT_SENDER
    headers = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
    try:
        r = requests.post(_ALT_HTTP_URL, data=data, headers=headers, timeout=20)
    except requests.RequestException as ex:
        raise HTTPException(502, f"Error de red hacia Altiria: {ex}")
    if r.status_code >= 400:
        raise HTTPException(502, f"Altiria respondió {r.status_code}: {r.text[:200]}")

# -------------------- Endpoints --------------------
@router.post("/send-otp", status_code=status.HTTP_200_OK)
def send_otp(payload: SendOtpIn, db: Session = Depends(get_db)):
    tel = _clean_phone(payload.telefono)
    if not tel.startswith("+"):
        raise HTTPException(400, "El teléfono debe venir en formato internacional, ej. +527771234567")
    otp = db.query(OTP).filter(OTP.telefono == tel).order_by(OTP.id.desc()).first()
    if otp and _must_wait(otp.last_sent_at):
        segundos = int(_OTP_RESEND - (_now() - otp.last_sent_at).total_seconds())
        raise HTTPException(429, f"Espera {max(segundos,1)}s para reenviar el código")
    code = _gen_code()
    msg = f"Tu código Bonube es {code}. Expira en {_OTP_TTL//60} min."
    _send_sms_altiria(_alt_dest(tel), msg)
    expires = _now() + dt.timedelta(seconds=_OTP_TTL)
    if not otp:
        otp = OTP(telefono=tel, code_hash=_hash_code(code), expires_at=expires, last_sent_at=_now())
        db.add(otp)
    else:
        otp.code_hash = _hash_code(code)
        otp.expires_at = expires
        otp.last_sent_at = _now()
    db.commit()
    u = db.query(User).filter(User.telefono == tel).first()
    if not u:
        u = User(
            telefono=tel,
            nombre=(payload.nombre or "").strip(),
            apellido_paterno=(payload.apellido_paterno or "").strip(),
            apellido_materno=(payload.apellido_materno or "").strip(),
            is_active=True,
        )
        db.add(u); db.commit()
    return {"ok": True, "sent": True}

@router.post("/verify-otp", response_model=TokenOut)
def verify_otp(payload: VerifyOtpIn, db: Session = Depends(get_db)):
    tel = _clean_phone(payload.telefono)
    otp = db.query(OTP).filter(OTP.telefono == tel).order_by(OTP.id.desc()).first()
    if not otp:
        raise HTTPException(400, "Solicita primero el código")
    if _now() > otp.expires_at:
        raise HTTPException(400, "Código expirado")
    if not _verify_code(payload.code, otp.code_hash):
        raise HTTPException(401, "Código incorrecto")
    u = db.query(User).filter(User.telefono == tel).first()
    if not u:
        u = User(telefono=tel, is_active=True)
        db.add(u); db.commit(); db.refresh(u)
    token = _create_access_token(sub=str(u.id))
    return TokenOut(access_token=token)

@router.get("/me", response_model=UserOut)
def me(token: str = Depends(oauth2), db: Session = Depends(get_db)):
    try:
        uid = jwt.decode(token, _SECRET, algorithms=[_ALG]).get("sub")
    except jwt.PyJWTError:
        raise HTTPException(401, "Token inválido")
    u = db.query(User).filter(User.id == int(uid)).first()
    if not u or not u.is_active:
        raise HTTPException(401, "Usuario no encontrado o inactivo")
    return UserOut(
        id=u.id, telefono=u.telefono, nombre=u.nombre,
        apellido_paterno=u.apellido_paterno, apellido_materno=u.apellido_materno,
        is_active=u.is_active
    )
