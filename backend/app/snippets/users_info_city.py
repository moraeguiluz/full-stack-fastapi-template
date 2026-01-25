# backend/app/snippets/users_info_city.py
from __future__ import annotations

import os
import json
import datetime as dt
from typing import Optional, List

import jwt
import requests
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session

ENABLED = True
base_router = APIRouter(tags=["users-info"])
router = APIRouter()
router.include_router(base_router, prefix="/users-info")
router.include_router(base_router, prefix="/users-city")

_DB_URL = os.getenv("DATABASE_URL")
_SECRET = os.getenv("SECRET_KEY", "dev-change-me")
_ALG = "HS256"

_engine = None
_SessionLocal = None
_inited = False

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/finalize")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _init_db():
    global _engine, _SessionLocal, _inited
    if _inited:
        return
    if not _DB_URL:
        raise HTTPException(status_code=503, detail="DB no configurada (falta DATABASE_URL)")
    url = _DB_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)

    _engine = create_engine(url, pool_pre_ping=True)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)

    # PostGIS + tabla
    with _engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS app_users_info_city (
                    id BIGSERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    source VARCHAR(10) NOT NULL,
                    geom geometry(Point, 4326) NOT NULL,
                    accuracy_m INTEGER NULL,
                    active_seconds INTEGER DEFAULT 0,
                    recorded_at TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT now()
                );
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS app_geo_cache (
                    id BIGSERIAL PRIMARY KEY,
                    lat_round DOUBLE PRECISION NOT NULL,
                    lng_round DOUBLE PRECISION NOT NULL,
                    city VARCHAR(120),
                    state VARCHAR(120),
                    country VARCHAR(120),
                    updated_at TIMESTAMPTZ DEFAULT now()
                );
                """
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_users_info_city_user ON app_users_info_city(user_id)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_users_info_city_time ON app_users_info_city(user_id, recorded_at DESC)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_users_info_city_geom ON app_users_info_city USING GIST(geom)")
        )
        conn.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS ix_geo_cache_unique ON app_geo_cache(lat_round, lng_round)")
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS app_users_info_usage (
                    user_id INTEGER PRIMARY KEY,
                    tab_usage_json JSONB DEFAULT '{}'::jsonb,
                    total_seconds INTEGER DEFAULT 0,
                    updated_at TIMESTAMPTZ DEFAULT now()
                );
                """
            )
        )

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


def _current_user_id(token: str = Depends(oauth2)) -> int:
    try:
        data = jwt.decode(token, _SECRET, algorithms=[_ALG])
    except Exception:
        raise HTTPException(401, "Token inválido")
    uid = data.get("sub")
    if not uid:
        raise HTTPException(401, "Token inválido")
    return int(uid)


def _extract_client_ip(request: Request) -> Optional[str]:
    # Respeta X-Forwarded-For si existe (primer IP)
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def _round_coord(value: float, precision: int = 2) -> float:
    fmt = f"{{:.{precision}f}}"
    return float(fmt.format(value))


def _reverse_geocode(lat: float, lng: float, db: Session) -> dict:
    lat_r = _round_coord(lat, 2)
    lng_r = _round_coord(lng, 2)

    cached = db.execute(
        text(
            """
            SELECT city, state, country
            FROM app_geo_cache
            WHERE lat_round = :lat AND lng_round = :lng
            """
        ),
        {"lat": lat_r, "lng": lng_r},
    ).fetchone()

    if cached:
        return {"city": cached[0], "state": cached[1], "country": cached[2]}

    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={
                "format": "jsonv2",
                "lat": lat,
                "lon": lng,
                "zoom": 10,
                "addressdetails": 1,
            },
            headers={"User-Agent": "PlanGuerrero/1.0"},
            timeout=5,
        )
        data = resp.json() if resp.ok else {}
        addr = data.get("address") or {}
        city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality")
        state = addr.get("state")
        country = addr.get("country")
    except Exception:
        city = state = country = None

    db.execute(
        text(
            """
            INSERT INTO app_geo_cache (lat_round, lng_round, city, state, country)
            VALUES (:lat, :lng, :city, :state, :country)
            ON CONFLICT (lat_round, lng_round)
            DO UPDATE SET city = :city, state = :state, country = :country, updated_at = now()
            """
        ),
        {"lat": lat_r, "lng": lng_r, "city": city, "state": state, "country": country},
    )
    db.commit()
    return {"city": city, "state": state, "country": country}


