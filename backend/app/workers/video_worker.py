from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

from sqlalchemy import BigInteger, DateTime, Float, Integer, String, Text, create_engine, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app import video_storage

log = logging.getLogger("video_worker")
logging.basicConfig(level=os.getenv("VIDEO_WORKER_LOG_LEVEL", "INFO"))

_DB_URL = os.getenv("DATABASE_URL")
_POLL_SECONDS = max(2, int(os.getenv("VIDEO_WORKER_POLL_SECONDS", "5")))
_LEASE_SECONDS = max(60, int(os.getenv("VIDEO_WORKER_LEASE_SECONDS", "900")))
_TMP_DIR = Path(os.getenv("VIDEO_WORKER_TMP_DIR", "/var/lib/app-video")).resolve()
_WORKER_ID = os.getenv("VIDEO_WORKER_ID", socket.gethostname())
_FFMPEG_PRESET = os.getenv("VIDEO_FFMPEG_PRESET", "veryfast")
_HLS_TIME = max(2, int(os.getenv("VIDEO_HLS_SEGMENT_SECONDS", "4")))
_POSTER_AT = max(0.0, float(os.getenv("VIDEO_POSTER_AT_SECONDS", "1.0")))

_engine = None
_SessionLocal = None
_inited = False


class Base(DeclarativeBase):
    pass


class Video(Base):
    __tablename__ = "app_video"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(26), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
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
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    published_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class VideoJob(Base):
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
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _init_db() -> None:
    global _engine, _SessionLocal, _inited
    if _inited or not _DB_URL:
        return
    url = _DB_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    _engine = create_engine(url, pool_pre_ping=True)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    _inited = True


def get_db() -> Session:
    _init_db()
    if not _SessionLocal:
        raise RuntimeError("DATABASE_URL no configurada")
    return _SessionLocal()


def _run(args: List[str], *, cwd: Optional[Path] = None) -> None:
    log.info("exec: %s", " ".join(args))
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Comando falló ({proc.returncode}): {' '.join(args)}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )


def _probe(input_path: Path) -> Dict[str, Any]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(input_path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe falló: {proc.stderr}")
    return json.loads(proc.stdout)


def _pick_variants(width: int, height: int) -> List[Dict[str, Any]]:
    source_h = max(1, int(height))
    presets = [
        {"name": "1080p", "height": 1080, "vb": "5000k", "maxrate": "5350k", "bufsize": "7500k", "bandwidth": 5500000, "ab": "128k"},
        {"name": "720p", "height": 720, "vb": "2800k", "maxrate": "2996k", "bufsize": "4200k", "bandwidth": 3100000, "ab": "128k"},
        {"name": "480p", "height": 480, "vb": "1400k", "maxrate": "1498k", "bufsize": "2100k", "bandwidth": 1600000, "ab": "96k"},
        {"name": "360p", "height": 360, "vb": "900k", "maxrate": "963k", "bufsize": "1350k", "bandwidth": 1020000, "ab": "96k"},
    ]
    selected = [item for item in presets if source_h >= item["height"]]
    if not selected:
        fallback_height = max(180, min(source_h, 360))
        selected = [
            {
                "name": f"{fallback_height}p",
                "height": fallback_height,
                "vb": "700k",
                "maxrate": "749k",
                "bufsize": "1000k",
                "bandwidth": 820000,
                "ab": "96k",
            }
        ]
    return selected[:3]


def _fit_dimensions(width: int, height: int, target_h: int) -> tuple[int, int]:
    out_h = min(height, target_h)
    out_w = max(2, int(round((width * out_h) / max(1, height))))
    if out_w % 2:
        out_w -= 1
    if out_h % 2:
        out_h -= 1
    return max(2, out_w), max(2, out_h)


def _generate_poster(input_path: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{_POSTER_AT:.2f}",
            "-i",
            str(input_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(output_path),
        ]
    )


def _transcode_variant(input_path: Path, output_dir: Path, profile: Dict[str, Any]) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    playlist_path = output_dir / "playlist.m3u8"
    segment_pattern = output_dir / "seg_%05d.ts"
    scale_expr = f"scale=w=-2:h='min({profile['height']},ih)':force_original_aspect_ratio=decrease"
    args = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        scale_expr,
        "-c:v",
        "libx264",
        "-profile:v",
        "main",
        "-preset",
        _FFMPEG_PRESET,
        "-pix_fmt",
        "yuv420p",
        "-sc_threshold",
        "0",
        "-g",
        "48",
        "-keyint_min",
        "48",
        "-b:v",
        profile["vb"],
        "-maxrate",
        profile["maxrate"],
        "-bufsize",
        profile["bufsize"],
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        profile["ab"],
        "-hls_time",
        str(_HLS_TIME),
        "-hls_playlist_type",
        "vod",
        "-hls_flags",
        "independent_segments",
        "-hls_segment_filename",
        str(segment_pattern),
        "-f",
        "hls",
        str(playlist_path),
    ]
    _run(args)
    return {
        "name": profile["name"],
        "playlist_path": playlist_path,
        "bandwidth": int(profile["bandwidth"]),
    }


