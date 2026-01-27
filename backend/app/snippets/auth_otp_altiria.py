# backend/app/snippets/auth_otp_altiria.py
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field
from typing import Optional, Literal
import os, datetime as dt, random, re, requests, jwt
from passlib.context import CryptContext

from sqlalchemy import (
    create_engine, String, Integer, DateTime, Boolean, UniqueConstraint, func
)
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column, Session
from sqlalchemy.exc import IntegrityError

router = APIRouter(prefix="/auth", tags=["auth-otp"])

# -------------------- Config & lazy init --------------------
_DB_URL = os.getenv("DATABASE_URL")
_SECRET = os.getenv("SECRET_KEY", "dev-change-me")
_ALG = "HS256"

EXPIRE_MIN = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "10080"))     # JWT de sesión (min)
OTP_TOKEN_TTL = int(os.getenv("OTP_VERIFY_TOKEN_TTL", "600"))           # minitoken tras verify-otp (seg)

ALT_HTTP_URL = os.getenv("ALTIRIA_HTTP_URL", "https://www.altiria.net:8443/api/http")
ALT_KEY = os.getenv("ALTIRIA_API_KEY", "")
ALT_SECRET = os.getenv("ALTIRIA_API_SECRET", "")
ALT_SENDER = os.getenv("ALTIRIA_SENDER", "")                            # opcional
ALT_DRY = os.getenv("ALTIRIA_DRY_RUN", "false").lower() == "true"
ALT_DEBUG = os.getenv("ALTIRIA_DEBUG", "false").lower() == "true"

OTP_TTL = int(os.getenv("OTP_CODE_TTL_SECONDS", "300"))                 # caducidad OTP (seg)
OTP_RESEND = int(os.getenv("OTP_RESEND_SECONDS", "60"))                 # throttle reenvío (seg)

_BYPASS_PHONE_DIGITS = os.getenv("OTP_BYPASS_PHONE", "7471932431").strip()
_BYPASS_CODE = os.getenv("OTP_BYPASS_CODE", "123456").strip()

_engine = None
_SessionLocal = None
_inited = False

# pbkdf2: sin límite 72 bytes (óptimo para OTP)
pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/finalize")

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "app_user_auth"
    __table_args__ = (UniqueConstraint("telefono", name="uq_user_tel"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nombre: Mapped[Optional[str]] = mapped_column(String(120), default="")
    apellido_paterno: Mapped[Optional[str]] = mapped_column(String(120), default="")
    apellido_materno: Mapped[Optional[str]] = mapped_column(String(120), default="")
    # NULL para poder “liberar” el número si se crea cuenta nueva
    telefono: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)
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
    try: yield db
    finally: db.close()

# -------------------- Schemas --------------------
class SendOtpIn(BaseModel):
    telefono: str = Field(min_length=7, max_length=32)

class VerifyOtpIn(BaseModel):
    telefono: str
    code: str = Field(min_length=4, max_length=8)

class UserPreview(BaseModel):
    id: int
    nombre: Optional[str] = ""
    apellido_paterno: Optional[str] = ""
    apellido_materno: Optional[str] = ""
    telefono: Optional[str] = None

class VerifyOtpOut(BaseModel):
    verified: bool
    otp_token: str
    exists: bool
    preview: Optional[UserPreview] = None

class FinalizeIn(BaseModel):
    otp_token: str
    action: Literal["use_existing", "new_account"]
    nombre: Optional[str] = None
    apellido_paterno: Optional[str] = None
    apellido_materno: Optional[str] = None

class UserOut(BaseModel):
    id: int
    telefono: Optional[str] = None
    nombre: Optional[str] = ""
    apellido_paterno: Optional[str] = ""
    apellido_materno: Optional[str] = ""
    is_active: bool

class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"

class FinalizeOut(TokenOut):
    pass

# -------------------- Helpers --------------------
def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)  # aware UTC

def _normalize_aware(ts: dt.datetime) -> dt.datetime:
    return ts.replace(tzinfo=dt.timezone.utc) if ts.tzinfo is None else ts.astimezone(dt.timezone.utc)

def _clean_phone(phone: str) -> str:
    return re.sub(r"[^\d+]", "", phone.strip())  # mantiene + y dígitos

