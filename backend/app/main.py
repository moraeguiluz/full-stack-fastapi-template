# backend/app/main.py
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
import importlib, pkgutil

app = FastAPI(
    title="API Bonube",
    openapi_url="/api/v1/openapi.json",  # Swagger para rutas bajo /api/v1
    docs_url="/docs",
    redoc_url="/redoc",
)

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")  # o devuelve {"ok": True}

@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}

# --- Autoload de "snippets" (app/snippets/*.py que expongan 'router') ---
from . import snippets as _snippets_pkg  # carpeta backend/app/snippets

for m in pkgutil.iter_modules(_snippets_pkg.__path__):
    mod = importlib.import_module(f"{_snippets_pkg.__name__}.{m.name}")
    if hasattr(mod, "router"):  # opcional: getattr(mod, "ENABLED", True) and ...
        app.include_router(mod.router, prefix="/api/v1")
