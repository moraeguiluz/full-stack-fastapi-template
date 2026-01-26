# backend/app/main.py
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import logging
import importlib
import pkgutil
import os

app = FastAPI(
    title="API Bonube",
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)

_LEGAL_APP_NAME = os.getenv("LEGAL_APP_NAME", "MEXOR")
_LEGAL_CONTACT_EMAIL = os.getenv("LEGAL_CONTACT_EMAIL", "info@bonube.com")

_PRIVACY_HTML = f"""<!doctype html>
<html lang="es">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Aviso de privacidad - {_LEGAL_APP_NAME}</title>
  </head>
  <body>
    <main>
      <h1>Aviso de privacidad</h1>
      <p>
        En {_LEGAL_APP_NAME} respetamos tu privacidad. Esta pagina explica de forma
        general como tratamos los datos personales que nos proporcionas al usar
        la aplicacion.
      </p>
      <p>
        Para preguntas o solicitudes relacionadas con privacidad, escribenos a
        <a href="mailto:{_LEGAL_CONTACT_EMAIL}">{_LEGAL_CONTACT_EMAIL}</a>.
      </p>
    </main>
  </body>
</html>
"""

_DATA_DELETION_HTML = f"""<!doctype html>
<html lang="es">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Solicitud de eliminacion de datos - {_LEGAL_APP_NAME}</title>
  </head>
  <body>
    <main>
      <h1>Solicitud de eliminacion de datos</h1>
      <p>
        Si deseas solicitar la eliminacion de tus datos personales, envianos un
        correo a <a href="mailto:{_LEGAL_CONTACT_EMAIL}">{_LEGAL_CONTACT_EMAIL}</a>
        con el asunto "Eliminacion de datos" e incluye el correo o identificador
        asociado a tu cuenta.
      </p>
    </main>
  </body>
</html>
"""

@app.get("/", include_in_schema=False)
def root():
    return {"ok": True}

@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}

@app.get("/privacy-policy", response_class=HTMLResponse, include_in_schema=False)
def aviso_privacidad():
    return HTMLResponse(content=_PRIVACY_HTML)

@app.get("/data-deletion", response_class=HTMLResponse, include_in_schema=False)
def eliminacion_datos():
    return HTMLResponse(content=_DATA_DELETION_HTML)

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
