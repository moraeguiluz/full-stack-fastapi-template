import datetime as dt
from typing import Optional

from sqlalchemy import BigInteger, String, Text, DateTime, JSON, UniqueConstraint, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import BaseOwn, BaseRO


class NotificationRealtime(BaseOwn):
    __tablename__ = "app_notification_realtime"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)

    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(String(64), default="general")
    data: Mapped[Optional[dict]] = mapped_column(JSON, default=None)

    read_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


Index("ix_notification_user_created", NotificationRealtime.user_id, NotificationRealtime.created_at.desc())


class DeviceTokenForNotifications(BaseOwn):
    __tablename__ = "app_device_token_for_notifications"
    __table_args__ = (
        UniqueConstraint("token", name="uq_device_token_for_notifications"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    token: Mapped[str] = mapped_column(Text)
    platform: Mapped[Optional[str]] = mapped_column(String(32), default=None)

    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)


Index("ix_device_token_user", DeviceTokenForNotifications.user_id)


class CodigoBase(BaseRO):
    __tablename__ = "app_codigo_base"
    __table_args__ = {"extend_existing": True}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    codigo: Mapped[str] = mapped_column(String(64))


class CodigoBaseUser(BaseRO):
    __tablename__ = "app_codigo_base_user"
    __table_args__ = {"extend_existing": True}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    codigo_base_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(String(16), default="approved")


class AppUserAuth(BaseRO):
    __tablename__ = "app_user_auth"
    __table_args__ = {"extend_existing": True}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