class CityPingIn(BaseModel):
    lat: Optional[float] = None
    lng: Optional[float] = None
    source: str = Field(default="gps", max_length=10)  # gps|ip
    accuracy_m: Optional[int] = None
    active_seconds: Optional[int] = Field(default=0, ge=0, le=86400)
    recorded_at: Optional[dt.datetime] = None


class CityClusterOut(BaseModel):
    lat: float
    lng: float
    samples: int
    users: Optional[int] = None
    seconds: int
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None


class UsagePingIn(BaseModel):
    tabs_delta: dict = Field(default_factory=dict)
    recorded_at: Optional[dt.datetime] = None


class UsageSummaryOut(BaseModel):
    tabs: dict
    total_seconds: int


class UsagePercentOut(BaseModel):
    tab: str
    percent: float
    seconds: int


@base_router.post("/city/ping")
def city_ping(
    body: CityPingIn,
    request: Request,
    token: str = Depends(oauth2),
    db: Session = Depends(get_db),
):
    uid = _current_user_id(token)

    lat = body.lat
    lng = body.lng
    source = (body.source or "gps").lower()
    recorded_at = body.recorded_at or _now()

    if lat is None or lng is None:
        # Sin lat/lng explícitos no podemos guardar en PostGIS
        # (geoip no está integrado en este snippet).
        ip = _extract_client_ip(request)
        raise HTTPException(
            400,
            f"lat/lng requeridos (source={source}). ip={ip or 'n/a'}",
        )

    # Evitar spam: si el último ping fue hace <45min, ignorar.
    last = db.execute(
        text(
            """
            SELECT recorded_at
            FROM app_users_info_city
            WHERE user_id = :uid
            ORDER BY recorded_at DESC
            LIMIT 1
            """
        ),
        {"uid": uid},
    ).fetchone()

    if last and isinstance(last[0], dt.datetime):
        diff = recorded_at - last[0]
        if diff.total_seconds() < 45 * 60:
            return {"ok": True, "skipped": True}

    db.execute(
        text(
            """
            INSERT INTO app_users_info_city
            (user_id, source, geom, accuracy_m, active_seconds, recorded_at)
            VALUES (:uid, :source, ST_SetSRID(ST_MakePoint(:lng, :lat), 4326), :accuracy, :seconds, :recorded_at)
            """
        ),
        {
            "uid": uid,
            "source": source,
            "lng": lng,
            "lat": lat,
            "accuracy": body.accuracy_m,
            "seconds": int(body.active_seconds or 0),
            "recorded_at": recorded_at,
        },
    )
    db.commit()
    return {"ok": True}


@base_router.post("/usage/ping")
def usage_ping(
    body: UsagePingIn,
    token: str = Depends(oauth2),
    db: Session = Depends(get_db),
):
    uid = _current_user_id(token)
    tabs = body.tabs_delta or {}
    if not isinstance(tabs, dict):
        raise HTTPException(400, "tabs_delta inválido")

    clean = {}
    total_delta = 0
    for k, v in tabs.items():
        if not isinstance(k, str):
            continue
        try:
            seconds = int(v)
        except Exception:
            continue
        if seconds <= 0:
            continue
        clean[k] = clean.get(k, 0) + seconds
        total_delta += seconds

    if total_delta <= 0:
        return {"ok": True, "skipped": True}

    row = db.execute(
        text(
            """
            SELECT tab_usage_json, total_seconds
            FROM app_users_info_usage
            WHERE user_id = :uid
            """
        ),
        {"uid": uid},
    ).fetchone()

    current = {}
    current_total = 0
    if row:
        current = row[0] or {}
        current_total = int(row[1] or 0)
    if not isinstance(current, dict):
        current = {}

    for k, v in clean.items():
        current[k] = int(current.get(k, 0)) + int(v)
    current_total += total_delta

    db.execute(
        text(
            """
            INSERT INTO app_users_info_usage (user_id, tab_usage_json, total_seconds, updated_at)
            VALUES (:uid, :tabs::jsonb, :total, now())
            ON CONFLICT (user_id) DO UPDATE SET
                tab_usage_json = :tabs::jsonb,
                total_seconds = :total,
                updated_at = now()
            """
        ),
        {"uid": uid, "tabs": json.dumps(current), "total": current_total},
    )
    db.commit()
    return {"ok": True}


