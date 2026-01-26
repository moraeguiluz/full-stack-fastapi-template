# backend/app/main.py
from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse
import logging
import importlib
import pkgutil
import os
import datetime as dt
from sqlalchemy import create_engine, DateTime, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.core.config import settings

app = FastAPI(
    title="API Bonube",
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)

_LEGAL_APP_NAME = os.getenv("LEGAL_APP_NAME", "MEXOR")
_LEGAL_CONTACT_EMAIL = os.getenv("LEGAL_CONTACT_EMAIL", "info@bonube.com")
_LEGAL_DB_URL = os.getenv("DATABASE_URL") or str(settings.SQLALCHEMY_DATABASE_URI)

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
      <p>Completa el formulario para solicitar la eliminacion de tus datos.</p>
      <form method="post" action="/data-deletion">
        <label>
          Nombre completo
          <input name="name" type="text" required />
        </label>
        <br />
        <label>
          Numero de telefono
          <input name="phone" type="tel" required />
        </label>
        <br />
        <label>
          Correo asociado a la cuenta
          <input name="email" type="email" required />
        </label>
        <br />
        <button type="submit">Solicitar eliminacion</button>
      </form>
    </main>
  </body>
</html>
"""

_DATA_DELETION_SUCCESS_HTML = """<!doctype html>
<html lang="es">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Solicitud recibida</title>
  </head>
  <body>
    <main>
      <h1>Solicitud recibida</h1>
      <p>Gracias. Tu solicitud fue registrada y sera revisada por nuestro equipo.</p>
    </main>
  </body>
</html>
"""

_legal_engine = None
_legal_SessionLocal = None
_legal_inited = False


class LegalBase(DeclarativeBase):
    pass


class DataDeletionRequest(LegalBase):
    __tablename__ = "app_data_deletion_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(50))
    email: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


def _init_legal_db() -> None:
    global _legal_engine, _legal_SessionLocal, _legal_inited
    if _legal_inited:
        return
    if not _LEGAL_DB_URL:
        raise HTTPException(status_code=503, detail="DB no configurada")
    url = _LEGAL_DB_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    _legal_engine = create_engine(url, pool_pre_ping=True)
    _legal_SessionLocal = sessionmaker(bind=_legal_engine, autoflush=False, autocommit=False)
    LegalBase.metadata.create_all(bind=_legal_engine)
    _legal_inited = True


def get_legal_db():
    _init_legal_db()
    if not _legal_SessionLocal:
        raise HTTPException(status_code=503, detail="DB no configurada")
    db: Session = _legal_SessionLocal()
    try:
        yield db
    finally:
        db.close()
@app.get("/", include_in_schema=False)
def root():
    return {"ok": True}

@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}

@app.get("/privacy-policy", response_class=HTMLResponse, include_in_schema=False)
def aviso_privacidad():
    return HTMLResponse(content=_PRIVACY_HTML)

@app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def privacy_shortcut():
    return HTMLResponse(content=_PRIVACY_HTML)

@app.get("/data-deletion", response_class=HTMLResponse, include_in_schema=False)
def eliminacion_datos():
    return HTMLResponse(content=_DATA_DELETION_HTML)

@app.post("/data-deletion", response_class=HTMLResponse, include_in_schema=False)
def submit_data_deletion(
    name: str = Form(..., min_length=2, max_length=200),
    phone: str = Form(..., min_length=5, max_length=50),
    email: str = Form(..., min_length=5, max_length=320),
    db: Session = Depends(get_legal_db),
):
    req = DataDeletionRequest(
        name=name.strip(),
        phone=phone.strip(),
        email=email.strip(),
    )
    db.add(req)
    db.commit()
    return HTMLResponse(content=_DATA_DELETION_SUCCESS_HTML, status_code=201)

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
