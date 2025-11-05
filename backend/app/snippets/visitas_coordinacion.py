# backend/app/snippets/visitas_coordinacion.py
from __future__ import annotations

import os, re, datetime as dt, jwt
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

router = APIRouter(prefix="/coordinadores", tags=["coordinadores"])

# ---------------- Config & lazy init ----------------
_DB_URL   = os.getenv("DATABASE_URL")
_SECRET   = os.getenv("SECRET_KEY", "dev-change-me")
_ALG      = "HS256"

_engine: Optional[sa.Engine] = None
_SessionLocal: Optional[sessionmaker] = None
_inited = False

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/finalize")

def _init_db():
    """Inicializa engine y asegura la tabla app_user_coord (lazy init)."""
    global _engine, _SessionLocal, _inited
    if _inited:
        return
    if not _DB_URL:
        raise HTTPException(status_code=503, detail="DB no configurada (falta DATABASE_URL)")
    url = _DB_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    _engine = sa.create_engine(url, pool_pre_ping=True)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)

    # DDL idempotente para la tabla de vínculos (sin ORM)
    ddl = """
    CREATE TABLE IF NOT EXISTS app_user_coord (
      id SERIAL PRIMARY KEY,
      coordinador_id INTEGER NOT NULL REFERENCES app_user_auth(id),
      miembro_id     INTEGER NOT NULL,
      is_active      BOOLEAN NOT NULL DEFAULT TRUE,
      selected       BOOLEAN NOT NULL DEFAULT FALSE,
      created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    -- Índice único para permitir ON CONFLICT
    CREATE UNIQUE INDEX IF NOT EXISTS uq_app_user_coord_pair
      ON app_user_coord(coordinador_id, miembro_id);

    -- Índices útiles
    CREATE INDEX IF NOT EXISTS idx_app_user_coord_coor ON app_user_coord(coordinador_id);
    CREATE INDEX IF NOT EXISTS idx_app_user_coord_member ON app_user_coord(miembro_id);
    """
    with _engine.begin() as con:
        con.execute(sa.text(ddl))

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
    if not s: return ""
    if s.startswith("+"): return s
    if len(s) == 10: return "+52" + s
    if s.startswith("52") and len(s) == 12: return "+" + s
    return "+" + s

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

# ---------------- Helpers SQL ----------------
def _user_id_by_phone(db: Session, tel: str) -> Optional[int]:
    q = sa.text("SELECT id FROM app_user_auth WHERE telefono = :tel LIMIT 1")
    row = db.execute(q, {"tel": tel}).first()
    return int(row[0]) if row else None

def _user_name_phone(db: Session, uid: int):
    q = sa.text("""
        SELECT nombre, apellido_paterno, apellido_materno, telefono
        FROM app_user_auth WHERE id = :uid LIMIT 1
    """)
    row = db.execute(q, {"uid": uid}).first()
    if not row: return None, None, None, None
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
        raise HTTPException(400, "Teléfono inválido. Usa formato internacional, ej. +527771234567")

    coor_id = _user_id_by_phone(db, tel)
    if not coor_id:
        raise HTTPException(404, "No se encontró un usuario con ese teléfono.")
    if coor_id == uid:
        raise HTTPException(400, "No puedes agregarte como tu propio coordinador.")

    # UPSERT atómico: inserta o reactiva (is_active=TRUE) si ya existe
    q = sa.text("""
        INSERT INTO app_user_coord (coordinador_id, miembro_id, is_active, selected)
        VALUES (:coor, :mem, TRUE, FALSE)
        ON CONFLICT (coordinador_id, miembro_id)
        DO UPDATE SET is_active = TRUE
        RETURNING is_active;
    """)
    row = db.execute(q, {"coor": coor_id, "mem": uid}).first()
    db.commit()
    # si ya existía y estaba activo, el RETURNING seguirá siendo true, pero para el cliente es ok
    return AddCoordinadorOut(ok=True, coordinador_id=coor_id, already_linked=True)

