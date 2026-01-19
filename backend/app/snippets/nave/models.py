import datetime as dt
from typing import Optional, Any

from sqlalchemy import BigInteger, String, DateTime, Boolean, func
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
