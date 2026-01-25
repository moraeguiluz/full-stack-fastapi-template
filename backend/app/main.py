# backend/app/main.py
from fastapi import FastAPI
import logging
import importlib
import pkgutil

app = FastAPI(
    title="API Bonube",
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)

@app.get("/", include_in_schema=False)
def root():
    return {"ok": True}

@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}

# -------------------------------------------------------------------
# Autoload de snippets: carga módulos en app/snippets y subcarpetas que
# expongan `router = APIRouter(...)` y los monta bajo /api/v1
# -------------------------------------------------------------------
_loaded, _failed = [], []

try:
    from . import snippets as _snippets_pkg  # requiere backend/app/snippets/__init__.py
    prefix = f"{_snippets_pkg.__name__}."
    for m in pkgutil.walk_packages(_snippets_pkg.__path__, prefix=prefix):
        modname = m.name
        relname = modname[len(prefix):]
        if "." in relname:
            leaf = relname.split(".")[-1]
            if leaf not in ("router", "routes"):
                continue
        elif m.ispkg:
            continue
        try:
            mod = importlib.import_module(modname)
            if getattr(mod, "ENABLED", True) and hasattr(mod, "router"):
                app.include_router(mod.router, prefix="/api/v1")
                _loaded.append(relname)
            else:
                _failed.append((relname, "sin 'router' o deshabilitado"))
        except Exception as e:
            _failed.append((relname, f"import error: {e}"))
except Exception as e:
    _failed.append(("__snippets__", f"package error: {e}"))

log = logging.getLogger("uvicorn")
if _loaded:
    log.info(f"Snippets cargados: {', '.join(_loaded)}")
if _failed:
    for name, reason in _failed:
        log.warning(f"Snippet omitido: {name} → {reason}")
# -------------------------------------------------------------------
