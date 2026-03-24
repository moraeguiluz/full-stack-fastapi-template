from __future__ import annotations

import base64
import datetime as dt
import json
import os
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from google.cloud import storage
from google.oauth2 import service_account

ENABLED = True
ROUTER_PREFIX = ""
router = APIRouter(include_in_schema=False)

_DEFAULT_BUCKET = "bonube"
_DEFAULT_OBJECT_NAME = "app-arm64-v8a-release.apk"

_BUCKET = (
    os.getenv("APP_DOWNLOAD_GCS_BUCKET", "").strip()
    or os.getenv("GCS_BUCKET", "").strip()
    or _DEFAULT_BUCKET
)
_SA_B64 = os.getenv("APP_DOWNLOAD_GCP_SA_KEY_B64", "").strip() or os.getenv("GCP_SA_KEY_B64", "").strip()
_OBJECT_NAME = os.getenv("APP_DOWNLOAD_OBJECT_NAME", _DEFAULT_OBJECT_NAME).strip().strip("/")
_PUBLIC_URL = os.getenv("APP_DOWNLOAD_PUBLIC_URL", "").strip()
_FILENAME = os.getenv("APP_DOWNLOAD_FILENAME", _DEFAULT_OBJECT_NAME).strip() or _DEFAULT_OBJECT_NAME

try:
    _EXPIRES_MINUTES = max(1, min(int(os.getenv("APP_DOWNLOAD_EXPIRES_MINUTES", "15")), 60))
except Exception:
    _EXPIRES_MINUTES = 15

_client: Optional[storage.Client] = None
_inited = False


def _init_gcs() -> None:
    global _client, _inited
    if _inited:
        return
    if _PUBLIC_URL:
        _inited = True
        return
    if not (_BUCKET and _SA_B64):
        return
    try:
        info = json.loads(base64.b64decode(_SA_B64).decode("utf-8"))
        creds = service_account.Credentials.from_service_account_info(info)
        _client = storage.Client(credentials=creds, project=info.get("project_id"))
        _inited = True
    except Exception:
        _client = None
        _inited = False


def _gcs() -> storage.Client:
    _init_gcs()
    if not _BUCKET:
        raise HTTPException(503, "APP_DOWNLOAD_GCS_BUCKET o GCS_BUCKET no configurado")
    if not _client:
        raise HTTPException(503, "GCS no configurado para descargas de app")
    return _client


def _bucket():
    return _gcs().bucket(_BUCKET)


def _download_url() -> str:
    if _PUBLIC_URL:
        return _PUBLIC_URL
    if not _OBJECT_NAME:
        raise HTTPException(503, "APP_DOWNLOAD_OBJECT_NAME no configurado")
    blob = _bucket().blob(_OBJECT_NAME)
    return blob.generate_signed_url(
        version="v4",
        expiration=dt.timedelta(minutes=_EXPIRES_MINUTES),
        method="GET",
        response_disposition=f'attachment; filename="{_FILENAME}"',
    )


@router.get("/appdownload")
def app_download():
    return RedirectResponse(_download_url(), status_code=307)


@router.get("/appdownload/health")
def app_download_health():
    _init_gcs()
    return {
        "ok": bool(_PUBLIC_URL) or bool(_BUCKET and _client and _OBJECT_NAME),
        "uses_public_url": bool(_PUBLIC_URL),
        "bucket": _BUCKET or None,
        "object_name": _OBJECT_NAME or None,
        "expires_minutes": _EXPIRES_MINUTES,
    }
