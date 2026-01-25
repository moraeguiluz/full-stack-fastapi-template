import base64
import datetime as dt
import json
import os
import re
from dataclasses import dataclass
from typing import Iterable

import requests
from google.cloud import storage
from google.oauth2 import service_account
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.snippets.news import News


@dataclass(frozen=True)
class SeedResult:
    created: int
    skipped: int
    total: int


_NEWS_ITEMS = [
    {
        "title": "Gobierno lanza programa de becas para jovenes en todo Mexico",
        "summary": "Se abren nuevas becas para apoyar a estudiantes de nivel medio y superior.",
        "body": "El gobierno de Mexico anuncio el inicio de un programa nacional de becas para jovenes. La iniciativa busca reducir la desercion escolar y apoyar a estudiantes con buen desempeno academico. El registro estara disponible durante las proximas cuatro semanas y se podra realizar en linea.",
        "image_url": "https://images.unsplash.com/photo-1523050854058-8df90110c9f1?auto=format&fit=crop&w=1200&q=80",
        "priority": 70,
    },
    {
        "title": "Arranca campana nacional de vacunacion de temporada",
        "summary": "Salud federal inicia jornadas de vacunacion en centros comunitarios.",
        "body": "La Secretaria de Salud informo que este mes inicia la campana nacional de vacunacion de temporada. Se priorizara a ninos, adultos mayores y personas con enfermedades cronicas. Las brigadas estaran presentes en clinicas y modulos itinerantes.",
        "image_url": "https://images.unsplash.com/photo-1580281657527-47f249e8f296?auto=format&fit=crop&w=1200&q=80",
        "priority": 65,
    },
    {
        "title": "Se modernizan tramites digitales en dependencias federales",
        "summary": "Nuevas plataformas buscan reducir tiempos de espera y filas.",
        "body": "El gobierno federal presento una actualizacion de sus plataformas de tramites digitales. El objetivo es ofrecer procesos mas rapidos y transparentes en pagos, solicitudes y consultas ciudadanas. Se habilitaran tutoriales y asistencia en linea.",
        "image_url": "https://images.unsplash.com/photo-1519389950473-47ba0277781c?auto=format&fit=crop&w=1200&q=80",
        "priority": 60,
    },
    {
        "title": "Inversion en infraestructura carretera para regiones prioritarias",
        "summary": "Se anuncia mantenimiento y ampliacion de rutas federales.",
        "body": "La Secretaria de Infraestructura confirmo un plan de mantenimiento y ampliacion de rutas federales en zonas prioritarias. El proyecto contempla mejoras de seguridad vial, puentes y senalamientos. Se espera concluir la primera fase en seis meses.",
        "image_url": "https://images.unsplash.com/photo-1500530855697-b586d89ba3ee?auto=format&fit=crop&w=1200&q=80",
        "priority": 62,
    },
    {
        "title": "Programa de apoyo a productores rurales entra en nueva etapa",
        "summary": "Se entregaran insumos y capacitacion tecnica a campesinos.",
        "body": "El gobierno de Mexico anuncio la nueva etapa de apoyo a productores rurales. Se entregaran insumos, paquetes tecnologicos y capacitacion tecnica para mejorar la productividad. El programa contempla seguimiento trimestral.",
        "image_url": "https://images.unsplash.com/photo-1501004318641-b39e6451bec6?auto=format&fit=crop&w=1200&q=80",
        "priority": 58,
    },
    {
        "title": "Se fortalecen acciones de seguridad en zonas urbanas",
        "summary": "Coordinacion con estados para incrementar patrullajes.",
        "body": "Autoridades federales informaron que se reforzaran operativos de seguridad en zonas urbanas con alta incidencia delictiva. Se implementaran patrullajes coordinados y acciones preventivas en espacios publicos.",
        "image_url": "https://images.unsplash.com/photo-1524499982521-1ffd58dd89ea?auto=format&fit=crop&w=1200&q=80",
        "priority": 64,
    },
    {
        "title": "Nuevos centros de atencion ciudadana en todo el pais",
        "summary": "Se abren oficinas para orientar tramites y servicios.",
        "body": "Se inauguran centros de atencion ciudadana para ofrecer orientacion sobre programas sociales, tramites y servicios. El objetivo es reducir tiempos de respuesta y brindar acompanamiento a la poblacion.",
        "image_url": "https://images.unsplash.com/photo-1465447142348-e9952c393450?auto=format&fit=crop&w=1200&q=80",
        "priority": 55,
    },
    {
        "title": "Gobierno impulsa plan de movilidad sustentable",
        "summary": "Se promueve el transporte publico y la infraestructura ciclista.",
        "body": "El plan nacional de movilidad sustentable incluye renovacion de unidades de transporte publico y expansion de ciclovias. Se busca reducir emisiones y mejorar la conectividad en zonas metropolitanas.",
        "image_url": "https://images.unsplash.com/photo-1500534314209-a25ddb2bd429?auto=format&fit=crop&w=1200&q=80",
        "priority": 57,
    },
    {
        "title": "Anuncian medidas para simplificar licencias y permisos",
        "summary": "Menos requisitos para tramites de bajo riesgo.",
        "body": "Dependencias federales acordaron simplificar requisitos para licencias y permisos considerados de bajo riesgo. La medida busca acelerar inversiones y reducir la carga administrativa.",
        "image_url": "https://images.unsplash.com/photo-1520607162513-77705c0f0d4a?auto=format&fit=crop&w=1200&q=80",
        "priority": 52,
    },
    {
        "title": "Se amplian programas de apoyo para madres trabajadoras",
        "summary": "Nuevos apoyos y estancias infantiles en zonas prioritarias.",
        "body": "El gobierno federal anuncio la ampliacion de programas para madres trabajadoras, incluyendo apoyos economicos y nuevas estancias infantiles. El registro se realizara por regiones con calendario oficial.",
        "image_url": "https://images.unsplash.com/photo-1504151932400-72d4384f04b3?auto=format&fit=crop&w=1200&q=80",
        "priority": 63,
    },
    {
        "title": "Plan nacional de reforestacion suma nuevas metas",
        "summary": "Se plantaran miles de arboles en zonas degradadas.",
        "body": "La estrategia nacional de reforestacion incremento sus metas anuales. Se trabajara con comunidades locales y brigadas ambientales para recuperar zonas degradadas y proteger cuencas.",
        "image_url": "https://images.unsplash.com/photo-1500530855697-b586d89ba3ee?auto=format&fit=crop&w=1200&q=80",
        "priority": 54,
    },
    {
        "title": "Gobierno anuncia mejora en servicios de salud comunitarios",
        "summary": "Se rehabilitan clinicas y se asigna nuevo personal.",
        "body": "Se destinaran recursos para rehabilitar clinicas comunitarias y contratar personal medico. El plan prioriza municipios con alta demanda y baja cobertura.",
        "image_url": "https://images.unsplash.com/photo-1576091160399-112ba8d25d1d?auto=format&fit=crop&w=1200&q=80",
        "priority": 61,
    },
    {
        "title": "Inicia programa de conectividad digital en escuelas publicas",
        "summary": "Se instalara internet en planteles de educacion basica.",
        "body": "La Secretaria de Educacion informo el arranque de un programa de conectividad digital en escuelas publicas. Se instalaran equipos y redes para mejorar el acceso a recursos educativos.",
        "image_url": "https://images.unsplash.com/photo-1498050108023-c5249f4df085?auto=format&fit=crop&w=1200&q=80",
        "priority": 59,
    },
    {
        "title": "Se habilita nueva linea de apoyo a PYMES",
        "summary": "Credito y asesoria para negocios locales.",
        "body": "El gobierno federal presento una nueva linea de apoyo a PYMES con credito preferencial y asesoria tecnica. El objetivo es fortalecer la economia local y el empleo.",
        "image_url": "https://images.unsplash.com/photo-1450101499163-c8848c66ca85?auto=format&fit=crop&w=1200&q=80",
        "priority": 56,
    },
    {
        "title": "Refuerzan acciones contra incendios forestales",
        "summary": "Se despliegan brigadas y equipo especializado.",
        "body": "Se reforzo el despliegue de brigadas contra incendios forestales en varias regiones. El plan incluye monitoreo satelital y capacitacion para respuesta rapida.",
        "image_url": "https://images.unsplash.com/photo-1477414348463-c0eb7f1359b6?auto=format&fit=crop&w=1200&q=80",
        "priority": 66,
    },
    {
        "title": "Se anuncia nuevo plan de vivienda social",
        "summary": "Apoyos para mejora y construccion de vivienda.",
        "body": "El plan de vivienda social contempla apoyos para mejora y construccion en zonas urbanas y rurales. Se priorizara a familias con mayores necesidades habitacionales.",
        "image_url": "https://images.unsplash.com/photo-1505691938895-1758d7feb511?auto=format&fit=crop&w=1200&q=80",
        "priority": 60,
    },
    {
        "title": "Nueva estrategia de atencion a juventudes en riesgo",
        "summary": "Se promueven actividades culturales y deportivas.",
        "body": "El gobierno presento una estrategia enfocada en juventudes en riesgo con programas culturales, deportivos y de capacitacion laboral. Se buscara cobertura en colonias prioritarias.",
        "image_url": "https://images.unsplash.com/photo-1500530855697-b586d89ba3ee?auto=format&fit=crop&w=1200&q=80",
        "priority": 58,
    },
    {
        "title": "Se amplian jornadas de registro civil en comunidades",
        "summary": "Tramites gratuitos y brigadas itinerantes.",
        "body": "El registro civil ampliara jornadas con brigadas itinerantes para facilitar tramites en comunidades alejadas. Se ofreceran actas de nacimiento y asesorias sin costo.",
        "image_url": "https://images.unsplash.com/photo-1454165804606-c3d57bc86b40?auto=format&fit=crop&w=1200&q=80",
        "priority": 53,
    },
    {
        "title": "Plan nacional de agua prioriza infraestructura y ahorro",
        "summary": "Se rehabilitaran redes y se impulsara el uso eficiente.",
        "body": "El plan nacional de agua contempla rehabilitacion de redes, mantenimiento de presas y campanas de uso eficiente. Se priorizaran regiones con mayor estres hidrico.",
        "image_url": "https://images.unsplash.com/photo-1501785888041-af3ef285b470?auto=format&fit=crop&w=1200&q=80",
        "priority": 67,
    },
    {
        "title": "Se fortalece la atencion a emergencias y proteccion civil",
        "summary": "Capacitacion y equipamiento para respuesta rapida.",
        "body": "Proteccion civil fortalecera la atencion a emergencias con capacitacion y equipamiento en municipios clave. Se implementaran simulacros y protocolos de respuesta.",
        "image_url": "https://images.unsplash.com/photo-1500530855697-b586d89ba3ee?auto=format&fit=crop&w=1200&q=80",
        "priority": 68,
    },
]


