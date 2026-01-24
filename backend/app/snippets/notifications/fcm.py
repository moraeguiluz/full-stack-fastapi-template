import base64
import json
from typing import Any, Dict, Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DeviceTokenForNotifications

_SCOPES = ["https://www.googleapis.com/auth/firebase.messaging"]
_SA_B64 = None
_creds = None
_project_id = None
_inited = False


def _init_fcm() -> None:
    global _inited, _creds, _project_id, _SA_B64
    if _inited:
        return
    import os

    _SA_B64 = os.getenv("GCP_SA_KEY_B64", "").strip()
    if not _SA_B64:
        return

    try:
        info = json.loads(base64.b64decode(_SA_B64).decode("utf-8"))
        _project_id = info.get("project_id")
        _creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
        _inited = True
    except Exception:
        _creds = None
        _project_id = None
        _inited = False


def _access_token() -> tuple[Optional[str], Optional[str]]:
    _init_fcm()
    if not _creds or not _project_id:
        return None, None
    if not _creds.valid or _creds.expired:
        _creds.refresh(Request())
    return _creds.token, _project_id


def _stringify_data(data: Optional[Dict[str, Any]]) -> Dict[str, str]:
    if not data:
        return {}
    return {str(k): "" if v is None else str(v) for k, v in data.items()}


def send_to_token(token: str, title: str, body: str, data: Optional[Dict[str, Any]] = None) -> bool:
    access_token, project_id = _access_token()
    if not access_token or not project_id:
        return False

    payload = {
        "message": {
            "token": token,
            "notification": {
                "title": title,
                "body": body,
            },
            "data": _stringify_data(data),
        }
    }

    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        return resp.status_code < 300
    except requests.RequestException:
        return False


def send_to_user(
    db: Session,
    user_id: int,
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
) -> int:
    stmt = select(DeviceTokenForNotifications.token).where(
        DeviceTokenForNotifications.user_id == user_id,
        DeviceTokenForNotifications.revoked_at.is_(None),
    )
    tokens = [row[0] for row in db.execute(stmt).all()]
    sent = 0
    for token in tokens:
        if send_to_token(token, title, body, data):
            sent += 1
    return sent
