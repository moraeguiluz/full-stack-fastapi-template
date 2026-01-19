import datetime as dt
from typing import Any, List, Optional

from pydantic import BaseModel


class ProfileListItem(BaseModel):
    id: int
    name: str
    is_active: bool = True
    updated_at: Optional[dt.datetime] = None


class ProfileListOut(BaseModel):
    data: List[ProfileListItem]


class ProfileOut(BaseModel):
    id: int
    name: str
    is_active: bool = True
    data_json: Optional[Any] = None
    network_json: Optional[Any] = None
    created_at: dt.datetime
    updated_at: Optional[dt.datetime] = None


class CookiesOut(BaseModel):
    profile_id: int
    cookies_json: Any


class NetworkOut(BaseModel):
    profile_id: int
    network_json: Any
