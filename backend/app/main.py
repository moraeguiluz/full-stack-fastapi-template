# backend/app/main.py
from fastapi import FastAPI, APIRouter
from fastapi.responses import RedirectResponse
import os, traceback
from sqlalchemy import create_engine, text

# --- FastAPI app ---
app = FastAPI(
    title="API Bonube",
    openapi_url="/api/v1/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
)

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")

@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}

# --- Tu router de diagnóstico DB ---
router = APIRouter(prefix="/db", tags=["debug-db"])

@router.get("/ping")
def db_ping():
    url = os.getenv("DATABASE_URL", "")
    if not url:
        return {"ok": False, "why": "DATABASE_URL missing"}
    info = {"driver": url.split("://", 1)[0]}
    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            row = conn.execute(text("select 1")).scalar()
        return {"ok": True, "select1": row, **info}
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "trace": traceback.format_exc().splitlines()[-1],
            **info,
        }

# Monta el router bajo /api/v1
app.include_router(router, prefix="/api/v1")
