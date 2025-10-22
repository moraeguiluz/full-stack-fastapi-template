from fastapi import APIRouter
import os, traceback
from sqlalchemy import create_engine, text

router = APIRouter(prefix="/db", tags=["debug-db"])

@router.get("/ping")
def db_ping():
    url = os.getenv("DATABASE_URL", "")
    if not url:
        return {"ok": False, "why": "DATABASE_URL missing"}
    info = {"driver": url.split("://", 1)[0]}
    try:
        # Intento de conexión
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            row = conn.execute(text("select 1")).scalar()
        return {"ok": True, "select1": row, **info}
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "trace": traceback.format_exc().splitlines()[-1],
            **info
        }