@base_router.get("/cities", response_model=List[CityClusterOut])
def get_top_cities(
    limit: int = Query(default=3, ge=1, le=10),
    days: int = Query(default=60, ge=1, le=365),
    eps_km: float = Query(default=5.0, ge=0.5, le=50.0),
    token: str = Depends(oauth2),
    db: Session = Depends(get_db),
):
    uid = _current_user_id(token)
    since = _now() - dt.timedelta(days=days)
    eps_deg = eps_km / 111.0  # aprox (km -> grados)

    rows = db.execute(
        text(
            """
            WITH pts AS (
                SELECT geom, active_seconds
                FROM app_users_info_city
                WHERE user_id = :uid AND recorded_at >= :since
            ),
            clusters AS (
                SELECT
                    ST_ClusterDBSCAN(geom, eps := :eps, minpoints := 1) OVER () AS cid,
                    geom,
                    active_seconds
                FROM pts
            ),
            agg AS (
                SELECT
                    cid,
                    ST_Centroid(ST_Collect(geom)) AS center,
                    COUNT(*) AS samples,
                    COALESCE(SUM(active_seconds), 0) AS seconds
                FROM clusters
                GROUP BY cid
            )
            SELECT
                ST_Y(center) AS lat,
                ST_X(center) AS lng,
                samples,
                seconds
            FROM agg
            ORDER BY seconds DESC, samples DESC
            LIMIT :limit
            """
        ),
        {"uid": uid, "since": since, "eps": eps_deg, "limit": limit},
    ).fetchall()

    out: List[CityClusterOut] = []
    for r in rows:
        lat = float(r[0])
        lng = float(r[1])
        info = _reverse_geocode(lat, lng, db)
        out.append(
            CityClusterOut(
                lat=lat,
                lng=lng,
                samples=int(r[2]),
                seconds=int(r[3]),
                city=info.get("city"),
                state=info.get("state"),
                country=info.get("country"),
            )
        )
    return out


@base_router.get("/cities/admin", response_model=List[CityClusterOut])
def admin_city_summary(
    limit: int = Query(default=20, ge=1, le=100),
    days: int = Query(default=60, ge=1, le=365),
    eps_km: float = Query(default=5.0, ge=0.5, le=50.0),
    token: str = Depends(oauth2),
    db: Session = Depends(get_db),
):
    _ = _current_user_id(token)
    since = _now() - dt.timedelta(days=days)
    eps_deg = eps_km / 111.0

    rows = db.execute(
        text(
            """
            WITH pts AS (
                SELECT user_id, geom, active_seconds
                FROM app_users_info_city
                WHERE recorded_at >= :since
            ),
            clusters AS (
                SELECT
                    ST_ClusterDBSCAN(geom, eps := :eps, minpoints := 1) OVER () AS cid,
                    user_id,
                    geom,
                    active_seconds
                FROM pts
            ),
            agg AS (
                SELECT
                    cid,
                    ST_Centroid(ST_Collect(geom)) AS center,
                    COUNT(*) AS samples,
                    COUNT(DISTINCT user_id) AS users,
                    COALESCE(SUM(active_seconds), 0) AS seconds
                FROM clusters
                GROUP BY cid
            )
            SELECT
                ST_Y(center) AS lat,
                ST_X(center) AS lng,
                samples,
                users,
                seconds
            FROM agg
            ORDER BY users DESC, seconds DESC
            LIMIT :limit
            """
        ),
        {"since": since, "eps": eps_deg, "limit": limit},
    ).fetchall()

    out: List[CityClusterOut] = []
    for r in rows:
        lat = float(r[0])
        lng = float(r[1])
        info = _reverse_geocode(lat, lng, db)
        out.append(
            CityClusterOut(
                lat=lat,
                lng=lng,
                samples=int(r[2]),
                users=int(r[3]),
                seconds=int(r[4]),
                city=info.get("city"),
                state=info.get("state"),
                country=info.get("country"),
            )
        )
    return out


