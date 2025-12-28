# backend/app/snippets/insignias.py
from __future__ import annotations

import os, json, datetime as dt, jwt
from typing import Optional, List, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field

from sqlalchemy import (
    create_engine, select, and_, func, Float, Integer, String, DateTime, Boolean,
    UniqueConstraint, text
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session, sessionmaker
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

router = APIRouter(prefix="/insignias", tags=["insignias"])

# -------------------- Config & lazy init --------------------
_DB_URL = os.getenv("DATABASE_URL")
_SECRET = os.getenv("SECRET_KEY", "dev-change-me")
_ALG = "HS256"

# TEMP: permitir admin a cualquier usuario autenticado para pruebas.
# Pon OPEN_INSIGNIAS_ADMIN=false para volver a restringir.
_OPEN_INSIGNIAS_ADMIN = os.getenv("OPEN_INSIGNIAS_ADMIN", "true").lower() == "true"

# Si OPEN_INSIGNIAS_ADMIN=false, entonces se usa ADMIN_USER_IDS="1,2,3"
_ADMIN_USER_IDS = {int(x) for x in (os.getenv("ADMIN_USER_IDS", "")).split(",") if x.strip().isdigit()}

_engine = None
_SessionLocal: Optional[sessionmaker] = None
_inited = False

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/finalize")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _ensure_tz(ts: Optional[dt.datetime]) -> Optional[dt.datetime]:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


def _current_user_id(token: str = Depends(oauth2)) -> int:
    try:
        data = jwt.decode(token, _SECRET, algorithms=[_ALG])
    except Exception:
        raise HTTPException(401, "Token inválido")
    uid = data.get("sub")
    if not uid:
        raise HTTPException(401, "Token inválido")
    return int(uid)


def _require_admin(uid: int):
    if _OPEN_INSIGNIAS_ADMIN:
        return
    if not _ADMIN_USER_IDS:
        raise HTTPException(403, "Admin no configurado (define ADMIN_USER_IDS o OPEN_INSIGNIAS_ADMIN=true)")
    if uid not in _ADMIN_USER_IDS:
        raise HTTPException(403, "No autorizado (admin)")


class Base(DeclarativeBase):
    pass


# -------------------- Models --------------------

class Visit(Base):
    """
    Mapeo mínimo de app_visita (YA existe).
    """
    __tablename__ = "app_visita"
    __table_args__ = {"extend_existing": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    codigo_base: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True, index=True)
    lng: Mapped[Optional[float]] = mapped_column(Float, nullable=True, index=True)
    hora: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)


InsigniaType = Literal["COUNT_TOTAL", "COUNT_IN_POLYGON", "AT_LOCATION"]


class Insignia(Base):
    """
    Catálogo unificado.
    - geom_json: GeoJSON (Polygon/MultiPolygon) cuando tipo=COUNT_IN_POLYGON
    - requisitos: JSONB flexible (required_visits, window_days, etc.)
    - display: JSONB flexible (condiciones UI)
    """
    __tablename__ = "app_insignia"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    codigo_base: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    tipo: Mapped[str] = mapped_column(String(32), index=True)

    titulo: Mapped[str] = mapped_column(String(160), default="")
    image_url: Mapped[str] = mapped_column(String(600), default="")
    orden: Mapped[int] = mapped_column(Integer, default=0, index=True)
    activa: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    requisitos: Mapped[dict] = mapped_column(JSONB, default=dict)
    display: Mapped[dict] = mapped_column(JSONB, default=dict)

    # IMPORTANTÍSIMO:
    # none_as_null=True evita guardar JSON null ('null'::jsonb). En vez de eso guarda SQL NULL.
    # Esto evita que índices/funciones PostGIS se rompan con geom_json = 'null'.
    geom_json: Mapped[Optional[dict]] = mapped_column(JSONB(none_as_null=True), nullable=True)
    bbox: Mapped[Optional[dict]] = mapped_column(JSONB(none_as_null=True), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class InsigniaClaim(Base):
    __tablename__ = "app_insignia_claim"
    __table_args__ = (
        UniqueConstraint("user_id", "insignia_id", name="uq_insignia_claim_user_insignia"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    insignia_id: Mapped[int] = mapped_column(Integer, index=True)

    status: Mapped[str] = mapped_column(String(24), default="claimed")  # claimed | pending | rejected
    claimed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)


# -------------------- DB init --------------------

def _init_db():
    """
    Lazy init:
    - crea tablas al primer uso
    - intenta crear índice espacial funcional (PostGIS) de forma segura
    """
    global _engine, _SessionLocal, _inited
    if _inited or not _DB_URL:
        return

    url = _DB_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)

    _engine = create_engine(url, pool_pre_ping=True)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)

    Base.metadata.create_all(bind=_engine)

    # Índice espacial funcional (PARCIAL) para evitar GeoJSON inválido:
    # - solo indexa cuando geom_json está presente y NO es JSON null.
    # - si PostGIS no está habilitado, fallará, pero no rompemos el arranque.
    try:
        with _engine.begin() as conn:
            conn.exec_driver_sql("""
            CREATE INDEX IF NOT EXISTS app_insignia_geom_gix
            ON app_insignia
            USING GIST ((ST_SetSRID(ST_GeomFromGeoJSON(geom_json::text), 4326)))
            WHERE geom_json IS NOT NULL AND geom_json::text <> 'null';
            """)
    except Exception:
        pass

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


