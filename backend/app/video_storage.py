from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class StorageConfig:
    endpoint: str
    bucket: str
    region: str
    access_key: str
    secret_key: str


_DEFAULT_REGION = os.getenv("VOS_REGION", "ewr1").strip() or "ewr1"


def _endpoint() -> str:
    raw = os.getenv("VOS_ENDPOINT", "").strip().rstrip("/")
    if not raw:
        return ""
    if "://" not in raw:
        return f"https://{raw}"
    return raw


def get_config() -> StorageConfig:
    return StorageConfig(
        endpoint=_endpoint(),
        bucket=os.getenv("VOS_BUCKET", "").strip(),
        region=_DEFAULT_REGION,
        access_key=os.getenv("VOS_ACCESS_KEY", "").strip(),
        secret_key=os.getenv("VOS_SECRET_KEY", "").strip(),
    )


def is_configured() -> bool:
    cfg = get_config()
    return bool(cfg.endpoint and cfg.bucket and cfg.access_key and cfg.secret_key)


def health() -> Dict[str, Optional[str]]:
    cfg = get_config()
    return {
        "ok": "true" if is_configured() else "false",
        "endpoint": cfg.endpoint or None,
        "bucket": cfg.bucket or None,
        "region": cfg.region or None,
    }


def _require_config() -> StorageConfig:
    cfg = get_config()
    if not (cfg.endpoint and cfg.bucket and cfg.access_key and cfg.secret_key):
        raise RuntimeError("Vultr Object Storage no configurado")
    return cfg


def _aws_quote(value: str, *, safe: str = "-_.~/") -> str:
    return quote(value, safe=safe)


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, datestamp: str, region: str, service: str = "s3") -> bytes:
    k_date = _sign(f"AWS4{secret_key}".encode("utf-8"), datestamp)
    k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()


def _canonical_uri(bucket: str, object_name: str) -> str:
    key = object_name.strip("/")
    return f"/{_aws_quote(bucket, safe='-_.~')}/{_aws_quote(key)}"


def object_url(object_name: str) -> str:
    cfg = _require_config()
    return f"{cfg.endpoint}{_canonical_uri(cfg.bucket, object_name)}"


def presign_url(
    method: str,
    object_name: str,
    *,
    expires_seconds: int = 900,
    extra_query: Optional[Dict[str, str]] = None,
) -> str:
    cfg = _require_config()
    parsed = urlparse(cfg.endpoint)
    host = parsed.netloc
    now = dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    credential_scope = f"{datestamp}/{cfg.region}/s3/aws4_request"
    canonical_uri = _canonical_uri(cfg.bucket, object_name)

    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{cfg.access_key}/{credential_scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(max(1, min(int(expires_seconds), 3600))),
        "X-Amz-SignedHeaders": "host",
    }
    if extra_query:
        for key, value in extra_query.items():
            query[key] = str(value)

    canonical_query = "&".join(
        f"{_aws_quote(str(k), safe='-_.~')}={_aws_quote(str(v), safe='-_.~')}"
        for k, v in sorted(query.items())
    )
    canonical_headers = f"host:{host}\n"
    canonical_request = "\n".join(
        [
            method.upper(),
            canonical_uri,
            canonical_query,
            canonical_headers,
            "host",
            "UNSIGNED-PAYLOAD",
        ]
    )
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(cfg.secret_key, datestamp, cfg.region),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{cfg.endpoint}{canonical_uri}?{canonical_query}&X-Amz-Signature={signature}"


def presign_upload_url(object_name: str, *, expires_seconds: int = 900) -> str:
    return presign_url("PUT", object_name, expires_seconds=expires_seconds)


def presign_download_url(
    object_name: str,
    *,
    expires_seconds: int = 900,
    filename: Optional[str] = None,
) -> str:
    extra_query = None
    if filename:
        extra_query = {
            "response-content-disposition": f'inline; filename="{filename}"',
        }
    return presign_url("GET", object_name, expires_seconds=expires_seconds, extra_query=extra_query)


def upload_bytes(object_name: str, payload: bytes, *, content_type: str) -> None:
    url = presign_upload_url(object_name, expires_seconds=900)
    req = Request(url, data=payload, method="PUT", headers={"Content-Type": content_type})
    with urlopen(req) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"Upload falló ({resp.status}) para {object_name}")


def upload_text(object_name: str, text: str, *, content_type: str = "application/octet-stream") -> None:
    upload_bytes(object_name, text.encode("utf-8"), content_type=content_type)


def download_to_file(object_name: str, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    url = presign_download_url(object_name, expires_seconds=900)
    with urlopen(url) as resp, target_path.open("wb") as fh:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)


def download_text(object_name: str) -> str:
    url = presign_download_url(object_name, expires_seconds=300)
    with urlopen(url) as resp:
        return resp.read().decode("utf-8")


def content_type_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".m3u8":
        return "application/vnd.apple.mpegurl"
    if suffix == ".ts":
        return "video/mp2t"
    if suffix == ".jpg" or suffix == ".jpeg":
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".mp4":
        return "video/mp4"
    return "application/octet-stream"