def _digits_only(phone: str) -> str:
    return re.sub(r"\D", "", phone.strip())

def _normalize_mx(phone: str) -> str:
    cleaned = _clean_phone(phone)
    if cleaned.startswith("+"):
        return cleaned
    digits = _digits_only(phone)
    if len(digits) == 10:
        return f"+52{digits}"
    return cleaned

def _alt_dest(phone_e164: str) -> str:
    return phone_e164.replace("+", "")          # Altiria quiere sin +

def _jwt(payload: dict, minutes: int) -> str:
    exp = _now() + dt.timedelta(minutes=minutes)
    return jwt.encode({**payload, "exp": exp}, _SECRET, algorithm=_ALG)

def _decode(token: str) -> dict:
    return jwt.decode(token, _SECRET, algorithms=[_ALG])

def _call_altiria_http(dest: str, message: str):
    # ALTIRIA HTTP clásico: requiere 'cmd=sendsms'
    data = {
        "cmd": "sendsms",
        "apikey": ALT_KEY,
        "apisecret": ALT_SECRET,
        "dest": dest,
        "msg": message,
    }
    if ALT_SENDER:
        data["senderId"] = ALT_SENDER
    headers = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
    return requests.post(ALT_HTTP_URL, data=data, headers=headers, timeout=20)

def _send_sms_altiria(dest: str, message: str) -> dict:
    if ALT_DRY:
        return {"dry_run": True, "dest": dest, "msg": message}
    if not (ALT_KEY and ALT_SECRET):
        raise HTTPException(500, "Altiria no configurado (ALTIRIA_API_KEY/SECRET)")
    try:
        r = _call_altiria_http(dest, message)
    except requests.RequestException as ex:
        raise HTTPException(502, f"Error de red hacia Altiria: {ex}")
    if r.status_code >= 400:
        raise HTTPException(502, f"Altiria respondió {r.status_code}: {r.text[:400]}")
    return {"dry_run": False, "status": r.status_code, "body": r.text[:400]}

# -------------------- Endpoints --------------------
@router.post("/send-otp")
def send_otp(payload: SendOtpIn, db: Session = Depends(get_db)):
    digits = _digits_only(payload.telefono)
    if digits == _BYPASS_PHONE_DIGITS:
        tel = _normalize_mx(payload.telefono)
        u = db.query(User).filter(User.telefono == tel).first()
        exists = bool(u)
        preview = (
            {"id": u.id, "telefono": u.telefono, "nombre": u.nombre,
             "apellido_paterno": u.apellido_paterno, "apellido_materno": u.apellido_materno}
            if exists else None
        )
        return {"ok": True, "sent": False, "exists": exists, "preview": preview, "test_code": _BYPASS_CODE}

    tel = _clean_phone(payload.telefono)
    if not tel.startswith("+"):
        raise HTTPException(400, "El teléfono debe venir en formato internacional, ej. +527771234567")

    # throttle (normaliza aware)
    otp = db.query(OTP).filter(OTP.telefono == tel).order_by(OTP.id.desc()).first()
    if otp:
        last = _normalize_aware(otp.last_sent_at)
        delta = (_now() - last).total_seconds()
        if delta < OTP_RESEND:
            secs = int(OTP_RESEND - delta)
            raise HTTPException(429, f"Espera {max(secs,1)}s para reenviar el código")

    code = f"{random.randint(0, 999999):06d}"
    msg = f"Tu código Bonube es {code}. Expira en {OTP_TTL//60} min."
    info = _send_sms_altiria(_alt_dest(tel), msg)

    expires = _now() + dt.timedelta(seconds=OTP_TTL)
    if not otp:
        otp = OTP(telefono=tel, code_hash=pwd.hash(code), expires_at=expires, last_sent_at=_now())
        db.add(otp)
    else:
        otp.code_hash = pwd.hash(code)
        otp.expires_at = expires
        otp.last_sent_at = _now()
    db.commit()

    u = db.query(User).filter(User.telefono == tel).first()
    exists = bool(u)
    preview = (
        {"id": u.id, "telefono": u.telefono, "nombre": u.nombre,
         "apellido_paterno": u.apellido_paterno, "apellido_materno": u.apellido_materno}
        if exists else None
    )

    resp = {"ok": True, "sent": True, "exists": exists, "preview": preview}
    if ALT_DRY or ALT_DEBUG:
        resp["altiria"] = info
        if ALT_DRY:
            resp["test_code"] = code
    return resp