# -------------------- Helpers (rules) --------------------

def _required_visits(ins: Insignia) -> int:
    try:
        rv = int((ins.requisitos or {}).get("required_visits", 1))
        return max(rv, 1)
    except Exception:
        return 1


def _window_days(ins: Insignia) -> Optional[int]:
    v = (ins.requisitos or {}).get("window_days")
    if v is None:
        return None
    try:
        d = int(v)
        return d if d > 0 else None
    except Exception:
        return None


def _count_visits_total(db: Session, uid: int, codigo_base: Optional[str], days: Optional[int]) -> int:
    conds = [Visit.user_id == uid]
    if codigo_base:
        conds.append(Visit.codigo_base == codigo_base)
    if days:
        conds.append(Visit.hora >= (_now() - dt.timedelta(days=days)))

    q = select(func.count()).select_from(Visit).where(and_(*conds))
    return int(db.execute(q).scalar_one())


def _count_visits_in_polygon(db: Session, uid: int, codigo_base: Optional[str], days: Optional[int], geom_json: dict) -> int:
    if not geom_json:
        return 0

    geom_str = json.dumps(geom_json)
    from_dt = (_now() - dt.timedelta(days=days)) if days else None

    # PostGIS requerido
    sql = """
    SELECT COUNT(*)
    FROM app_visita v
    WHERE v.user_id = :uid
      AND (:codigo_base IS NULL OR v.codigo_base = :codigo_base)
      AND v.lat IS NOT NULL AND v.lng IS NOT NULL
      AND (:from_dt IS NULL OR v.hora >= :from_dt)
      AND ST_COVERS(
            ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326),
            ST_SetSRID(ST_MakePoint(v.lng, v.lat), 4326)
          );
    """
    try:
        row = db.execute(
            text(sql),
            {"uid": uid, "codigo_base": codigo_base, "from_dt": from_dt, "geom": geom_str},
        ).scalar_one()
    except Exception as e:
        raise HTTPException(500, f"Error PostGIS/GeoJSON al verificar polígono: {e}")
    return int(row)


def _already_claimed(db: Session, uid: int, insignia_id: int) -> Optional[InsigniaClaim]:
    return db.execute(
        select(InsigniaClaim).where(
            InsigniaClaim.user_id == uid,
            InsigniaClaim.insignia_id == insignia_id
        )
    ).scalars().first()


def _bbox_from_geojson(geom_json: dict) -> Optional[dict]:
    """
    Calcula bbox {minLat,minLng,maxLat,maxLng} desde GeoJSON Polygon/MultiPolygon.
    """
    if not isinstance(geom_json, dict):
        return None
    t = geom_json.get("type")
    coords = geom_json.get("coordinates")
    if t not in ("Polygon", "MultiPolygon") or not coords:
        return None

    lats: List[float] = []
    lngs: List[float] = []

    def add_ring(ring):
        for pt in ring:
            if not isinstance(pt, (list, tuple)) or len(pt) < 2:
                continue
            lng, lat = pt[0], pt[1]
            try:
                lngs.append(float(lng))
                lats.append(float(lat))
            except Exception:
                continue

    if t == "Polygon":
        for ring in coords:
            add_ring(ring)
    else:
        for poly in coords:
            for ring in poly:
                add_ring(ring)

    if not lats or not lngs:
        return None

    return {
        "minLat": min(lats),
        "minLng": min(lngs),
        "maxLat": max(lats),
        "maxLng": max(lngs),
    }


# -------------------- Schemas --------------------

class InsigniaOut(BaseModel):
    id: int
    codigo_base: Optional[str] = None
    tipo: str
    titulo: str
    image_url: str
    orden: int
    activa: bool
    requisitos: dict = Field(default_factory=dict)
    display: dict = Field(default_factory=dict)
    geom_json: Optional[dict] = None
    bbox: Optional[dict] = None

    claimed: bool = False
    claim_status: Optional[str] = None
    claimed_at: Optional[dt.datetime] = None

    class Config:
        from_attributes = True


