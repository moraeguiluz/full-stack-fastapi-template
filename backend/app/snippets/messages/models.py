import datetime as dt
from typing import Optional

from sqlalchemy import (
    String,
    Integer,
    BigInteger,
    DateTime,
    Text,
    UniqueConstraint,
    Index,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import BaseOwn, BaseRO


class UserAuth(BaseRO):
    __tablename__ = "app_user_auth"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nombre: Mapped[Optional[str]] = mapped_column(String(120), default="")
    apellido_paterno: Mapped[Optional[str]] = mapped_column(String(120), default="")
    apellido_materno: Mapped[Optional[str]] = mapped_column(String(120), default="")
    telefono: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)


class UserProfile(BaseRO):
    __tablename__ = "app_user_profile"
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    photo_url: Mapped[Optional[str]] = mapped_column(Text, default=None)
    photo_object_name: Mapped[Optional[str]] = mapped_column(Text, default=None)


class MessageThread(BaseOwn):
    __tablename__ = "app_message_thread"
    __table_args__ = (
        UniqueConstraint("user_low_id", "user_high_id", name="uq_message_thread_pair"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_low_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True, nullable=True)
    user_high_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True, nullable=True)
    is_group: Mapped[bool] = mapped_column(default=False)
    group_name: Mapped[Optional[str]] = mapped_column(String(120), default=None)

    last_message_text: Mapped[Optional[str]] = mapped_column(Text, default=None)
    last_message_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)
    last_sender_id: Mapped[Optional[int]] = mapped_column(BigInteger, default=None)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Message(BaseOwn):
    __tablename__ = "app_message"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(BigInteger, index=True)
    sender_id: Mapped[int] = mapped_column(BigInteger, index=True)
    text: Mapped[str] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


Index("ix_message_thread_last", MessageThread.last_message_at.desc())
Index("ix_message_thread_users", MessageThread.user_low_id, MessageThread.user_high_id)
Index("ix_message_thread_updated", MessageThread.updated_at.desc())
Index("ix_message_thread_message", Message.thread_id, Message.id.desc())


class MessageThreadMember(BaseOwn):
    __tablename__ = "app_message_thread_member"
    __table_args__ = (
        UniqueConstraint("thread_id", "user_id", name="uq_message_thread_member"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    role: Mapped[str] = mapped_column(String(32), default="member")

    joined_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_read_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)


Index("ix_message_member_thread", MessageThreadMember.thread_id, MessageThreadMember.user_id)
