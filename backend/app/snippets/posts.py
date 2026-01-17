# backend/app/snippets/posts.py
from __future__ import annotations

import os
import time
import secrets
import datetime as dt
from typing import Optional, List, Literal, Dict, Any

import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field

from sqlalchemy import (
    create_engine,
    String,
    Integer,
    BigInteger,
    DateTime,
    Boolean,
    SmallInteger,
    Text,
    UniqueConstraint,
    Index,
    func,
    and_,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column, Session

router = APIRouter(tags=["posts"])

_DB_URL = os.getenv("DATABASE_URL")
_SECRET = os.getenv("SECRET_KEY", "dev-change-me")
_ALG = "HS256"

_engine = None
_SessionLocal = None
_inited = False

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/finalize")

# -------------------- Bases --------------------
class BaseOwn(DeclarativeBase):
    """Tablas propias de este snippet."""
    pass

class BaseRO(DeclarativeBase):
    """Tablas existentes (NO se crean aquí)."""
    pass

# -------------------- Helpers --------------------
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

def _encode_crockford(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(out))

def _new_public_id() -> str:
    # 26 chars (ULID-like): 48-bit timestamp (ms) + 80-bit randomness
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    rnd = secrets.randbits(80)
    return _encode_crockford(ts, 10) + _encode_crockford(rnd, 16)

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def _init_db():
    global _engine, _SessionLocal, _inited
    if _inited or not _DB_URL:
        return

    url = _DB_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)

    _engine = create_engine(url, pool_pre_ping=True)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)

    # Crea SOLO tablas propias
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
            raise HTTPException(401, "Token inválido (sin sub)")
        return int(uid)
    except jwt.PyJWTError:
        raise HTTPException(401, "Token inválido")

# Reacciones soportadas
_REACT_MAP = {"like": 1, "love": 2, "haha": 3, "wow": 4, "sad": 5, "angry": 6}
_REACT_REV = {v: k for k, v in _REACT_MAP.items()}

def _reaction_to_int(t: str) -> int:
    if t not in _REACT_MAP:
        raise HTTPException(400, f"reaction type inválido: {t}")
    return _REACT_MAP[t]

def _reaction_to_str(v: Optional[int]) -> Optional[str]:
    if v is None:
        return None
    return _REACT_REV.get(int(v))

# -------------------- External table (read-only mapping) --------------------
class User(BaseRO):
    __tablename__ = "app_user_auth"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nombre: Mapped[Optional[str]] = mapped_column(String(120), default="")
    apellido_paterno: Mapped[Optional[str]] = mapped_column(String(120), default="")
    apellido_materno: Mapped[Optional[str]] = mapped_column(String(120), default="")
    telefono: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

def _full_name(u: Optional[User]) -> str:
    if not u:
        return ""
    parts = [u.nombre or "", u.apellido_paterno or "", u.apellido_materno or ""]
    return " ".join([p.strip() for p in parts if p and p.strip()]).strip()

# -------------------- Own tables --------------------
class Post(BaseOwn):
    __tablename__ = "app_post"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(26), unique=True, index=True)

    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    codigo_base: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)

    # 0=text, 1=media, 2=repost
    type: Mapped[int] = mapped_column(SmallInteger, default=0)

    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    repost_post_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True, nullable=True)

    # Lista JSON: [{object_name, type(image|video), mime, w,h,duration?,thumb_object_name?}]
    media_json: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)

    visibility: Mapped[int] = mapped_column(SmallInteger, default=0)  # 0=public, 1=codigo, 2=priv
    status: Mapped[int] = mapped_column(SmallInteger, default=1)      # 1=activo, 0=eliminado

    reaction_count: Mapped[int] = mapped_column(BigInteger, default=0)
    comment_count: Mapped[int] = mapped_column(BigInteger, default=0)
    repost_count: Mapped[int] = mapped_column(BigInteger, default=0)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