class CatalogOut(BaseModel):
    items: List[InsigniaOut]
    total: int


class ClaimReq(BaseModel):
    evidence: Optional[dict] = Field(default_factory=dict)


class ClaimResp(BaseModel):
    ok: bool
    claimed: bool
    status: str
    required_visits: int
    counted_visits: int
    reason: Optional[str] = None


class InsigniaCreate(BaseModel):
    codigo_base: Optional[str] = Field(default=None, max_length=64)
    tipo: InsigniaType

    titulo: Optional[str] = Field(default=None, max_length=160)
    image_url: Optional[str] = Field(default="", max_length=600)
    orden: int = 0
    activa: bool = True

    requisitos: dict = Field(default_factory=dict)
    display: dict = Field(default_factory=dict)

    geom_json: Optional[dict] = None
    bbox: Optional[dict] = None


class InsigniaUpdate(BaseModel):
    codigo_base: Optional[str] = Field(default=None, max_length=64)
    tipo: Optional[InsigniaType] = None

    titulo: Optional[str] = Field(default=None, max_length=160)
    image_url: Optional[str] = Field(default=None, max_length=600)
    orden: Optional[int] = None
    activa: Optional[bool] = None

    requisitos: Optional[dict] = None
    display: Optional[dict] = None

    geom_json: Optional[dict] = None
    bbox: Optional[dict] = None


# -------------------- Public endpoints --------------------

