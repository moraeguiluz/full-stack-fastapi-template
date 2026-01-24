from __future__ import annotations

import os
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db, now_utc, ensure_db
from .models import NotificationRealtime, CodigoBase, CodigoBaseUser, AppUserAuth
from .schemas import (
    NotificationCreateIn,
    NotificationOut,
    NotificationListOut,
    NotificationCreateOut,
)
from ..realtime.manager import connection_manager

router = APIRouter(prefix="/notifications", tags=["notifications"])

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


@router.post("", response_model=NotificationCreateOut)
def create_notification(
    body: NotificationCreateIn,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2),
) -> NotificationCreateOut:
    ensure_db()
    user_id = _decode_uid(token)
    if body.broadcast and body.codigo_base:
        raise HTTPException(400, "No puedes usar broadcast y codigo_base al mismo tiempo")

    if body.broadcast:
        stmt = select(AppUserAuth.id)
        target_ids = [row[0] for row in db.execute(stmt).all()]
    elif body.codigo_base and body.codigo_base.strip():
        codigo = body.codigo_base.strip()
        cb = db.execute(select(CodigoBase).where(CodigoBase.codigo == codigo)).scalar_one_or_none()
        if not cb:
            raise HTTPException(404, "Codigo base no encontrado")
        members_stmt = select(CodigoBaseUser.user_id).where(
            CodigoBaseUser.codigo_base_id == cb.id,
            CodigoBaseUser.is_active.is_(True),
            CodigoBaseUser.status == "approved",
        )
        target_ids = [row[0] for row in db.execute(members_stmt).all()]
    else:
        target_ids = [body.user_id or user_id]

    created = 0
    for target_user_id in target_ids:
        item = NotificationRealtime(
            user_id=int(target_user_id),
            title=body.title.strip(),
            body=body.body.strip(),
            type=body.type.strip() or "general",
            data=body.data,
        )
        db.add(item)
        db.flush()
        created += 1

        if connection_manager.has_user_sync(int(target_user_id)):
            connection_manager.send_to_user_sync(
                int(target_user_id),
                {
                    "type": "notification:new",
                    "notification_id": item.id,
                    "title": item.title,
                    "body": item.body,
                    "data": item.data,
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                },
            )

    db.commit()
    return NotificationCreateOut(created=created)


@router.get("", response_model=NotificationListOut)
def list_notifications(
    limit: int = 50,
    before_id: Optional[int] = None,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2),
) -> NotificationListOut:
    ensure_db()
    user_id = _decode_uid(token)

    stmt = select(NotificationRealtime).where(NotificationRealtime.user_id == user_id)
    if before_id is not None:
        stmt = stmt.where(NotificationRealtime.id < before_id)
    stmt = stmt.order_by(NotificationRealtime.id.desc()).limit(min(limit, 200))

    rows = db.execute(stmt).scalars().all()
    out = [
        NotificationOut(
            id=n.id,
            user_id=n.user_id,
            title=n.title,
            body=n.body,
            type=n.type,
            data=n.data,
            read_at=n.read_at,
            created_at=n.created_at,
        )
        for n in rows
    ]
    return NotificationListOut(data=out)


@router.patch("/{notification_id}/read", response_model=NotificationOut)
def mark_read(
    notification_id: int,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2),
) -> NotificationOut:
    ensure_db()
    user_id = _decode_uid(token)
    item = db.get(NotificationRealtime, notification_id)
    if not item or item.user_id != user_id:
        raise HTTPException(404, "Notificacion no encontrada")

    if item.read_at is None:
        item.read_at = now_utc()
        db.add(item)
        db.commit()
        db.refresh(item)

    return NotificationOut(
        id=item.id,
        user_id=item.user_id,
        title=item.title,
        body=item.body,
        type=item.type,
        data=item.data,
        read_at=item.read_at,
        created_at=item.created_at,
    )
