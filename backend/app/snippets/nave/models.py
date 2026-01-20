import datetime as dt
from typing import Optional, Any

from sqlalchemy import BigInteger, String, DateTime, Boolean, func, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import BaseOwn


class NaveProfile(BaseOwn):
    __tablename__ = "app_nave_profile"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(120), index=True)

    data_json: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    network_json: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    cookies_json: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )


class NaveUser(BaseOwn):
    __tablename__ = "app_nave_user"
    __table_args__ = (UniqueConstraint("username", name="uq_nave_user_username"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )


class NaveProject(BaseOwn):
    __tablename__ = "app_nave_project"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(120), default=None)
    folder_id: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    meta_json: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )


class NaveProvision(BaseOwn):
    __tablename__ = "app_nave_provision"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    profile_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True, nullable=True)
    vm_name: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(32), default="starting")
    timeline_json: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    result_json: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    error_json: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )


class NaveExit(BaseOwn):
    __tablename__ = "app_nave_exit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    profile_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True, nullable=True)
    vm_name: Mapped[Optional[str]] = mapped_column(String(120), index=True, nullable=True)
    address_name: Mapped[Optional[str]] = mapped_column(String(120), index=True, nullable=True)
    public_ip: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)

    agent_token: Mapped[str] = mapped_column(Text, index=True)
    desired_json: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    instance_json: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    status_json: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)

    last_seen_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )
