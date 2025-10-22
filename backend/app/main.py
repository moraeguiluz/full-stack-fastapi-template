# backend/app/main.py
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
import logging
import importlib
import pkgutil

app = FastAPI(
    title="API Bonube",
    openapi_url="/api/v1/openapi.json",  # útil si montas tus rutas bajo /api/v1
    docs_url="/docs",
    redoc_url="/redoc",
)

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")

@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}

# -------------------------------------------------------------------
# Autoload de snippets: carga todo módulo en app/snippets/*.py que
# exponga `router = APIRouter(...)` y lo monta bajo /api/v1
# -------------------------------------------------------------------
_loaded, _failed = [], []

try:
    from . import snippets as _snippets_pkg  # requiere backend/app/snippets/__init__.py
    for m in pkgutil.iter_modules(_snippets_pkg.__path__):
        modname = f"{_snippets_pkg.__name__}.{m.name}"
        try:
            mod = importlib.import_module(modname)
            if getattr(mod, "ENABLED", True) and hasattr(mod, "router"):
                app.include_router(mod.router, prefix="/api/v1")
                _loaded.append(m.name)
            else:
                _failed.append((m.name, "sin 'router' o deshabilitado"))
        except Exception as e:
            _failed.append((m.name, f"import error: {e}"))
except Exception as e:
    _failed.append(("__snippets__", f"package error: {e}"))

log = logging.getLogger("uvicorn")
if _loaded:
    log.info(f"Snippets cargados: {', '.join(_loaded)}")
if _failed:
    for name, reason in _failed:
        log.warning(f"Snippet omitido: {name} → {reason}")
# -------------------------------------------------------------------
