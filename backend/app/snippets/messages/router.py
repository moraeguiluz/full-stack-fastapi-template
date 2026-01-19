from __future__ import annotations

import os
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select, case, or_, and_, func
from sqlalchemy.orm import Session

from .db import get_db, now_utc
from .models import MessageThread, Message, MessageThreadMember, UserAuth, UserProfile
from .schemas import (
    ThreadOut,
    ThreadListOut,
    MessageOut,
    MessageListOut,
    MessageCreateIn,
    GroupCreateIn,
    GroupMembersAddIn,
)

router = APIRouter(prefix="/messages", tags=["messages"])

_SECRET = os.getenv("SECRET_KEY", "dev-change-me")
_ALG = "HS256"

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/finalize")


def _decode_uid(token: str) -> int:
    try:
        data = jwt.decode(token, _SECRET, algorithms=[_ALG])
        uid = data.get("sub")
        if not uid:
            raise HTTPException(401, "Token invalido (sin sub)")
        return int(uid)
    except jwt.PyJWTError:
        raise HTTPException(401, "Token invalido")


def _full_name(u: Optional[UserAuth]) -> str:
    if not u:
        return ""
    parts = [u.nombre or "", u.apellido_paterno or "", u.apellido_materno or ""]
    return " ".join([p.strip() for p in parts if p and p.strip()]).strip()


def _pair(user_id: int, other_id: int) -> tuple[int, int]:
    return (user_id, other_id) if user_id < other_id else (other_id, user_id)


def _ensure_member(thread: MessageThread, user_id: int, db: Session) -> None:
    if not thread.is_group:
        if user_id not in (thread.user_low_id, thread.user_high_id):
            raise HTTPException(404, "Thread no encontrado")
        return
    stmt = select(MessageThreadMember).where(
        MessageThreadMember.thread_id == thread.id, MessageThreadMember.user_id == user_id
    )
    member = db.execute(stmt).scalar_one_or_none()
    if member is None:
        raise HTTPException(404, "Thread no encontrado")


@router.get("/threads", response_model=ThreadListOut)
def list_threads(db: Session = Depends(get_db), token: str = Depends(oauth2)) -> ThreadListOut:
    user_id = _decode_uid(token)
    member = MessageThreadMember
    stmt = (
        select(MessageThread)
        .outerjoin(
            member,
            and_(member.thread_id == MessageThread.id, member.user_id == user_id),
        )
        .where(
            or_(
                and_(
                    MessageThread.is_group.is_(False),
                    or_(MessageThread.user_low_id == user_id, MessageThread.user_high_id == user_id),
                ),
                and_(MessageThread.is_group.is_(True), member.user_id.is_not(None)),
            )
        )
        .order_by(MessageThread.last_message_at.desc().nullslast(), MessageThread.updated_at.desc())
    )
    threads = db.execute(stmt).scalars().all()

    group_counts = {}
    group_ids = [t.id for t in threads if t.is_group]
    if group_ids:
        count_stmt = (
            select(MessageThreadMember.thread_id, func.count(MessageThreadMember.user_id))
            .where(MessageThreadMember.thread_id.in_(group_ids))
            .group_by(MessageThreadMember.thread_id)
        )
        for thread_id, count in db.execute(count_stmt).all():
            group_counts[int(thread_id)] = int(count)

    out = []
    for thread in threads:
        if thread.is_group:
            out.append(
                ThreadOut(
                    id=thread.id,
                    is_group=True,
                    group_name=thread.group_name,
                    member_count=group_counts.get(thread.id),
                    last_message_text=thread.last_message_text,
                    last_message_at=thread.last_message_at,
                    last_sender_id=thread.last_sender_id,
                    created_at=thread.created_at,
                    updated_at=thread.updated_at,
                )
            )
            continue

        other_id = (
            thread.user_high_id if thread.user_low_id == user_id else thread.user_low_id
        )
        user = db.get(UserAuth, other_id) if other_id is not None else None
        profile = db.get(UserProfile, other_id) if other_id is not None else None
        out.append(
            ThreadOut(
                id=thread.id,
                other_user_id=int(other_id) if other_id is not None else None,
                other_user_name=_full_name(user),
                other_user_photo_url=getattr(profile, "photo_url", None),
                other_user_photo_object_name=getattr(profile, "photo_object_name", None),
                last_message_text=thread.last_message_text,
                last_message_at=thread.last_message_at,
                last_sender_id=thread.last_sender_id,
                created_at=thread.created_at,
                updated_at=thread.updated_at,
            )
        )

    return ThreadListOut(data=out)


