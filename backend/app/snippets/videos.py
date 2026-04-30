from __future__ import annotations

import datetime as dt
import hashlib
import os
import secrets
import time
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    and_,
    create_engine,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app import video_storage

router = APIRouter(prefix="/videos", tags=["videos"])

_DB_URL = os.getenv("DATABASE_URL")
_SECRET = os.getenv("SECRET_KEY", "dev-change-me")
_ALG = "HS256"
_PLAYBACK_TOKEN_TTL = max(60, min(int(os.getenv("VIDEO_PLAYBACK_TOKEN_TTL", "900")), 3600))
_SIGNED_SEGMENT_TTL = max(60, min(int(os.getenv("VIDEO_SIGNED_SEGMENT_TTL", "900")), 3600))

_engine = None
_SessionLocal = None
_inited = False

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/finalize")

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class BaseOwn(DeclarativeBase):
    pass


class BaseRO(DeclarativeBase):
    pass


class User(BaseRO):
    __tablename__ = "app_user_auth"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nombre: Mapped[Optional[str]] = mapped_column(String(120), default="")
    apellido_paterno: Mapped[Optional[str]] = mapped_column(String(120), default="")
    apellido_materno: Mapped[Optional[str]] = mapped_column(String(120), default="")
    telefono: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Video(BaseOwn):
    __tablename__ = "app_video"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(26), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    codigo_base: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    visibility: Mapped[int] = mapped_column(SmallInteger, default=0, index=True)
    title: Mapped[str] = mapped_column(String(180), default="")
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    source_object_name: Mapped[Optional[str]] = mapped_column(String(600), nullable=True)
    source_mime: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    source_filename: Mapped[Optional[str]] = mapped_column(String(260), nullable=True)
    source_size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    hls_prefix: Mapped[Optional[str]] = mapped_column(String(600), nullable=True)
    master_playlist_object_name: Mapped[Optional[str]] = mapped_column(String(600), nullable=True)
    poster_object_name: Mapped[Optional[str]] = mapped_column(String(600), nullable=True)
    duration_s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    processing_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extra: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    uploaded_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


Index("ix_video_ready_feed", Video.status, Video.visibility, Video.id.desc())
Index("ix_video_user_recent", Video.user_id, Video.id.desc())


class VideoJob(BaseOwn):
    __tablename__ = "app_video_job"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    video_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(32), default="transcode_hls", index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    worker_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    lease_token: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    leased_until: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    error_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


Index("ix_video_job_lease", VideoJob.kind, VideoJob.status, VideoJob.created_at.asc())


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _encode_crockford(value: int, length: int) -> str:
    out: List[str] = []
    for _ in range(length):
        out.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(out))


def _new_public_id() -> str:
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    rnd = secrets.randbits(80)
    return _encode_crockford(ts, 10) + _encode_crockford(rnd, 16)


def _init_db():
    global _engine, _SessionLocal, _inited
    if _inited or not _DB_URL:
        return
    url = _DB_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    _engine = create_engine(url, pool_pre_ping=True)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    BaseOwn.metadata.create_all(bind=_engine)
    _inited = True


def get_db():
    _init_db()
    if not _SessionLocal:
        raise HTTPException(status_code=503, detail="DB no configurada (falta DATABASE_URL)")
    db: Session = _SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _decode_uid(token: str) -> int:
    try:
        data = jwt.decode(token, _SECRET, algorithms=[_ALG])
        uid = data.get("sub")
        if not uid:
            raise HTTPException(401, "Token inválido")
        return int(uid)
    except jwt.PyJWTError:
        raise HTTPException(401, "Token inválido")


def _full_name(user: Optional[User]) -> str:
    if not user:
        return ""
    parts = [user.nombre or "", user.apellido_paterno or "", user.apellido_materno or ""]
    return " ".join([part.strip() for part in parts if part and part.strip()]).strip()


def _sanitize_extension(filename: Optional[str], content_type: str) -> str:
    if filename:
        suffix = PurePosixPath(filename).suffix.lower()
        if suffix and len(suffix) <= 10:
            return suffix
    mapping = {
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/x-matroska": ".mkv",
        "video/webm": ".webm",
    }
    return mapping.get(content_type.lower(), ".mp4")