Index("ix_post_feed_global", Post.id.desc())
Index("ix_post_feed_codigo", Post.codigo_base, Post.id.desc())

class PostReaction(BaseOwn):
    __tablename__ = "app_post_reaction"
    __table_args__ = (UniqueConstraint("post_id", "user_id", name="uq_post_reaction"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    type: Mapped[int] = mapped_column(SmallInteger)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class Comment(BaseOwn):
    __tablename__ = "app_comment"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(26), unique=True, index=True)

    post_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)

    parent_comment_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True, nullable=True)
    text: Mapped[str] = mapped_column(Text)

    status: Mapped[int] = mapped_column(SmallInteger, default=1)  # 1 activo, 0 eliminado
    reaction_count: Mapped[int] = mapped_column(BigInteger, default=0)
    reply_count: Mapped[int] = mapped_column(BigInteger, default=0)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

Index("ix_comment_post_parent_id", Comment.post_id, Comment.parent_comment_id, Comment.id.desc())

class CommentReaction(BaseOwn):
    __tablename__ = "app_comment_reaction"
    __table_args__ = (UniqueConstraint("comment_id", "user_id", name="uq_comment_reaction"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    comment_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    type: Mapped[int] = mapped_column(SmallInteger)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

# -------------------- Schemas --------------------
class AuthorOut(BaseModel):
    id: int
    nombre_completo: str = ""
    telefono: Optional[str] = None

class MediaItem(BaseModel):
    object_name: str
    type: Literal["image", "video"]
    mime: str
    w: Optional[int] = None
    h: Optional[int] = None
    duration: Optional[float] = None
    thumb_object_name: Optional[str] = None

class CommentPreviewOut(BaseModel):
    public_id: str
    user_id: int
    author: AuthorOut
    text: str
    created_at: dt.datetime

class PostCreateIn(BaseModel):
    text: Optional[str] = None
    codigo_base: Optional[str] = None
    visibility: int = 0
    repost_public_id: Optional[str] = None
    media: Optional[List[MediaItem]] = None

class PostOut(BaseModel):
    public_id: str
    user_id: int
    author: AuthorOut

    codigo_base: Optional[str] = None
    visibility: int
    status: int
    type: int
    text: Optional[str] = None
    media: Optional[List[MediaItem]] = None

    repost: Optional[Dict[str, Any]] = None  # preview del original (ligero)

    reaction_count: int
    comment_count: int
    repost_count: int
    my_reaction: Optional[str] = None

    # NUEVO: desglose de reacciones (para UI)
    reaction_breakdown: Dict[str, int] = {}

    created_at: dt.datetime

    # NUEVO: 2 comentarios por defecto en feed
    comments_preview: List[CommentPreviewOut] = []

class FeedOut(BaseModel):
    items: List[PostOut]
    next_before_id: Optional[int] = None

class ReactIn(BaseModel):
    # null -> quitar reacción
    type: Optional[Literal["like", "love", "haha", "wow", "sad", "angry"]] = None

class ReactOut(BaseModel):
    reacted: bool
    my_reaction: Optional[str] = None
    reaction_count: int

class CommentCreateIn(BaseModel):
    text: str = Field(min_length=1, max_length=5000)

class CommentOut(BaseModel):
    public_id: str
    post_public_id: str
    user_id: int
    author: AuthorOut
    parent_comment_public_id: Optional[str] = None
    text: str
    reaction_count: int
    reply_count: int
    my_reaction: Optional[str] = None
    created_at: dt.datetime

class CommentsOut(BaseModel):
    items: List[CommentOut]
    next_before_id: Optional[int] = None

# -------------------- Builders --------------------
def _author_out(db: Session, uid: int) -> AuthorOut:
    u = db.query(User).filter(User.id == uid).first()
    return AuthorOut(id=uid, nombre_completo=_full_name(u), telefono=(u.telefono if u else None))

def _post_out(
    db: Session,
    p: Post,
    my_reaction_type: Optional[int],
    repost_preview: Optional[Dict[str, Any]],
    reaction_breakdown: Optional[Dict[str, int]] = None,
    comments_preview: Optional[List[CommentPreviewOut]] = None,
) -> PostOut:
    author = _author_out(db, int(p.user_id))

    media = None
    if isinstance(p.media_json, list):
        media = [MediaItem(**x) for x in p.media_json]  # type: ignore

    return PostOut(
        public_id=p.public_id,
        user_id=int(p.user_id),
        author=author,
        codigo_base=p.codigo_base,
        visibility=int(p.visibility),
        status=int(p.status),
        type=int(p.type),
        text=p.text,
        media=media,
        repost=repost_preview,
        reaction_count=int(p.reaction_count or 0),
        comment_count=int(p.comment_count or 0),
        repost_count=int(p.repost_count or 0),
        my_reaction=_reaction_to_str(my_reaction_type),
        reaction_breakdown=reaction_breakdown or {},
        created_at=p.created_at,
        comments_preview=comments_preview or [],
    )

# -------------------- Endpoints --------------------
@router.get("/feed", response_model=FeedOut)
def get_feed(
    codigo_base: Optional[str] = None,
    before_id: Optional[int] = None,
    limit: int = 20,
    token: str = Depends(oauth2),
    db: Session = Depends(get_db),
):
    uid = _decode_uid(token)
    limit = max(1, min(limit, 50))

    q = db.query(Post).filter(Post.status == 1)

    if codigo_base:
        q = q.filter(Post.codigo_base == codigo_base)

    if before_id:
        q = q.filter(Post.id < before_id)

    posts = q.order_by(Post.id.desc()).limit(limit).all()
    if not posts:
        return FeedOut(items=[], next_before_id=None)

    post_ids = [p.id for p in posts]

    # My reactions (batch)
    my_reacts = db.query(PostReaction).filter(
        and_(PostReaction.user_id == uid, PostReaction.post_id.in_(post_ids))
    ).all()
    myr_map = {r.post_id: r.type for r in my_reacts}

    # Repost previews (batch)
    repost_ids = [p.repost_post_id for p in posts if p.repost_post_id]
    repost_preview_map: Dict[int, Dict[str, Any]] = {}
    if repost_ids:
        originals = db.query(Post).filter(Post.id.in_(repost_ids), Post.status == 1).all()
        for o in originals:
            repost_preview_map[o.id] = {
                "public_id": o.public_id,
                "user_id": int(o.user_id),
                "author": _author_out(db, int(o.user_id)).model_dump(),
                "text": o.text,
                "media": (o.media_json if isinstance(o.media_json, list) else None),
                "created_at": o.created_at.isoformat() if o.created_at else None,
            }

    # reaction_breakdown: count por tipo para estos posts
    rrows = db.execute(
        select(PostReaction.post_id, PostReaction.type, func.count().label("cnt"))
        .where(PostReaction.post_id.in_(post_ids))
        .group_by(PostReaction.post_id, PostReaction.type)
    ).all()
    reaction_map: Dict[int, Dict[str, int]] = {pid: {} for pid in post_ids}
    for r in rrows:
        pid = int(r.post_id)
        t = _reaction_to_str(int(r.type))
        if t:
            reaction_map[pid][t] = int(r.cnt)

    # comments_preview: top 2 comments per post
    comments_preview_map: Dict[int, List[CommentPreviewOut]] = {pid: [] for pid in post_ids}

    csub = (
        select(
            Comment.public_id.label("public_id"),
            Comment.post_id.label("post_id"),
            Comment.user_id.label("user_id"),
            Comment.text.label("text"),
            Comment.created_at.label("created_at"),
            func.row_number()
                .over(partition_by=Comment.post_id, order_by=Comment.id.desc())
                .label("rn"),
        )
        .where(
            Comment.post_id.in_(post_ids),
            Comment.status == 1,
            Comment.parent_comment_id.is_(None),
        )
        .subquery()
    )

    rows = db.execute(
        select(
            csub.c.public_id,
            csub.c.post_id,
            csub.c.user_id,
            csub.c.text,
            csub.c.created_at,
        )
        .where(csub.c.rn <= 2)
        .order_by(csub.c.post_id, csub.c.created_at.desc())
    ).all()

    preview_user_ids = list({int(r.user_id) for r in rows}) if rows else []
    preview_users = db.query(User).filter(User.id.in_(preview_user_ids)).all() if preview_user_ids else []
    preview_user_map = {u.id: u for u in preview_users}

    for r in rows:
        u = preview_user_map.get(int(r.user_id))
        author = AuthorOut(
            id=int(r.user_id),
            nombre_completo=_full_name(u) if u else "",
            telefono=u.telefono if u else None,
        )
        comments_preview_map[int(r.post_id)].append(
            CommentPreviewOut(
                public_id=str(r.public_id),
                user_id=int(r.user_id),
                author=author,
                text=str(r.text),
                created_at=r.created_at,
            )
        )

    # Build response
    items: List[PostOut] = []
    for p in posts:
        preview = repost_preview_map.get(p.repost_post_id) if p.repost_post_id else None
        items.append(
            _post_out(
                db=db,
                p=p,
                my_reaction_type=myr_map.get(p.id),
                repost_preview=preview,
                reaction_breakdown=reaction_map.get(p.id, {}),
                comments_preview=comments_preview_map.get(p.id, []),
            )
        )

    return FeedOut(items=items, next_before_id=int(posts[-1].id))

@router.post("/posts", response_model=PostOut)
def create_post(body: PostCreateIn, token: str = Depends(oauth2), db: Session = Depends(get_db)):
    uid = _decode_uid(token)

    text = (body.text or "").strip()
    media = body.media or []
    repost_public_id = (body.repost_public_id or "").strip() or None

    if not text and not media and not repost_public_id:
        raise HTTPException(400, "El post requiere text, media o repost_public_id.")

    media_json = None
    if media:
        mj = []
        for it in media:
            if not it.object_name.strip():
                raise HTTPException(400, "media.object_name requerido")
            if it.type == "image" and not it.mime.startswith("image/"):
                raise HTTPException(400, f"mime inválido para image: {it.mime}")
            if it.type == "video" and not it.mime.startswith("video/"):
                raise HTTPException(400, f"mime inválido para video: {it.mime}")
            mj.append(it.model_dump())
        media_json = mj

    repost_post_id = None
    if repost_public_id:
        original = db.query(Post).filter(Post.public_id == repost_public_id, Post.status == 1).first()
        if not original:
            raise HTTPException(404, "Post original no encontrado (repost_public_id).")
        repost_post_id = original.id

    ptype = 0
    if repost_post_id:
        ptype = 2
    elif media_json:
        ptype = 1

    p = Post(
        public_id=_new_public_id(),
        user_id=uid,
        codigo_base=(body.codigo_base.strip() if body.codigo_base else None),
        visibility=int(body.visibility or 0),
        status=1,
        type=ptype,
        text=(text if text else None),
        repost_post_id=repost_post_id,
        media_json=media_json,
        updated_at=_now(),
    )

    db.add(p)
    db.commit()
    db.refresh(p)

    if repost_post_id:
        db.query(Post).filter(Post.id == repost_post_id).update({Post.repost_count: Post.repost_count + 1})
        db.commit()

    preview = None
    if repost_post_id:
        o = db.query(Post).filter(Post.id == repost_post_id, Post.status == 1).first()
        if o:
            preview = {
                "public_id": o.public_id,
                "user_id": int(o.user_id),
                "author": _author_out(db, int(o.user_id)).model_dump(),
                "text": o.text,
                "media": (o.media_json if isinstance(o.media_json, list) else None),
                "created_at": o.created_at.isoformat() if o.created_at else None,
            }

    return _post_out(db, p, None, preview, reaction_breakdown={}, comments_preview=[])

@router.get("/posts/{public_id}", response_model=PostOut)
def get_post(public_id: str, token: str = Depends(oauth2), db: Session = Depends(get_db)):
    uid = _decode_uid(token)
    p = db.query(Post).filter(Post.public_id == public_id, Post.status == 1).first()
    if not p:
        raise HTTPException(404, "Post no encontrado.")

    myr = db.query(PostReaction).filter(PostReaction.post_id == p.id, PostReaction.user_id == uid).first()
    myr_type = myr.type if myr else None

    preview = None
    if p.repost_post_id:
        o = db.query(Post).filter(Post.id == p.repost_post_id, Post.status == 1).first()
        if o:
            preview = {
                "public_id": o.public_id,
                "user_id": int(o.user_id),
                "author": _author_out(db, int(o.user_id)).model_dump(),
                "text": o.text,
                "media": (o.media_json if isinstance(o.media_json, list) else None),
                "created_at": o.created_at.isoformat() if o.created_at else None,
            }

    # reaction_breakdown for this post
    rrows = db.execute(
        select(PostReaction.type, func.count().label("cnt"))
        .where(PostReaction.post_id == p.id)
        .group_by(PostReaction.type)
    ).all()
    reaction_breakdown: Dict[str, int] = {}
    for r in rrows:
        t = _reaction_to_str(int(r.type))
        if t:
            reaction_breakdown[t] = int(r.cnt)

    return _post_out(db, p, myr_type, preview, reaction_breakdown=reaction_breakdown, comments_preview=[])

@router.patch("/posts/{public_id}", response_model=PostOut)
def edit_post(public_id: str, body: PostCreateIn, token: str = Depends(oauth2), db: Session = Depends(get_db)):
    uid = _decode_uid(token)
    p = db.query(Post).filter(Post.public_id == public_id, Post.status == 1).first()
    if not p:
        raise HTTPException(404, "Post no encontrado.")
    if int(p.user_id) != uid:
        raise HTTPException(403, "No puedes editar este post.")

    if body.text is not None:
        t = body.text.strip()
        p.text = t if t else None

    p.updated_at = _now()
    db.commit()
    db.refresh(p)

    myr = db.query(PostReaction).filter(PostReaction.post_id == p.id, PostReaction.user_id == uid).first()
    return _post_out(db, p, (myr.type if myr else None), repost_preview=None, reaction_breakdown={}, comments_preview=[])

@router.delete("/posts/{public_id}")
def delete_post(public_id: str, token: str = Depends(oauth2), db: Session = Depends(get_db)):
    uid = _decode_uid(token)
    p = db.query(Post).filter(Post.public_id == public_id, Post.status == 1).first()
    if not p:
        raise HTTPException(404, "Post no encontrado.")
    if int(p.user_id) != uid:
        raise HTTPException(403, "No puedes borrar este post.")
    p.status = 0
    p.updated_at = _now()
    db.commit()
    return {"ok": True}

@router.post("/posts/{public_id}/react", response_model=ReactOut)
def react_post(public_id: str, body: ReactIn, token: str = Depends(oauth2), db: Session = Depends(get_db)):
    uid = _decode_uid(token)
    p = db.query(Post).filter(Post.public_id == public_id, Post.status == 1).first()
    if not p:
        raise HTTPException(404, "Post no encontrado.")

    existing = db.query(PostReaction).filter(PostReaction.post_id == p.id, PostReaction.user_id == uid).first()

    if body.type is None:
        if existing:
            db.delete(existing)
            p.reaction_count = max(0, int(p.reaction_count or 0) - 1)
            p.updated_at = _now()
            db.commit()
        return ReactOut(reacted=False, my_reaction=None, reaction_count=int(p.reaction_count or 0))

    rtype = _reaction_to_int(body.type)

    if not existing:
        db.add(PostReaction(post_id=p.id, user_id=uid, type=rtype, updated_at=_now()))
        p.reaction_count = int(p.reaction_count or 0) + 1
        p.updated_at = _now()
        db.commit()
        return ReactOut(reacted=True, my_reaction=body.type, reaction_count=int(p.reaction_count or 0))

    existing.type = rtype
    existing.updated_at = _now()
    db.commit()
    return ReactOut(reacted=True, my_reaction=body.type, reaction_count=int(p.reaction_count or 0))

@router.get("/posts/{public_id}/comments", response_model=CommentsOut)
def list_comments(
    public_id: str,
    before_id: Optional[int] = None,
    limit: int = 30,
    token: str = Depends(oauth2),
    db: Session = Depends(get_db),
):
    uid = _decode_uid(token)
    limit = max(1, min(limit, 50))

    p = db.query(Post).filter(Post.public_id == public_id, Post.status == 1).first()
    if not p:
        raise HTTPException(404, "Post no encontrado.")

    q = db.query(Comment).filter(
        Comment.post_id == p.id,
        Comment.status == 1,
        Comment.parent_comment_id.is_(None),
    )
    if before_id:
        q = q.filter(Comment.id < before_id)

    items = q.order_by(Comment.id.desc()).limit(limit).all()
    if not items:
        return CommentsOut(items=[], next_before_id=None)

    cids = [c.id for c in items]
    myrs = db.query(CommentReaction).filter(
        and_(CommentReaction.user_id == uid, CommentReaction.comment_id.in_(cids))
    ).all()
    myr_map = {r.comment_id: r.type for r in myrs}

    out = []
    for c in items:
        out.append(CommentOut(
            public_id=c.public_id,
            post_public_id=p.public_id,
            user_id=int(c.user_id),
            author=_author_out(db, int(c.user_id)),
            parent_comment_public_id=None,
            text=c.text,
            reaction_count=int(c.reaction_count or 0),
            reply_count=int(c.reply_count or 0),
            my_reaction=_reaction_to_str(myr_map.get(c.id)),
            created_at=c.created_at,
        ))

    return CommentsOut(items=out, next_before_id=int(items[-1].id))

@router.post("/posts/{public_id}/comments", response_model=CommentOut)
def create_comment(public_id: str, body: CommentCreateIn, token: str = Depends(oauth2), db: Session = Depends(get_db)):
    uid = _decode_uid(token)

    p = db.query(Post).filter(Post.public_id == public_id, Post.status == 1).first()
    if not p:
        raise HTTPException(404, "Post no encontrado.")

    c = Comment(
        public_id=_new_public_id(),
        post_id=p.id,
        user_id=uid,
        parent_comment_id=None,
        text=body.text.strip(),
        status=1,
        updated_at=_now(),
    )
    db.add(c)

    p.comment_count = int(p.comment_count or 0) + 1
    p.updated_at = _now()

    db.commit()
    db.refresh(c)

    return CommentOut(
        public_id=c.public_id,
        post_public_id=p.public_id,
        user_id=uid,
        author=_author_out(db, uid),
        parent_comment_public_id=None,
        text=c.text,
        reaction_count=int(c.reaction_count or 0),
        reply_count=int(c.reply_count or 0),
        my_reaction=None,
        created_at=c.created_at,
    )

@router.get("/comments/{comment_public_id}/replies", response_model=CommentsOut)
def list_replies(
    comment_public_id: str,
    before_id: Optional[int] = None,
    limit: int = 30,
    token: str = Depends(oauth2),
    db: Session = Depends(get_db),
):
    uid = _decode_uid(token)
    limit = max(1, min(limit, 50))

    parent = db.query(Comment).filter(Comment.public_id == comment_public_id, Comment.status == 1).first()
    if not parent:
        raise HTTPException(404, "Comentario no encontrado.")

    post = db.query(Post).filter(Post.id == parent.post_id).first()
    post_public_id = post.public_id if post else ""

    q = db.query(Comment).filter(Comment.parent_comment_id == parent.id, Comment.status == 1)
    if before_id:
        q = q.filter(Comment.id < before_id)

    items = q.order_by(Comment.id.desc()).limit(limit).all()
    if not items:
        return CommentsOut(items=[], next_before_id=None)

    rids = [r.id for r in items]
    myrs = db.query(CommentReaction).filter(
        and_(CommentReaction.user_id == uid, CommentReaction.comment_id.in_(rids))
    ).all()
    myr_map = {r.comment_id: r.type for r in myrs}

    out = []
    for r in items:
        out.append(CommentOut(
            public_id=r.public_id,
            post_public_id=post_public_id,
            user_id=int(r.user_id),
            author=_author_out(db, int(r.user_id)),
            parent_comment_public_id=parent.public_id,
            text=r.text,
            reaction_count=int(r.reaction_count or 0),
            reply_count=int(r.reply_count or 0),
            my_reaction=_reaction_to_str(myr_map.get(r.id)),
            created_at=r.created_at,
        ))

    return CommentsOut(items=out, next_before_id=int(items[-1].id))

@router.post("/comments/{comment_public_id}/reply", response_model=CommentOut)
def create_reply(comment_public_id: str, body: CommentCreateIn, token: str = Depends(oauth2), db: Session = Depends(get_db)):
    uid = _decode_uid(token)

    parent = db.query(Comment).filter(Comment.public_id == comment_public_id, Comment.status == 1).first()
    if not parent:
        raise HTTPException(404, "Comentario no encontrado.")

    post = db.query(Post).filter(Post.id == parent.post_id, Post.status == 1).first()
    if not post:
        raise HTTPException(404, "Post no encontrado.")

    r = Comment(
        public_id=_new_public_id(),
        post_id=post.id,
        user_id=uid,
        parent_comment_id=parent.id,
        text=body.text.strip(),
        status=1,
        updated_at=_now(),
    )
    db.add(r)

    parent.reply_count = int(parent.reply_count or 0) + 1
    parent.updated_at = _now()

    post.comment_count = int(post.comment_count or 0) + 1
    post.updated_at = _now()

    db.commit()
    db.refresh(r)

    return CommentOut(
        public_id=r.public_id,
        post_public_id=post.public_id,
        user_id=uid,
        author=_author_out(db, uid),
        parent_comment_public_id=parent.public_id,
        text=r.text,
        reaction_count=int(r.reaction_count or 0),
        reply_count=int(r.reply_count or 0),
        my_reaction=None,
        created_at=r.created_at,
    )

@router.post("/comments/{comment_public_id}/react", response_model=ReactOut)
def react_comment(comment_public_id: str, body: ReactIn, token: str = Depends(oauth2), db: Session = Depends(get_db)):
    uid = _decode_uid(token)

    c = db.query(Comment).filter(Comment.public_id == comment_public_id, Comment.status == 1).first()
    if not c:
        raise HTTPException(404, "Comentario no encontrado.")

    existing = db.query(CommentReaction).filter(CommentReaction.comment_id == c.id, CommentReaction.user_id == uid).first()

    if body.type is None:
        if existing:
            db.delete(existing)
            c.reaction_count = max(0, int(c.reaction_count or 0) - 1)
            c.updated_at = _now()
            db.commit()
        return ReactOut(reacted=False, my_reaction=None, reaction_count=int(c.reaction_count or 0))

    rtype = _reaction_to_int(body.type)

    if not existing:
        db.add(CommentReaction(comment_id=c.id, user_id=uid, type=rtype, updated_at=_now()))
        c.reaction_count = int(c.reaction_count or 0) + 1
        c.updated_at = _now()
        db.commit()
        return ReactOut(reacted=True, my_reaction=body.type, reaction_count=int(c.reaction_count or 0))

    existing.type = rtype
    existing.updated_at = _now()
    db.commit()
    return ReactOut(reacted=True, my_reaction=body.type, reaction_count=int(c.reaction_count or 0))