@router.get("/catalogo", response_model=CatalogOut)
def catalogo(
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
    codigo_base: Optional[str] = Query(None, max_length=64),
    include_inactive: bool = Query(False),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conds = []
    if codigo_base:
        conds.append(Insignia.codigo_base == codigo_base)
    if not include_inactive:
        conds.append(Insignia.activa == True)  # noqa: E712

    base_q = select(Insignia).where(and_(*conds)).order_by(Insignia.orden.asc(), Insignia.id.asc())
    total = int(db.execute(select(func.count()).select_from(base_q.subquery())).scalar_one())
    items = db.execute(base_q.limit(limit).offset(offset)).scalars().all()

    ids = [x.id for x in items]
    claims = {}
    if ids:
        rows = db.execute(
            select(InsigniaClaim).where(
                InsigniaClaim.user_id == uid,
                InsigniaClaim.insignia_id.in_(ids)
            )
        ).scalars().all()
        claims = {c.insignia_id: c for c in rows}

    out: List[InsigniaOut] = []
    for it in items:
        c = claims.get(it.id)
        out.append(InsigniaOut(
            **{k: getattr(it, k) for k in [
                "id","codigo_base","tipo","titulo","image_url","orden","activa",
                "requisitos","display","geom_json","bbox"
            ]},
            claimed=bool(c),
            claim_status=c.status if c else None,
            claimed_at=c.claimed_at if c else None,
        ))

    return CatalogOut(items=out, total=total)


@router.get("/{insignia_id:int}", response_model=InsigniaOut)
def detalle_insignia(
    insignia_id: int,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    ins = db.get(Insignia, insignia_id)
    if not ins:
        raise HTTPException(404, "Insignia no encontrada")

    c = _already_claimed(db, uid, insignia_id)
    return InsigniaOut(
        **{k: getattr(ins, k) for k in [
            "id","codigo_base","tipo","titulo","image_url","orden","activa",
            "requisitos","display","geom_json","bbox"
        ]},
        claimed=bool(c),
        claim_status=c.status if c else None,
        claimed_at=c.claimed_at if c else None,
    )


@router.post("/{insignia_id:int}/claim", response_model=ClaimResp)
def reclamar(
    insignia_id: int,
    payload: ClaimReq,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    prev = _already_claimed(db, uid, insignia_id)
    if prev:
        return ClaimResp(ok=True, claimed=True, status=prev.status, required_visits=0, counted_visits=0, reason="Ya estaba reclamada")

    ins = db.get(Insignia, insignia_id)
    if not ins or not ins.activa:
        raise HTTPException(404, "Insignia no encontrada o inactiva")

    req = _required_visits(ins)
    days = _window_days(ins)

    if ins.tipo == "COUNT_TOTAL":
        counted = _count_visits_total(db, uid, ins.codigo_base, days)
    elif ins.tipo == "COUNT_IN_POLYGON":
        if not ins.geom_json:
            raise HTTPException(500, "Insignia geográfica mal configurada (falta geom_json)")
        counted = _count_visits_in_polygon(db, uid, ins.codigo_base, days, ins.geom_json)
    else:
        raise HTTPException(400, f"Tipo de insignia no soportado aún: {ins.tipo}")

    if counted < req:
        return ClaimResp(ok=True, claimed=False, status="locked", required_visits=req, counted_visits=counted, reason="Aún no cumples los requisitos")

    claim = InsigniaClaim(user_id=uid, insignia_id=insignia_id, status="claimed", evidence=payload.evidence or {})
    db.add(claim)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return ClaimResp(ok=True, claimed=True, status="claimed", required_visits=req, counted_visits=counted, reason="Reclamada (race)")
    db.refresh(claim)
    return ClaimResp(ok=True, claimed=True, status=claim.status, required_visits=req, counted_visits=counted)


# -------------------- Admin endpoints --------------------

@router.post("/admin", response_model=InsigniaOut)
def admin_crear(
    payload: InsigniaCreate,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    _require_admin(uid)

    geom = payload.geom_json
    bbox = payload.bbox

    if payload.tipo == "COUNT_IN_POLYGON":
        if not geom:
            raise HTTPException(400, "Para COUNT_IN_POLYGON debes enviar geom_json")
        if not bbox:
            bbox = _bbox_from_geojson(geom)
    else:
        geom = None
        bbox = None

    ins = Insignia(
        codigo_base=(payload.codigo_base or None),
        tipo=payload.tipo,
        titulo=(payload.titulo or "").strip(),
        image_url=(payload.image_url or "").strip(),
        orden=payload.orden,
        activa=payload.activa,
        requisitos=payload.requisitos or {},
        display=payload.display or {},
        geom_json=geom,
        bbox=bbox,
    )
    db.add(ins)
    db.flush()

    if not ins.titulo:
        ins.titulo = f"Insignia {ins.id}"

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(400, f"No se pudo crear insignia: {e}")
    db.refresh(ins)

    return InsigniaOut(
        **{k: getattr(ins, k) for k in [
            "id","codigo_base","tipo","titulo","image_url","orden","activa",
            "requisitos","display","geom_json","bbox"
        ]},
        claimed=False,
    )


def _apply_update(ins: Insignia, payload: InsigniaUpdate):
    if payload.codigo_base is not None:
        ins.codigo_base = payload.codigo_base or None
    if payload.tipo is not None:
        ins.tipo = payload.tipo

    if payload.titulo is not None:
        ins.titulo = payload.titulo.strip()
    if payload.image_url is not None:
        ins.image_url = (payload.image_url or "").strip()
    if payload.orden is not None:
        ins.orden = int(payload.orden)
    if payload.activa is not None:
        ins.activa = bool(payload.activa)

    if payload.requisitos is not None:
        ins.requisitos = payload.requisitos
    if payload.display is not None:
        ins.display = payload.display

    if payload.geom_json is not None:
        ins.geom_json = payload.geom_json
        if payload.bbox is None and payload.geom_json:
            ins.bbox = _bbox_from_geojson(payload.geom_json)

    if payload.bbox is not None:
        ins.bbox = payload.bbox

    if ins.tipo != "COUNT_IN_POLYGON":
        ins.geom_json = None
        ins.bbox = None


@router.patch("/admin/{insignia_id:int}", response_model=InsigniaOut)
def admin_actualizar_patch(
    insignia_id: int,
    payload: InsigniaUpdate,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    _require_admin(uid)

    ins = db.get(Insignia, insignia_id)
    if not ins:
        raise HTTPException(404, "Insignia no encontrada")

    _apply_update(ins, payload)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(400, f"No se pudo actualizar: {e}")
    db.refresh(ins)

    return InsigniaOut(
        **{k: getattr(ins, k) for k in [
            "id","codigo_base","tipo","titulo","image_url","orden","activa",
            "requisitos","display","geom_json","bbox"
        ]},
        claimed=False,
    )


# Wrapper POST para tu ApiClient (no tiene patch)
@router.post("/admin/{insignia_id:int}", response_model=InsigniaOut)
def admin_actualizar_post(
    insignia_id: int,
    payload: InsigniaUpdate,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    return admin_actualizar_patch(insignia_id, payload, db, uid)


@router.delete("/admin/{insignia_id:int}")
def admin_delete(
    insignia_id: int,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    _require_admin(uid)

    ins = db.get(Insignia, insignia_id)
    if not ins:
        raise HTTPException(404, "Insignia no encontrada")

    ins.activa = False
    try:
        d = ins.display or {}
        d["deleted"] = True
        ins.display = d
    except Exception:
        pass

    db.commit()
    return {"ok": True, "deleted": True, "soft": True, "id": insignia_id}


# Wrapper POST para tu ApiClient (no tiene delete)
@router.post("/admin/{insignia_id:int}/delete")
def admin_delete_post(
    insignia_id: int,
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
):
    return admin_delete(insignia_id, db, uid)
