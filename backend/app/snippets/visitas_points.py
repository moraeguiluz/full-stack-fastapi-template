# backend/app/snippets/visitas.py ya crea la tabla.
# Este snippet solo LEE puntos y devuelve GeoJSON para el mapa.

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from typing import Literal
import os, datetime as dt, jwt

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session

router = APIRouter(prefix="/visitas", tags=["visitas"])

# -------------------- Config & lazy init --------------------
_DB_URL = os.getenv("DATABASE_URL")
_SECRET = os.getenv("SECRET_KEY", "dev-change-me")
_ALG = "HS256"

_engine = None
_SessionLocal = None
_inited = False

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/finalize")

def _init_db():
    """Inicializa conexión al primer uso; no crea tablas nuevas aquí."""
    global _engine, _SessionLocal, _inited
    if _inited or not _DB_URL:
        return
    url = _DB_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    _engine = create_engine(url, pool_pre_ping=True)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
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

def _decode(token: str) -> dict:
    return jwt.decode(token, _SECRET, algorithms=[_ALG])

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

def _ensure_tz(ts: Optional[dt.datetime]) -> Optional[dt.datetime]:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)

# -------------------- Schemas (GeoJSON) --------------------
class GeoPoint(BaseModel):
    type: str = "Point"
    coordinates: List[float]  # [lng, lat]

class Feature(BaseModel):
    type: str = "Feature"
    geometry: GeoPoint
    properties: Dict[str, Any] = Field(default_factory=dict)

class FeatureCollection(BaseModel):
    type: str = "FeatureCollection"
    features: List[Feature]

# -------------------- GET /api/v1/visitas/points --------------------
@router.get("/points", response_model=FeatureCollection)
def listar_puntos(
    db: Session = Depends(get_db),
    uid: int = Depends(_current_user_id),
    limit: int = Query(10000, ge=1, le=50000, description="Máximo de puntos a devolver"),
    from_dt: Optional[dt.datetime] = Query(None, alias="from"),
    to_dt: Optional[dt.datetime] = Query(None, alias="to"),
):
    """
    Devuelve TODAS las visitas del usuario autenticado con lat/lng en GeoJSON.
    properties incluye: id, user_id, nombre, apellidos, telefono, hora (ISO 8601 UTC), created_at.
    """
    from_dt = _ensure_tz(from_dt)
    to_dt = _ensure_tz(to_dt)

    sql = """
    SELECT id, user_id, nombre, apellido_paterno, apellido_materno, telefono,
           lat, lng, hora, created_at
    FROM app_visita
    WHERE user_id = :uid
      AND lat IS NOT NULL AND lng IS NOT NULL
      {and_from}
      {and_to}
    ORDER BY hora DESC
    LIMIT :limit
    """.format(
        and_from="AND hora >= :from_dt" if from_dt is not None else "",
        and_to="AND hora < :to_dt" if to_dt is not None else "",
    )

    params = {"uid": uid, "limit": limit}
    if from_dt is not None:
        params["from_dt"] = from_dt
    if to_dt is not None:
        params["to_dt"] = to_dt

    rows = db.execute(text(sql), params).mappings().all()

    features: List[Feature] = []
    for r in rows:
        lat = r["lat"]
        lng = r["lng"]
        if lat is None or lng is None:
            continue

        props = {
            "id": r["id"],
            "user_id": r["user_id"],
            "nombre": r.get("nombre") or "",
            "apellido_paterno": r.get("apellido_paterno") or "",
            "apellido_materno": r.get("apellido_materno") or "",
            "telefono": r.get("telefono"),
            "hora": r["hora"].astimezone(dt.timezone.utc).isoformat()
                    if isinstance(r["hora"], dt.datetime) else str(r["hora"]),
            "created_at": r["created_at"].astimezone(dt.timezone.utc).isoformat()
                          if isinstance(r["created_at"], dt.datetime) else str(r["created_at"]),
        }

        features.append(
            Feature(
                geometry=GeoPoint(coordinates=[float(lng), float(lat)]),
                properties=props,
            )
        )

    return FeatureCollection(
        type="text/plain",  # pydantic ignores this; response_model enforces "FeatureCollection"
        features=features
    )