@router.get("", response_model=List[CoordinadorOut])
def list_mis_coordinadores(
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
    include_inactivos: bool = Query(False),
):
    q = sa.text(f"""
        SELECT uc.coordinador_id,
               u.nombre, u.apellido_paterno, u.apellido_materno, u.telefono,
               uc.is_active
        FROM app_user_coord uc
        JOIN app_user_auth u ON u.id = uc.coordinador_id
        WHERE uc.miembro_id = :uid
        {"AND uc.is_active = TRUE" if not include_inactivos else ""}
        ORDER BY u.nombre NULLS LAST, u.apellido_paterno NULLS LAST
    """)
    rows = db.execute(q, {"uid": uid}).mappings().all()
    out: List[CoordinadorOut] = []
    for r in rows:
        full = " ".join(p for p in [r["nombre"], r["apellido_paterno"], r["apellido_materno"]] if p)
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
    # valida existencia
    chk = sa.text("""
        SELECT id FROM app_user_coord
        WHERE coordinador_id = :coor AND miembro_id = :mem LIMIT 1
    """)
    row = db.execute(chk, {"coor": coordinador_id, "mem": uid}).first()
    if not row:
        raise HTTPException(404, "Relación no encontrada.")

    upd = sa.text("""
        UPDATE app_user_coord
        SET is_active = :act
        WHERE coordinador_id = :coor AND miembro_id = :mem
    """)
    db.execute(upd, {"act": activo, "coor": coordinador_id, "mem": uid})
    db.commit()

    n, ap, am, tel = _user_name_phone(db, coordinador_id)
    full = " ".join(p for p in [n, ap, am] if p)
    return CoordinadorOut(
        coordinador_id=coordinador_id,
        nombre=full or None,
        telefono=tel,
        activo=bool(activo),
    )

@router.get("/mis-miembros", response_model=List[MiembroOut])
def list_mis_miembros(
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
    include_inactivos: bool = Query(False),
):
    q = sa.text(f"""
        SELECT u.id AS miembro_id,
               u.nombre, u.apellido_paterno, u.apellido_materno, u.telefono,
               uc.selected, uc.is_active
        FROM app_user_coord uc
        JOIN app_user_auth u ON u.id = uc.miembro_id
        WHERE uc.coordinador_id = :uid
        {"AND uc.is_active = TRUE" if not include_inactivos else ""}
        ORDER BY u.nombre NULLS LAST, u.apellido_paterno NULLS LAST
    """)
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
        for r in db.execute(q, {"uid": uid}).mappings().all()
    ]

@router.patch("/mis-miembros/{miembro_id}", response_model=MiembroOut)
def update_miembro_por_coordinador(
    miembro_id: int,
    body: UpdateMemberBody,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    # valida existencia
    chk = sa.text("""
        SELECT id FROM app_user_coord
        WHERE coordinador_id = :coor AND miembro_id = :mem LIMIT 1
    """)
    row = db.execute(chk, {"coor": uid, "mem": miembro_id}).first()
    if not row:
        raise HTTPException(404, "No tienes asignado a este miembro.")

    sets = []
    params = {"coor": uid, "mem": miembro_id}
    if body.selected is not None:
        sets.append("selected = :sel")
        params["sel"] = bool(body.selected)
    if body.activo is not None:
        sets.append("is_active = :act")
        params["act"] = bool(body.activo)

    if sets:
        upd = sa.text(f"""
            UPDATE app_user_coord
            SET {", ".join(sets)}
            WHERE coordinador_id = :coor AND miembro_id = :mem
        """)
        db.execute(upd, params)
        db.commit()

    n, ap, am, tel = _user_name_phone(db, miembro_id)
    # lee el estado actualizado
    st = sa.text("""
        SELECT selected, is_active FROM app_user_coord
        WHERE coordinador_id = :coor AND miembro_id = :mem
        LIMIT 1
    """)
    strow = db.execute(st, {"coor": uid, "mem": miembro_id}).first()
    return MiembroOut(
        miembro_id=miembro_id,
        nombre=n, apellido_paterno=ap, apellido_materno=am, telefono=tel,
        selected=bool(strow[0]) if strow else False,
        activo=bool(strow[1]) if strow else False,
    )