def _source_object_name(public_id: str, filename: Optional[str], content_type: str) -> str:
    ext = _sanitize_extension(filename, content_type)
    return f"videos/source/{public_id}/original{ext}"


def _playback_token(video: Video, uid: int) -> str:
    payload = {
        "typ": "video_playback",
        "sub": str(uid),
        "vid": video.public_id,
        "exp": _now() + dt.timedelta(seconds=_PLAYBACK_TOKEN_TTL),
    }
    return jwt.encode(payload, _SECRET, algorithm=_ALG)


def _decode_playback_token(token: str, public_id: str) -> int:
    try:
        data = jwt.decode(token, _SECRET, algorithms=[_ALG])
        if data.get("typ") != "video_playback" or data.get("vid") != public_id:
            raise HTTPException(401, "Token de reproducción inválido")
        return int(data.get("sub") or 0)
    except jwt.PyJWTError:
        raise HTTPException(401, "Token de reproducción inválido")


def _video_author_map(db: Session, videos: List[Video]) -> Dict[int, User]:
    user_ids = sorted({int(v.user_id) for v in videos})
    if not user_ids:
        return {}
    users = db.execute(select(User).where(User.id.in_(user_ids))).scalars().all()
    return {int(user.id): user for user in users}


def _poster_url(object_name: Optional[str]) -> Optional[str]:
    if not object_name or not video_storage.is_configured():
        return None
    return video_storage.presign_download_url(object_name, expires_seconds=900)


class VideoAuthorOut(BaseModel):
    id: int
    nombre_completo: str
    telefono: Optional[str] = None


class VideoOut(BaseModel):
    public_id: str
    user_id: int
    author: VideoAuthorOut
    codigo_base: Optional[str] = None
    visibility: int
    title: str
    description: Optional[str] = None
    status: str
    duration_s: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    poster_object_name: Optional[str] = None
    poster_download_url: Optional[str] = None
    master_playlist_object_name: Optional[str] = None
    source_filename: Optional[str] = None
    source_mime: Optional[str] = None
    source_size_bytes: Optional[int] = None
    processing_error: Optional[str] = None
    created_at: Optional[dt.datetime] = None
    uploaded_at: Optional[dt.datetime] = None
    published_at: Optional[dt.datetime] = None
    is_owner: bool = False


class VideoFeedOut(BaseModel):
    items: List[VideoOut]
    next_before_id: Optional[int] = None