def _write_master_playlist(master_path: Path, variants: List[Dict[str, Any]]) -> None:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-INDEPENDENT-SEGMENTS", ""]
    for item in variants:
        lines.append(
            f"#EXT-X-STREAM-INF:BANDWIDTH={item['bandwidth']},RESOLUTION={item['width']}x{item['height']}"
        )
        lines.append(f"{item['name']}/playlist.m3u8")
        lines.append("")
    master_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _upload_tree(local_root: Path, remote_prefix: str) -> None:
    for path in sorted(local_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(local_root).as_posix()
        object_name = f"{remote_prefix.rstrip('/')}/{rel}"
        video_storage.upload_bytes(object_name, path.read_bytes(), content_type=video_storage.content_type_for_path(path))


def _lease_job() -> Optional[int]:
    db = get_db()
    try:
        with db.begin():
            stmt = (
                select(VideoJob)
                .where(VideoJob.kind == "transcode_hls", VideoJob.status == "pending")
                .order_by(VideoJob.id.asc())
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            job = db.execute(stmt).scalar_one_or_none()
            if job is None:
                return None
            job.status = "leased"
            job.worker_id = _WORKER_ID
            job.lease_token = secrets.token_hex(8)
            job.attempts = int(job.attempts) + 1
            job.started_at = _now()
            job.leased_until = _now() + dt.timedelta(seconds=_LEASE_SECONDS)
            job.error_text = None
            return int(job.id)
    finally:
        db.close()


def _finish_job(job_id: int, *, ok: bool, error_text: Optional[str] = None) -> None:
    db = get_db()
    try:
        with db.begin():
            job = db.get(VideoJob, job_id)
            if job is None:
                return
            if ok:
                job.status = "done"
                job.finished_at = _now()
                job.leased_until = None
                job.error_text = None
            else:
                exhausted = int(job.attempts) >= int(job.max_attempts)
                job.status = "failed" if exhausted else "pending"
                job.leased_until = None
                job.error_text = error_text
                if exhausted:
                    job.finished_at = _now()
    finally:
        db.close()


def _mark_video_failed(video_id: int, error_text: str, *, final: bool) -> None:
    db = get_db()
    try:
        with db.begin():
            video = db.get(Video, video_id)
            if video is None:
                return
            video.processing_error = error_text[:4000]
            video.status = "failed" if final else "uploaded"
    finally:
        db.close()


def _process(job_id: int) -> None:
    db = get_db()
    try:
        job = db.get(VideoJob, job_id)
        if job is None:
            return
        video = db.get(Video, int(job.video_id))
        if video is None:
            raise RuntimeError("Video no encontrado para job")
        if not video.source_object_name:
            raise RuntimeError("Video sin source_object_name")
        video.status = "processing"
        video.processing_error = None
        db.commit()
        db.refresh(video)

        _TMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"video-{video.public_id}-", dir=str(_TMP_DIR)) as tmp:
            workdir = Path(tmp)
            source_path = workdir / PurePosixPath(video.source_object_name).name
            video_storage.download_to_file(video.source_object_name, source_path)

            probe = _probe(source_path)
            streams = probe.get("streams") or []
            video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
            if not video_stream:
                raise RuntimeError("El archivo subido no contiene video válido")
            src_w = int(video_stream.get("width") or 0)
            src_h = int(video_stream.get("height") or 0)
            if src_w <= 0 or src_h <= 0:
                raise RuntimeError("No se pudo detectar resolución de video")
            duration = float((probe.get("format") or {}).get("duration") or video_stream.get("duration") or 0.0)

            poster_path = workdir / "poster.jpg"
            _generate_poster(source_path, poster_path)

            hls_root = workdir / "hls"
            variants_meta: List[Dict[str, Any]] = []
            for profile in _pick_variants(src_w, src_h):
                variant_dir = hls_root / profile["name"]
                meta = _transcode_variant(source_path, variant_dir, profile)
                out_w, out_h = _fit_dimensions(src_w, src_h, int(profile["height"]))
                meta["width"] = out_w
                meta["height"] = out_h
                variants_meta.append(meta)

            master_path = hls_root / "master.m3u8"
            _write_master_playlist(master_path, variants_meta)

            hls_prefix = f"videos/hls/{video.public_id}"
            poster_object_name = f"videos/thumbs/{video.public_id}/poster.jpg"
            _upload_tree(hls_root, hls_prefix)
            video_storage.upload_bytes(
                poster_object_name,
                poster_path.read_bytes(),
                content_type=video_storage.content_type_for_path(poster_path),
            )

            db.refresh(video)
            video.status = "ready"
            video.hls_prefix = hls_prefix
            video.master_playlist_object_name = f"{hls_prefix}/master.m3u8"
            video.poster_object_name = poster_object_name
            video.width = src_w
            video.height = src_h
            video.duration_s = duration
            video.processing_error = None
            video.published_at = _now()
            db.commit()
    finally:
        db.close()


def main() -> None:
    if not video_storage.is_configured():
        raise RuntimeError("VOS no configurado para video-worker")
    if not _DB_URL:
        raise RuntimeError("DATABASE_URL no configurada para video-worker")
    log.info("video-worker listo en %s", _WORKER_ID)
    while True:
        job_id = _lease_job()
        if job_id is None:
            time.sleep(_POLL_SECONDS)
            continue
        log.info("procesando job %s", job_id)
        db = get_db()
        try:
            job = db.get(VideoJob, job_id)
            video_id = int(job.video_id) if job else 0
            attempts = int(job.attempts) if job else 1
            max_attempts = int(job.max_attempts) if job else 1
        finally:
            db.close()

        try:
            _process(job_id)
            _finish_job(job_id, ok=True)
        except Exception as exc:
            error_text = str(exc)
            log.exception("falló job %s: %s", job_id, error_text)
            _mark_video_failed(video_id, error_text, final=attempts >= max_attempts)
            _finish_job(job_id, ok=False, error_text=error_text)
        time.sleep(1)


if __name__ == "__main__":
    main()
