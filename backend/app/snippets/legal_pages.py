import datetime as dt
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Integer, String, create_engine, func
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

ENABLED = True
ROUTER_PREFIX = ""
router = APIRouter(include_in_schema=False)

_LEGAL_APP_NAME = os.getenv("LEGAL_APP_NAME", "MEXOR")
_LEGAL_CONTACT_EMAIL = os.getenv("LEGAL_CONTACT_EMAIL", "info@mexor.app")
_LEGAL_DB_URL = os.getenv("DATABASE_URL")

_PRIVACY_HTML = f"""<!doctype html>
<html lang="es">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Aviso de privacidad - {_LEGAL_APP_NAME}</title>
    <style>
      :root {{
        --bg: #f4f6f8;
        --card: #ffffff;
        --text: #1e2a32;
        --muted: #5a6b75;
        --line: #e6ebef;
        --accent: #0f4c81;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: "Georgia", "Times New Roman", serif;
        background: var(--bg);
        color: var(--text);
      }}
      .page {{
        min-height: 100vh;
        display: grid;
        place-items: center;
        padding: 32px 16px;
      }}
      .card {{
        width: min(860px, 92vw);
        background: var(--card);
        border: 1px solid var(--line);
        box-shadow: 0 12px 32px rgba(15, 30, 45, 0.08);
        padding: 40px 42px;
      }}
      h1 {{
        margin: 0 0 16px;
        font-size: 28px;
        letter-spacing: 0.2px;
      }}
      .lead {{
        color: var(--muted);
        margin: 0 0 20px;
        line-height: 1.6;
      }}
      .section {{
        margin-top: 18px;
        line-height: 1.65;
      }}
      .section h2 {{
        margin: 18px 0 8px;
        font-size: 18px;
        color: var(--accent);
      }}
      .footer {{
        margin-top: 22px;
        padding-top: 16px;
        border-top: 1px solid var(--line);
        color: var(--muted);
      }}
      a {{ color: var(--accent); }}
    </style>
  </head>
  <body>
    <main class="page">
      <section class="card">
        <h1>Aviso de privacidad</h1>
        <p class="lead">
          En {_LEGAL_APP_NAME} tratamos tus datos personales de manera responsable,
          transparente y segura. Este aviso describe, de forma clara, cómo
          recopilamos, usamos y protegemos la información vinculada a nuestros
          servicios.
        </p>
        <div class="section">
          <h2>Uso responsable de la información</h2>
          <p>
            Solo utilizamos los datos estrictamente necesarios para operar la
            aplicación y mejorar la experiencia de nuestros usuarios. Aplicamos
            prácticas de seguridad y controles internos para proteger la
            información contra accesos no autorizados o uso indebido.
          </p>
        </div>
        <div class="section">
          <h2>Confidencialidad y resguardo</h2>
          <p>
            Toda la información se maneja con confidencialidad y bajo estándares
            razonables de protección. Nuestro equipo sigue lineamientos que
            privilegian la integridad y el cuidado de los datos.
          </p>
        </div>
        <div class="section">
          <h2>Contacto</h2>
          <p>
            Si tienes dudas o solicitudes relacionadas con privacidad, por favor
            escríbenos a
            <a href="mailto:{_LEGAL_CONTACT_EMAIL}">{_LEGAL_CONTACT_EMAIL}</a>.
          </p>
        </div>
        <div class="footer">
          Última actualización: Enero 2026.
        </div>
      </section>
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
    <style>
      :root {{
        --bg: #f4f6f8;
        --card: #ffffff;
        --text: #1e2a32;
        --muted: #5a6b75;
        --line: #e6ebef;
        --accent: #0f4c81;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: "Georgia", "Times New Roman", serif;
        background: var(--bg);
        color: var(--text);
      }}
      .page {{
        min-height: 100vh;
        display: grid;
        place-items: center;
        padding: 32px 16px;
      }}
      .card {{
        width: min(640px, 92vw);
        background: var(--card);
        border: 1px solid var(--line);
        box-shadow: 0 12px 32px rgba(15, 30, 45, 0.08);
        padding: 36px 38px;
      }}
      h1 {{
        margin: 0 0 12px;
        font-size: 24px;
        letter-spacing: 0.2px;
      }}
      p {{
        color: var(--muted);
        line-height: 1.6;
      }}
      label {{
        display: block;
        margin-top: 14px;
        font-weight: 600;
      }}
      input {{
        width: 100%;
        margin-top: 6px;
        padding: 10px 12px;
        border: 1px solid var(--line);
        border-radius: 6px;
        font-size: 15px;
      }}
      button {{
        margin-top: 18px;
        background: var(--accent);
        color: #fff;
        border: none;
        padding: 10px 18px;
        border-radius: 6px;
        font-size: 15px;
        cursor: pointer;
      }}
      #status {{
        margin-top: 16px;
        color: var(--muted);
      }}
      .brand {{
        font-weight: 700;
        color: var(--accent);
      }}
    </style>
  </head>
  <body>
    <main class="page">
      <section class="card">
        <h1>Solicitud de eliminación de datos</h1>
        <p>
          En <span class="brand">MEXOR</span> atendemos las solicitudes de eliminación
          con seriedad y respeto. Por favor comparte la información solicitada.
        </p>
        <form id="data-deletion-form">
          <label>
            Nombre completo
            <input id="name" name="name" type="text" required />
          </label>
          <label>
            Número de teléfono
            <input id="phone" name="phone" type="tel" required />
          </label>
          <button type="submit">Solicitar eliminación</button>
        </form>
        <p id="status" role="status" aria-live="polite"></p>
      </section>
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

_CHILD_SAFETY_HTML = f"""<!doctype html>
<html lang="es">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Estándares de seguridad infantil - {_LEGAL_APP_NAME}</title>
    <style>
      :root {{
        --bg: #f4f6f8;
        --card: #ffffff;
        --text: #1e2a32;
        --muted: #5a6b75;
        --line: #e6ebef;
        --accent: #0f4c81;
        --accent-soft: rgba(15, 76, 129, 0.08);
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: "Georgia", "Times New Roman", serif;
        background: var(--bg);
        color: var(--text);
      }}
      .page {{
        min-height: 100vh;
        display: grid;
        place-items: center;
        padding: 32px 16px;
      }}
      .card {{
        width: min(940px, 92vw);
        background: var(--card);
        border: 1px solid var(--line);
        box-shadow: 0 12px 32px rgba(15, 30, 45, 0.08);
        padding: 40px 42px;
      }}
      h1 {{
        margin: 0 0 12px;
        font-size: 28px;
        letter-spacing: 0.2px;
      }}
      .lead {{
        color: var(--muted);
        margin: 0 0 20px;
        line-height: 1.6;
      }}
      .section {{
        margin-top: 18px;
        line-height: 1.7;
      }}
      .section h2 {{
        margin: 18px 0 8px;
        font-size: 18px;
        color: var(--accent);
      }}
      .pill {{
        display: inline-block;
        padding: 6px 12px;
        border-radius: 999px;
        background: var(--accent-soft);
        color: var(--accent);
        font-weight: 600;
        margin: 6px 0 2px;
      }}
      ul {{ padding-left: 18px; }}
      li {{ margin: 6px 0; }}
      a {{ color: var(--accent); }}
      .footer {{
        margin-top: 22px;
        padding-top: 16px;
        border-top: 1px solid var(--line);
        color: var(--muted);
      }}
    </style>
  </head>
  <body>
    <main class="page">
      <section class="card">
        <h1>Estándares de seguridad infantil (CSAE)</h1>
        <p class="lead">
          En {_LEGAL_APP_NAME} mantenemos una política de tolerancia cero frente a la
          explotación y el abuso sexual infantil (CSAE). Esta página describe de
          forma clara cómo prevenimos, detectamos y actuamos frente a cualquier
          contenido o conducta que ponga en riesgo a menores de edad.
        </p>
        <div class="section">
          <span class="pill">Compromiso público</span>
          <h2>Principios y conducta prohibida</h2>
          <p>
            Prohibimos cualquier contenido, actividad o intento de interacción que
            involucre explotación, abuso o sexualización de menores. Esto incluye,
            sin limitarse, material gráfico, solicitud de imágenes, grooming,
            lenguaje sexual explícito dirigido a menores o cualquier conducta que
            busque contactar o dañar a una persona menor de edad.
          </p>
        </div>
        <div class="section">
          <h2>Prevención y controles</h2>
          <p>
            Aplicamos controles de acceso, medidas de seguridad y revisión continua
            de flujos críticos dentro de la app. Cuando un usuario reporta
            contenido o actividad sospechosa, priorizamos su revisión para actuar
            de forma inmediata. También mantenemos registros de auditoría y señales
            internas para detectar patrones de abuso o fraude.
          </p>
        </div>
        <div class="section">
          <h2>CSAM (material de abuso sexual infantil)</h2>
          <p>
            El material de abuso sexual infantil (CSAM) está estrictamente prohibido.
            Cualquier indicio o reporte de CSAM se atiende con carácter urgente:
            removemos contenido, suspendemos cuentas, preservamos evidencia y
            colaboramos con las autoridades competentes.
          </p>
        </div>
        <div class="section">
          <h2>Reportes y respuesta</h2>
          <p>
            Si detectamos conductas relacionadas con CSAE, suspendemos la cuenta,
            preservamos evidencia relevante y reportamos a las autoridades
            competentes cuando aplica. Nuestro objetivo es proteger a la comunidad
            y colaborar con las investigaciones oficiales.
          </p>
          <ul>
            <li>Acción inmediata ante reportes verificados.</li>
            <li>Bloqueo preventivo de cuentas en casos críticos.</li>
            <li>Cooperación con autoridades y preservación de evidencia.</li>
          </ul>
        </div>
        <div class="section">
          <h2>Mecanismo de reporte dentro de la app</h2>
          <p>
            La app incluye un canal de retroalimentación y reporte accesible para los
            usuarios. Cualquier persona puede enviar alertas de seguridad o reportes
            relacionados con CSAE desde la sección de Opciones.
          </p>
        </div>
        <div class="section">
          <h2>Cumplimiento legal</h2>
          <p>
            {_LEGAL_APP_NAME} cumple con las leyes y normativas aplicables en materia
            de seguridad infantil, protección de datos y prevención de abuso. Cuando
            es requerido, cooperamos con autoridades y seguimos los procedimientos
            legales vigentes.
          </p>
        </div>
        <div class="section">
          <h2>Educación y cultura de seguridad</h2>
          <p>
            Promovemos un entorno seguro a través de guías internas, capacitación
            del equipo y reglas claras de uso. La seguridad de los menores es
            prioritaria y no negociable dentro de nuestra plataforma.
          </p>
        </div>
        <div class="section">
          <h2>Estándares externos publicados</h2>
          <p>
            Consulta nuestros estándares contra la explotación y el abuso sexual
            infantil (CSAE) publicados externamente aquí:
            <a href="/child-safety" target="_blank" rel="noopener noreferrer">
              /child-safety
            </a>
          </p>
        </div>
        <div class="section">
          <h2>Contacto</h2>
          <p>
            Para reportar contenido o solicitar información relacionada con la
            seguridad infantil, escríbenos a
            <a href="mailto:{_LEGAL_CONTACT_EMAIL}">{_LEGAL_CONTACT_EMAIL}</a>.
          </p>
        </div>
        <div class="footer">
          Última actualización: Enero 2026.
        </div>
      </section>
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


class FeedbackMessage(LegalBase):
    __tablename__ = "app_feedback_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message: Mapped[str] = mapped_column(String(4000))
    phone: Mapped[str] = mapped_column(String(50), default="")
    source: Mapped[str] = mapped_column(String(50), default="in_app")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DataDeletionIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    phone: str = Field(min_length=5, max_length=50)


class FeedbackIn(BaseModel):
    message: str = Field(min_length=10, max_length=4000)
    phone: Optional[str] = Field(default=None, max_length=50)
    source: Optional[str] = Field(default="in_app", max_length=50)


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
        email="",
    )
    db.add(req)
    db.commit()
    return HTMLResponse(content=_DATA_DELETION_SUCCESS_HTML, status_code=201)


@router.post("/feedback")
def submit_feedback(
    payload: FeedbackIn,
    db: Session = Depends(get_legal_db),
):
    msg = FeedbackMessage(
        message=payload.message.strip(),
        phone=(payload.phone or "").strip(),
        source=(payload.source or "in_app").strip(),
    )
    db.add(msg)
    db.commit()
    return {"status": "ok"}


@router.get("/child-safety", response_class=HTMLResponse)
def child_safety():
    return HTMLResponse(content=_CHILD_SAFETY_HTML)