class VideoUploadInitIn(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    description: Optional[str] = None
    filename: Optional[str] = Field(default=None, max_length=260)
    content_type: str = Field(..., examples=["video/mp4"])
    visibility: int = Field(default=0, ge=0, le=2)
    codigo_base: Optional[str] = Field(default=None, max_length=64)
    extra: Dict[str, Any] = Field(default_factory=dict)


class VideoUploadInitOut(BaseModel):
    video: VideoOut
    source_object_name: str
    upload_url: str
    expires_seconds: int


class VideoMarkUploadedIn(BaseModel):
    source_size_bytes: Optional[int] = Field(default=None, ge=0)
    checksum_sha256: Optional[str] = Field(default=None, min_length=16, max_length=128)


class VideoPlaybackOut(BaseModel):
    playlist_url: str
    expires_seconds: int


def _to_video_out(video: Video, author: Optional[User], *, uid: int) -> VideoOut:
    return VideoOut(
        public_id=video.public_id,
        user_id=int(video.user_id),
        author=VideoAuthorOut(
            id=int(video.user_id),
            nombre_completo=_full_name(author),
            telefono=author.telefono if author else None,
        ),
        codigo_base=video.codigo_base,
        visibility=int(video.visibility),
        title=video.title,
        description=video.description,
        status=video.status,
        duration_s=video.duration_s,
        width=video.width,
        height=video.height,
        poster_object_name=video.poster_object_name,
        poster_download_url=_poster_url(video.poster_object_name),
        master_playlist_object_name=video.master_playlist_object_name,
        source_filename=video.source_filename,
        source_mime=video.source_mime,
        source_size_bytes=video.source_size_bytes,
        processing_error=video.processing_error,
        created_at=video.created_at,
        uploaded_at=video.uploaded_at,
        published_at=video.published_at,
        is_owner=int(video.user_id) == uid,
    )


def _can_view(video: Video, uid: int) -> bool:
    if int(video.user_id) == uid:
        return True
    return video.status == "ready" and int(video.visibility) == 0


def _get_video_by_public_id(db: Session, public_id: str) -> Video:
    video = db.execute(select(Video).where(Video.public_id == public_id)).scalar_one_or_none()
    if video is None:
        raise HTTPException(404, "Video no encontrado")
    return video


def _ensure_job(db: Session, video: Video) -> None:
    existing = db.execute(
        select(VideoJob).where(
            VideoJob.video_id == video.id,
            VideoJob.kind == "transcode_hls",
            VideoJob.status.in_(["pending", "leased"]),
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    db.add(
        VideoJob(
            video_id=video.id,
            kind="transcode_hls",
            status="pending",
            payload_json={"video_public_id": video.public_id},
        )
    )


def _storage_ready() -> None:
    if not video_storage.is_configured():
        raise HTTPException(503, "Object Storage no configurado")


def _rewrite_master_playlist(text: str, request: Request, public_id: str, token: str) -> str:
    out: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            out.append(raw_line)
            continue
        child_url = str(request.url_for("video_hls_path", public_id=public_id, path=line))
        out.append(f"{child_url}?{urlencode({'token': token})}")
    return "\n".join(out) + "\n"


def _rewrite_variant_playlist(text: str, object_prefix: str) -> str:
    out: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            out.append(raw_line)
            continue
        object_name = f"{object_prefix}/{line}".strip("/")
        out.append(video_storage.presign_download_url(object_name, expires_seconds=_SIGNED_SEGMENT_TTL))
    return "\n".join(out) + "\n"


@router.get("/health")
def health():
    _init_db()
    return {
        "db_inited": _inited,
        "storage": video_storage.health(),
    }


@router.post("/uploads/init", response_model=VideoUploadInitOut)
def init_video_upload(
    payload: VideoUploadInitIn,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2),
):
    _storage_ready()
    uid = _decode_uid(token)
    content_type = payload.content_type.strip().lower()
    if not content_type.startswith("video/"):
        raise HTTPException(400, "content_type debe ser video/*")

    public_id = _new_public_id()
    object_name = _source_object_name(public_id, payload.filename, content_type)
    video = Video(
        public_id=public_id,
        user_id=uid,
        codigo_base=(payload.codigo_base or None),
        visibility=payload.visibility,
        title=payload.title.strip(),
        description=(payload.description or None),
        status="draft",
        source_object_name=object_name,
        source_mime=content_type,
        source_filename=(payload.filename or None),
        extra=payload.extra or {},
    )
    db.add(video)
    db.commit()
    db.refresh(video)

    upload_url = video_storage.presign_upload_url(object_name, expires_seconds=900)
    author = db.execute(select(User).where(User.id == uid)).scalar_one_or_none()
    return VideoUploadInitOut(
        video=_to_video_out(video, author, uid=uid),
        source_object_name=object_name,
        upload_url=upload_url,
        expires_seconds=900,
    )


@router.post("/{public_id}/mark-uploaded", response_model=VideoOut)
def mark_uploaded(
    public_id: str,
    payload: VideoMarkUploadedIn,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2),
):
    uid = _decode_uid(token)
    video = _get_video_by_public_id(db, public_id)
    if int(video.user_id) != uid:
        raise HTTPException(403, "No puedes actualizar este video")

    if video.status in {"processing", "ready"}:
        author = db.execute(select(User).where(User.id == uid)).scalar_one_or_none()
        return _to_video_out(video, author, uid=uid)

    video.status = "uploaded"
    video.uploaded_at = _now()
    video.processing_error = None
    if payload.source_size_bytes is not None:
        video.source_size_bytes = payload.source_size_bytes
    if payload.checksum_sha256:
        extra = dict(video.extra or {})
        extra["checksum_sha256"] = payload.checksum_sha256.lower()
        video.extra = extra
    _ensure_job(db, video)
    db.commit()
    db.refresh(video)
    author = db.execute(select(User).where(User.id == uid)).scalar_one_or_none()
    return _to_video_out(video, author, uid=uid)


@router.get("/feed", response_model=VideoFeedOut)
def feed(
    before_id: Optional[int] = None,
    limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
    token: str = Depends(oauth2),
):
    uid = _decode_uid(token)
    stmt = select(Video).where(Video.status == "ready", Video.visibility == 0)
    if before_id is not None:
        stmt = stmt.where(Video.id < before_id)
    items = db.execute(stmt.order_by(Video.id.desc()).limit(limit)).scalars().all()
    author_map = _video_author_map(db, items)
    return VideoFeedOut(
        items=[_to_video_out(video, author_map.get(int(video.user_id)), uid=uid) for video in items],
        next_before_id=(int(items[-1].id) if items else None),
    )


@router.get("/mine", response_model=VideoFeedOut)
def mine(
    before_id: Optional[int] = None,
    limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
    token: str = Depends(oauth2),
):
    uid = _decode_uid(token)
    stmt = select(Video).where(Video.user_id == uid)
    if before_id is not None:
        stmt = stmt.where(Video.id < before_id)
    items = db.execute(stmt.order_by(Video.id.desc()).limit(limit)).scalars().all()
    author_map = _video_author_map(db, items)
    return VideoFeedOut(
        items=[_to_video_out(video, author_map.get(int(video.user_id)), uid=uid) for video in items],
        next_before_id=(int(items[-1].id) if items else None),
    )


@router.get("/{public_id}", response_model=VideoOut)
def get_video(
    public_id: str,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2),
):
    uid = _decode_uid(token)
    video = _get_video_by_public_id(db, public_id)
    if not _can_view(video, uid):
        raise HTTPException(404, "Video no encontrado")
    author = db.execute(select(User).where(User.id == video.user_id)).scalar_one_or_none()
    return _to_video_out(video, author, uid=uid)