@router.post("/threads/with/{other_user_id}", response_model=ThreadOut)
def get_or_create_thread(
    other_user_id: int,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2),
) -> ThreadOut:
    user_id = _decode_uid(token)
    if other_user_id == user_id:
        raise HTTPException(400, "No puedes crear un chat contigo mismo")

    low_id, high_id = _pair(user_id, other_user_id)
    stmt = select(MessageThread).where(
        MessageThread.user_low_id == low_id, MessageThread.user_high_id == high_id
    )
    thread = db.execute(stmt).scalar_one_or_none()
    if thread is None:
        thread = MessageThread(user_low_id=low_id, user_high_id=high_id, updated_at=now_utc())
        db.add(thread)
        db.commit()
        db.refresh(thread)
    elif thread.is_group:
        raise HTTPException(400, "Thread invalido para mensaje directo")

    user = db.get(UserAuth, other_user_id)
    profile = db.get(UserProfile, other_user_id)

    return ThreadOut(
        id=thread.id,
        is_group=thread.is_group,
        group_name=thread.group_name,
        other_user_id=other_user_id,
        other_user_name=_full_name(user),
        other_user_photo_url=getattr(profile, "photo_url", None),
        other_user_photo_object_name=getattr(profile, "photo_object_name", None),
        last_message_text=thread.last_message_text,
        last_message_at=thread.last_message_at,
        last_sender_id=thread.last_sender_id,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


@router.get("/threads/{thread_id}/messages", response_model=MessageListOut)
def list_messages(
    thread_id: int,
    limit: int = 50,
    before_id: Optional[int] = None,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2),
) -> MessageListOut:
    user_id = _decode_uid(token)
    thread = db.get(MessageThread, thread_id)
    if not thread:
        raise HTTPException(404, "Thread no encontrado")
    _ensure_member(thread, user_id, db)

    stmt = select(Message).where(Message.thread_id == thread_id)
    if before_id is not None:
        stmt = stmt.where(Message.id < before_id)
    stmt = stmt.order_by(Message.id.desc()).limit(min(limit, 200))
    rows = db.execute(stmt).scalars().all()

    out = [
        MessageOut(
            id=m.id,
            thread_id=m.thread_id,
            sender_id=m.sender_id,
            text=m.text,
            created_at=m.created_at,
        )
        for m in rows
    ]
    return MessageListOut(data=out)


@router.post("/threads/{thread_id}/messages", response_model=MessageOut)
def send_message(
    thread_id: int,
    body: MessageCreateIn,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2),
) -> MessageOut:
    user_id = _decode_uid(token)
    thread = db.get(MessageThread, thread_id)
    if not thread:
        raise HTTPException(404, "Thread no encontrado")
    _ensure_member(thread, user_id, db)

    msg = Message(thread_id=thread.id, sender_id=user_id, text=body.text)
    thread.last_message_text = body.text[:200]
    thread.last_message_at = now_utc()
    thread.last_sender_id = user_id
    thread.updated_at = now_utc()

    db.add(msg)
    db.add(thread)
    db.commit()
    db.refresh(msg)
    return MessageOut(
        id=msg.id,
        thread_id=msg.thread_id,
        sender_id=msg.sender_id,
        text=msg.text,
        created_at=msg.created_at,
    )