@router.post("/verify-otp", response_model=VerifyOtpOut)
def verify_otp(payload: VerifyOtpIn, db: Session = Depends(get_db)):
    digits = _digits_only(payload.telefono)
    if digits == _BYPASS_PHONE_DIGITS and payload.code == _BYPASS_CODE:
        tel = _normalize_mx(payload.telefono)
        u = db.query(User).filter(User.telefono == tel).first()
        otp_token = _jwt({"otp_phone": tel}, minutes=max(OTP_TOKEN_TTL // 60, 1))
        preview = (
            {"id": u.id, "telefono": u.telefono, "nombre": u.nombre,
             "apellido_paterno": u.apellido_paterno, "apellido_materno": u.apellido_materno}
            if u else None
        )
        return VerifyOtpOut(verified=True, otp_token=otp_token, exists=bool(u), preview=preview)

    tel = _clean_phone(payload.telefono)
    otp = db.query(OTP).filter(OTP.telefono == tel).order_by(OTP.id.desc()).first()
    if not otp:
        raise HTTPException(400, "Solicita primero el código")

    exp = _normalize_aware(otp.expires_at)
    if _now() > exp:
        raise HTTPException(400, "Código expirado")

    if not pwd.verify(payload.code, otp.code_hash):
        raise HTTPException(401, "Código incorrecto")

    u = db.query(User).filter(User.telefono == tel).first()
    otp_token = _jwt({"otp_phone": tel}, minutes=max(OTP_TOKEN_TTL // 60, 1))
    preview = (
        {"id": u.id, "telefono": u.telefono, "nombre": u.nombre,
         "apellido_paterno": u.apellido_paterno, "apellido_materno": u.apellido_materno}
        if u else None
    )
    return VerifyOtpOut(verified=True, otp_token=otp_token, exists=bool(u), preview=preview)

class FinalizeIn(BaseModel):
    otp_token: str
    action: Literal["use_existing", "new_account"]
    nombre: Optional[str] = None
    apellido_paterno: Optional[str] = None
    apellido_materno: Optional[str] = None

@router.post("/finalize", response_model=TokenOut)
def finalize(payload: FinalizeIn, db: Session = Depends(get_db)):
    # validar otp_token
    try:
        data = _decode(payload.otp_token)
        tel = data.get("otp_phone")
    except jwt.PyJWTError:
        raise HTTPException(401, "otp_token inválido o expirado")
    if not tel:
        raise HTTPException(400, "otp_token inválido")

    u = db.query(User).filter(User.telefono == tel).first()

    if payload.action == "use_existing":
        if not u:
            raise HTTPException(404, "No existe cuenta con ese teléfono (usa 'new_account')")
        return {"access_token": _jwt({"sub": str(u.id)}, minutes=EXPIRE_MIN), "token_type": "bearer"}

    if payload.action == "new_account":
        # liberar teléfono de la cuenta previa (si hay)
        if u:
            try:
                u.telefono = None
                db.commit()
            except IntegrityError:
                db.rollback()
                raise HTTPException(409, "No se pudo liberar el teléfono de la cuenta previa. "
                                          "Verifica restricciones en app_user_auth.telefono.")
        # crear nueva cuenta con el teléfono
        new_user = User(
            telefono=tel,
            nombre=(payload.nombre or "").strip(),
            apellido_paterno=(payload.apellido_paterno or "").strip(),
            apellido_materno=(payload.apellido_materno or "").strip(),
            is_active=True,
        )
        db.add(new_user); db.commit(); db.refresh(new_user)
        return {"access_token": _jwt({"sub": str(new_user.id)}, minutes=EXPIRE_MIN), "token_type": "bearer"}

    raise HTTPException(400, "action inválida (usa use_existing | new_account)")

@router.get("/me", response_model=UserOut)
def me(token: str = Depends(oauth2), db: Session = Depends(get_db)):
    try:
        uid = _decode(token).get("sub")
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
