from __future__ import annotations

import os

import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import NaveProfile
from .schemas import (
    ProfileListOut,
    ProfileListItem,
    ProfileOut,
    CookiesOut,
    NetworkOut,
)

router = APIRouter(prefix="/nave", tags=["nave"])

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


def _get_profile_or_404(db: Session, profile_id: int, user_id: int) -> NaveProfile:
    stmt = select(NaveProfile).where(
        NaveProfile.id == profile_id,
        NaveProfile.user_id == user_id,
    )
    profile = db.execute(stmt).scalar_one_or_none()
    if profile is None:
        raise HTTPException(404, "Perfil no encontrado")
    return profile


@router.get("/profiles", response_model=ProfileListOut)
def list_profiles(
    db: Session = Depends(get_db),
    token: str = Depends(oauth2),
) -> ProfileListOut:
    user_id = _decode_uid(token)
    stmt = (
        select(NaveProfile)
        .where(NaveProfile.user_id == user_id)
        .order_by(NaveProfile.updated_at.desc().nullslast(), NaveProfile.id.desc())
    )
    profiles = db.execute(stmt).scalars().all()

    out = [
        ProfileListItem(
            id=p.id,
            name=p.name,
            is_active=bool(p.is_active),
            updated_at=p.updated_at,
        )
        for p in profiles
    ]
    return ProfileListOut(data=out)


@router.get("/profiles/{profile_id}", response_model=ProfileOut)
def get_profile(
    profile_id: int,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2),
) -> ProfileOut:
    user_id = _decode_uid(token)
    profile = _get_profile_or_404(db, profile_id, user_id)
    return ProfileOut(
        id=profile.id,
        name=profile.name,
        is_active=bool(profile.is_active),
        data_json=profile.data_json or {},
        network_json=profile.network_json or {},
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


@router.get("/profiles/{profile_id}/cookies", response_model=CookiesOut)
def get_profile_cookies(
    profile_id: int,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2),
) -> CookiesOut:
    user_id = _decode_uid(token)
    profile = _get_profile_or_404(db, profile_id, user_id)
    return CookiesOut(profile_id=profile.id, cookies_json=profile.cookies_json or [])


@router.get("/profiles/{profile_id}/network", response_model=NetworkOut)
def get_profile_network(
    profile_id: int,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2),
) -> NetworkOut:
    user_id = _decode_uid(token)
    profile = _get_profile_or_404(db, profile_id, user_id)
    return NetworkOut(profile_id=profile.id, network_json=profile.network_json or {})
