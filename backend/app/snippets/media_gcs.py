# backend/app/snippets/media_gcs.py
from __future__ import annotations

import os, uuid, json, base64, datetime as dt
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from google.cloud import storage
from google.oauth2 import service_account

router = APIRouter(prefix="/media", tags=["media-gcs"])

# -------------------- Config & lazy init --------------------
_BUCKET = os.getenv("GCS_BUCKET", "").strip()
_SA_B64 = os.getenv("GCP_SA_KEY_B64", "").strip()

_client: Optional[storage.Client] = None
_inited = False
_project_id = None

def _init_gcs():
    """Inicializa cliente GCS al primer uso; no rompe el arranque si falta env."""
    global _client, _inited, _project_id
    if _inited:
        return
    if not _BUCKET:
        return  # se validará en runtime
    if not _SA_B64:
        return

    info = json.loads(base64.b64decode(_SA_B64).decode("utf-8"))
    _project_id = info.get("project_id")
    creds = service_account.Credentials.from_service_account_info(info)
    _client = storage.Client(credentials=creds, project=_project_id)
    _inited = True

def _gcs() -> storage.Client:
    _init_gcs()
    if not _BUCKET:
        raise HTTPException(503, detail="GCS no configurado (falta GCS_BUCKET)")
    if not _client:
        raise HTTPException(503, detail="GCS no configurado (falta GCP_SA_KEY_B64 o inválido)")
    return _client

def _bucket():
    return _gcs().bucket(_BUCKET)

# -------------------- Schemas --------------------
class SignUploadIn(BaseModel):
    content_type: str = Field(..., examples=["image/jpeg", "video/mp4"])
    prefix: str = Field("uploads", description="Carpeta dentro del bucket")
    expires_minutes: int = Field(15, ge=1, le=60)

class SignUploadOut(BaseModel):
    object_name: str
    upload_url: str

class SignDownloadOut(BaseModel):
    download_url: str

# -------------------- Endpoints --------------------
@router.get("/health")
def health():
    _init_gcs()
    return {
        "ok": bool(_client) and bool(_BUCKET),
        "bucket": _BUCKET or None,
        "project_id": _project_id,
        "inited": _inited,
    }

@router.post("/sign-upload", response_model=SignUploadOut)
def sign_upload(inp: SignUploadIn):
    if not (inp.content_type.startswith("image/") or inp.content_type.startswith("video/")):
        raise HTTPException(400, "content_type debe ser image/* o video/*")

    prefix = (inp.prefix or "uploads").strip().strip("/")
    object_name = f"{prefix}/{uuid.uuid4().hex}"

    blob = _bucket().blob(object_name)
    url = blob.generate_signed_url(
        version="v4",
        expiration=dt.timedelta(minutes=inp.expires_minutes),
        method="PUT",
        content_type=inp.content_type,
    )
    return SignUploadOut(object_name=object_name, upload_url=url)

@router.get("/sign-download", response_model=SignDownloadOut)
def sign_download(object_name: str, expires_minutes: int = 15):
    if not object_name:
        raise HTTPException(400, "object_name requerido")

    blob = _bucket().blob(object_name)
    url = blob.generate_signed_url(
        version="v4",
        expiration=dt.timedelta(minutes=max(1, min(expires_minutes, 60))),
        method="GET",
    )
    return SignDownloadOut(download_url=url)
