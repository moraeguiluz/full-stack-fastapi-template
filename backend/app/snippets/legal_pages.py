import datetime as dt
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Integer, String, create_engine, func
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

ENABLED = True
ROUTER_PREFIX = ""
router = APIRouter(include_in_schema=False)

_LEGAL_APP_NAME = os.getenv("LEGAL_APP_NAME", "MEXOR")
_LEGAL_CONTACT_EMAIL = os.getenv("LEGAL_CONTACT_EMAIL", "info@bonube.com")
_LEGAL_DB_URL = os.getenv("DATABASE_URL")

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
      <form id="data-deletion-form">
        <label>
          Nombre completo
          <input id="name" name="name" type="text" required />
        </label>
        <br />
        <label>
          Numero de telefono
          <input id="phone" name="phone" type="tel" required />
        </label>
        <br />
        <label>
          Correo asociado a la cuenta
          <input id="email" name="email" type="email" required />
        </label>
        <br />
        <button type="submit">Solicitar eliminacion</button>
      </form>
      <p id="status" role="status" aria-live="polite"></p>
    </main>
    <script>
      (function () {{
        const form = document.getElementById("data-deletion-form");
        const status = document.getElementById("status");
        form.addEventListener("submit", async function (ev) {{
          ev.preventDefault();
          status.textContent = "Enviando solicitud...";
          const payload = {{
            name: document.getElementById("name").value,
            phone: document.getElementById("phone").value,
            email: document.getElementById("email").value,
          }};
          try {{
            const res = await fetch("/data-deletion", {{
              method: "POST",
              headers: {{"Content-Type": "application/json"}},
              body: JSON.stringify(payload),
            }});
            if (res.ok) {{
              const html = await res.text();
              document.open();
              document.write(html);
              document.close();
              return;
            }}
            const msg = await res.text();
            status.textContent = "Error: " + msg;
          }} catch (err) {{
            status.textContent = "Error al enviar la solicitud.";
          }}
        }});
      }})();
    </script>
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


class DataDeletionIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    phone: str = Field(min_length=5, max_length=50)
    email: str = Field(min_length=5, max_length=320)


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


@router.get("/privacy-policy", response_class=HTMLResponse)
def aviso_privacidad():
    return HTMLResponse(content=_PRIVACY_HTML)


@router.get("/privacy", response_class=HTMLResponse)
def privacy_shortcut():
    return HTMLResponse(content=_PRIVACY_HTML)


@router.get("/data-deletion", response_class=HTMLResponse)
def eliminacion_datos():
    return HTMLResponse(content=_DATA_DELETION_HTML)


@router.post("/data-deletion", response_class=HTMLResponse)
def submit_data_deletion(
    payload: DataDeletionIn,
    db: Session = Depends(get_legal_db),
):
    req = DataDeletionRequest(
        name=payload.name.strip(),
        phone=payload.phone.strip(),
        email=payload.email.strip(),
    )
    db.add(req)
    db.commit()
    return HTMLResponse(content=_DATA_DELETION_SUCCESS_HTML, status_code=201)
