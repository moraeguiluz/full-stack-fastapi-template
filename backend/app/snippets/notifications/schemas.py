import datetime as dt
from typing import Optional, Any, Dict, List

from pydantic import BaseModel, Field


class NotificationCreateIn(BaseModel):
    user_id: Optional[int] = None
    broadcast: bool = False
    codigo_base: Optional[str] = Field(default=None, max_length=64)
    delay_seconds: int = Field(default=0, ge=0, le=3600)
    force_fcm: bool = False
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=4000)
    type: str = Field(default="general", max_length=64)
    data: Optional[Dict[str, Any]] = None


class NotificationOut(BaseModel):
    id: int
    user_id: int
    title: str
    body: str
    type: str
    data: Optional[Dict[str, Any]] = None
    read_at: Optional[dt.datetime] = None
    created_at: dt.datetime


class NotificationListOut(BaseModel):
    data: List[NotificationOut]


class NotificationCreateOut(BaseModel):
    created: int
    scheduled: int = 0


class DeviceTokenRegisterIn(BaseModel):
    token: str = Field(min_length=20, max_length=4096)
    platform: Optional[str] = Field(default=None, max_length=32)


class DeviceTokenUnregisterIn(BaseModel):
    token: str = Field(min_length=20, max_length=4096)