def _slugify(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "news"


def _object_name(index: int, title: str, prefix: str) -> str:
    slug = _slugify(title)
    return f"{prefix}/{index:02d}-{slug}.jpg"


def _gcs_bucket() -> storage.Bucket:
    bucket_name = os.getenv("GCS_BUCKET", "").strip()
    if not bucket_name:
        raise RuntimeError("GCS_BUCKET is required")

    sa_b64 = os.getenv("GCP_SA_KEY_B64", "").strip()
    if not sa_b64:
        raise RuntimeError("GCP_SA_KEY_B64 is required")

    info = json.loads(base64.b64decode(sa_b64).decode("utf-8"))
    creds = service_account.Credentials.from_service_account_info(info)
    client = storage.Client(credentials=creds, project=info.get("project_id"))
    return client.bucket(bucket_name)


def _download_image(url: str) -> bytes:
    resp = requests.get(url, timeout=20)
    if resp.status_code >= 400:
        raise RuntimeError(f"image download failed: {resp.status_code} {url}")
    return resp.content


def _upload_if_missing(bucket: storage.Bucket, object_name: str, image_url: str) -> None:
    if bucket.get_blob(object_name) is not None:
        return
    data = _download_image(image_url)
    blob = bucket.blob(object_name)
    blob.upload_from_string(data, content_type="image/jpeg")


def seed_news(
    db: Session,
    prefix: str = "news/seed",
    items: Iterable[dict] | None = None,
) -> SeedResult:
    news_items = list(items or _NEWS_ITEMS)
    bucket = _gcs_bucket()
    now = dt.datetime.now(dt.timezone.utc)

    created = 0
    skipped = 0
    for idx, item in enumerate(news_items, start=1):
        existing = db.execute(
            select(News).where(News.title == item["title"])
        ).scalar_one_or_none()
        if existing:
            skipped += 1
            continue

        object_name = _object_name(idx, item["title"], prefix)
        _upload_if_missing(bucket, object_name, item["image_url"])

        n = News(
            title=item["title"],
            summary=item.get("summary"),
            body=item["body"],
            image_object_name=object_name,
            status="published",
            priority=item.get("priority", 50),
            scope_type="global",
            scope_value=None,
            pinned_until=None,
            published_at=now,
        )
        db.add(n)
        created += 1

    db.commit()
    return SeedResult(created=created, skipped=skipped, total=len(news_items))
