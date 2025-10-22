# backend/app/main.py
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
import importlib, pkgutil, logging

app = FastAPI(
    title="API Bonube",
    openapi_url="/api/v1/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
)

@app.get("/", include_in_schema=False)
def root():
    # O devuelve {"ok": True, "hint": "Visita /docs"}
    return RedirectResponse(url="/docs")

@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}

# -------- Autocarga de "snippets" (app/snippets/*.py) --------
from . import snippets as _snippets_pkg  # carpeta backend/app/snippets

_loaded = []
for m in pkgutil.iter_modules(_snippets_pkg.__path__):
    mod = importlib.import_module(f"{_snippets_pkg.__name__}.{m.name}")
    # Solo módulos que expongan "router" (APIRouter)
    if getattr(mod, "ENABLED", True) and hasattr(mod, "router"):
        app.include_router(mod.router, prefix="/api/v1")
        _loaded.append(m.name)

logging.getLogger("uvicorn").info(f"Snippets cargados: {', '.join(_loaded) or '(ninguno)'}")
# --------------------------------------------------------------