class UserSearchOut(BaseModel):
    id: int
    nombre: Optional[str] = None
    telefono: Optional[str] = None


class UserCityOut(BaseModel):
    user_id: int
    lat: float
    lng: float
    source: str
    recorded_at: dt.datetime
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None


@base_router.get("/users/search", response_model=List[UserSearchOut])
def admin_user_search(
    query: str = Query(min_length=2, max_length=120),
    limit: int = Query(default=20, ge=1, le=100),
    token: str = Depends(oauth2),
    db: Session = Depends(get_db),
):
    _ = _current_user_id(token)
    q = f"%{query.strip()}%"

    rows = db.execute(
        text(
            """
            SELECT id, nombre, telefono
            FROM app_user_auth
            WHERE
                (nombre ILIKE :q)
                OR (apellido_paterno ILIKE :q)
                OR (apellido_materno ILIKE :q)
                OR (telefono ILIKE :q)
            ORDER BY id DESC
            LIMIT :limit
            """
        ),
        {"q": q, "limit": limit},
    ).fetchall()

    return [UserSearchOut(id=int(r[0]), nombre=r[1], telefono=r[2]) for r in rows]


@base_router.get("/users/{user_id:int}/city", response_model=Optional[UserCityOut])
def admin_user_city(
    user_id: int,
    token: str = Depends(oauth2),
    db: Session = Depends(get_db),
):
    _ = _current_user_id(token)
    row = db.execute(
        text(
            """
            SELECT
                ST_Y(geom) AS lat,
                ST_X(geom) AS lng,
                source,
                recorded_at
            FROM app_users_info_city
            WHERE user_id = :uid
            ORDER BY recorded_at DESC
            LIMIT 1
            """
        ),
        {"uid": user_id},
    ).fetchone()

    if not row:
        return None

    lat = float(row[0])
    lng = float(row[1])
    info = _reverse_geocode(lat, lng, db)

    return UserCityOut(
        user_id=user_id,
        lat=lat,
        lng=lng,
        source=row[2] or "gps",
        recorded_at=row[3],
        city=info.get("city"),
        state=info.get("state"),
        country=info.get("country"),
    )


@base_router.get("/usage/admin/{user_id:int}", response_model=UsageSummaryOut)
def admin_usage_by_user(
    user_id: int,
    token: str = Depends(oauth2),
    db: Session = Depends(get_db),
):
    _ = _current_user_id(token)
    row = db.execute(
        text(
            """
            SELECT tab_usage_json, total_seconds
            FROM app_users_info_usage
            WHERE user_id = :uid
            """
        ),
        {"uid": user_id},
    ).fetchone()

    if not row:
        return UsageSummaryOut(tabs={}, total_seconds=0)

    return UsageSummaryOut(tabs=row[0] or {}, total_seconds=int(row[1] or 0))


@base_router.get("/usage/admin/summary", response_model=List[UsagePercentOut])
def admin_usage_summary(
    token: str = Depends(oauth2),
    db: Session = Depends(get_db),
):
    _ = _current_user_id(token)

    rows = db.execute(
        text(
            """
            SELECT key, SUM((value)::int) AS seconds
            FROM app_users_info_usage, jsonb_each_text(tab_usage_json)
            GROUP BY key
            ORDER BY seconds DESC
            """
        )
    ).fetchall()

    total = sum(int(r[1] or 0) for r in rows)
    if total <= 0:
        return []

    return [
        UsagePercentOut(tab=str(r[0]), seconds=int(r[1] or 0), percent=(int(r[1] or 0) * 100.0) / total)
        for r in rows
    ]