@router.get("/{public_id}/playback", response_model=VideoPlaybackOut)
def get_playback(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2),
):
    uid = _decode_uid(token)
    video = _get_video_by_public_id(db, public_id)
    if not _can_view(video, uid):
        raise HTTPException(404, "Video no encontrado")
    if video.status != "ready" or not video.master_playlist_object_name:
        raise HTTPException(409, "El video aún no está listo para reproducirse")
    playback_token = _playback_token(video, uid)
    playlist_url = str(request.url_for("video_hls_master", public_id=public_id))
    playlist_url = f"{playlist_url}?{urlencode({'token': playback_token})}"
    return VideoPlaybackOut(playlist_url=playlist_url, expires_seconds=_PLAYBACK_TOKEN_TTL)


@router.get("/{public_id}/hls/master.m3u8", include_in_schema=False, name="video_hls_master")
def hls_master(public_id: str, token: str, request: Request, db: Session = Depends(get_db)):
    _storage_ready()
    uid = _decode_playback_token(token, public_id)
    video = _get_video_by_public_id(db, public_id)
    if not _can_view(video, uid):
        raise HTTPException(404, "Video no encontrado")
    if not video.master_playlist_object_name:
        raise HTTPException(409, "Playlist aún no disponible")
    text = video_storage.download_text(video.master_playlist_object_name)
    return PlainTextResponse(
        _rewrite_master_playlist(text, request, public_id, token),
        media_type="application/vnd.apple.mpegurl",
    )


@router.get("/{public_id}/hls/{path:path}", include_in_schema=False, name="video_hls_path")
def hls_path(public_id: str, path: str, token: str, db: Session = Depends(get_db)):
    _storage_ready()
    uid = _decode_playback_token(token, public_id)
    video = _get_video_by_public_id(db, public_id)
    if not _can_view(video, uid):
        raise HTTPException(404, "Video no encontrado")
    if not video.hls_prefix:
        raise HTTPException(409, "HLS aún no disponible")
    if not path or path.startswith("/") or ".." in PurePosixPath(path).parts:
        raise HTTPException(400, "Ruta HLS inválida")

    object_name = f"{video.hls_prefix.rstrip('/')}/{path}"
    if path.endswith(".m3u8"):
        text = video_storage.download_text(object_name)
        object_prefix = str(PurePosixPath(object_name).parent)
        return PlainTextResponse(
            _rewrite_variant_playlist(text, object_prefix),
            media_type="application/vnd.apple.mpegurl",
        )
    raise HTTPException(404, "Recurso HLS no soportado")