@router.post("/threads/with/{other_user_id}/messages", response_model=MessageOut)
def send_message_to_user(
    other_user_id: int,
    body: MessageCreateIn,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2),
) -> MessageOut:
    user_id = _decode_uid(token)
    if other_user_id == user_id:
        raise HTTPException(400, "No puedes crear un chat contigo mismo")

    low_id, high_id = _pair(user_id, other_user_id)
    stmt = select(MessageThread).where(
        MessageThread.user_low_id == low_id, MessageThread.user_high_id == high_id
    )
    thread = db.execute(stmt).scalar_one_or_none()
    if thread is None:
        thread = MessageThread(user_low_id=low_id, user_high_id=high_id, updated_at=now_utc())
        db.add(thread)
        db.commit()
        db.refresh(thread)
    elif thread.is_group:
        raise HTTPException(400, "Thread invalido para mensaje directo")

    msg = Message(thread_id=thread.id, sender_id=user_id, text=body.text)
    thread.last_message_text = body.text[:200]
    thread.last_message_at = now_utc()
    thread.last_sender_id = user_id
    thread.updated_at = now_utc()

    db.add(msg)
    db.add(thread)
    db.commit()
    db.refresh(msg)
    return MessageOut(
        id=msg.id,
        thread_id=msg.thread_id,
        sender_id=msg.sender_id,
        text=msg.text,
        created_at=msg.created_at,
    )


@router.post("/groups", response_model=ThreadOut)
def create_group(
    body: GroupCreateIn,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2),
) -> ThreadOut:
    user_id = _decode_uid(token)
    member_ids = set(body.member_ids or [])
    member_ids.add(user_id)

    thread = MessageThread(
        is_group=True,
        group_name=body.name.strip(),
        updated_at=now_utc(),
    )
    db.add(thread)
    db.commit()
    db.refresh(thread)

    members = [
        MessageThreadMember(thread_id=thread.id, user_id=mid, role="owner" if mid == user_id else "member")
        for mid in member_ids
    ]
    db.add_all(members)
    db.commit()

    return ThreadOut(
        id=thread.id,
        is_group=True,
        group_name=thread.group_name,
        member_count=len(member_ids),
        last_message_text=thread.last_message_text,
        last_message_at=thread.last_message_at,
        last_sender_id=thread.last_sender_id,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


@router.post("/groups/{thread_id}/members", response_model=ThreadOut)
def add_group_members(
    thread_id: int,
    body: GroupMembersAddIn,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2),
) -> ThreadOut:
    user_id = _decode_uid(token)
    thread = db.get(MessageThread, thread_id)
    if not thread or not thread.is_group:
        raise HTTPException(404, "Thread no encontrado")

    member_stmt = select(MessageThreadMember).where(
        MessageThreadMember.thread_id == thread_id, MessageThreadMember.user_id == user_id
    )
    member = db.execute(member_stmt).scalar_one_or_none()
    if member is None:
        raise HTTPException(403, "No tienes permisos")

    new_ids = [mid for mid in body.member_ids if mid != user_id]
    if new_ids:
        existing_stmt = select(MessageThreadMember.user_id).where(
            MessageThreadMember.thread_id == thread_id,
            MessageThreadMember.user_id.in_(new_ids),
        )
        existing = {row[0] for row in db.execute(existing_stmt).all()}
        to_add = [mid for mid in new_ids if mid not in existing]
        if to_add:
            db.add_all(
                [MessageThreadMember(thread_id=thread_id, user_id=mid, role="member") for mid in to_add]
            )
            db.commit()

    count_stmt = select(func.count(MessageThreadMember.user_id)).where(
        MessageThreadMember.thread_id == thread_id
    )
    count = db.execute(count_stmt).scalar_one()

    return ThreadOut(
        id=thread.id,
        is_group=True,
        group_name=thread.group_name,
        member_count=int(count),
        last_message_text=thread.last_message_text,
        last_message_at=thread.last_message_at,
        last_sender_id=thread.last_sender_id,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )
