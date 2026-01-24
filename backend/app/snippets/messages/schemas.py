import datetime as dt
from typing import Optional, List

from pydantic import BaseModel, Field


class ThreadOut(BaseModel):
    id: int
    is_group: bool = False
    group_name: Optional[str] = None
    member_count: Optional[int] = None

    other_user_id: Optional[int] = None
    other_user_name: str = ""
    other_user_photo_url: Optional[str] = None
    other_user_photo_object_name: Optional[str] = None

    last_message_text: Optional[str] = None
    last_message_at: Optional[dt.datetime] = None
    last_sender_id: Optional[int] = None

    created_at: dt.datetime
    updated_at: dt.datetime


class ThreadListOut(BaseModel):
    data: List[ThreadOut]


class MessageOut(BaseModel):
    id: int
    thread_id: int
    sender_id: int
    text: str
    delivered_at: Optional[dt.datetime] = None
    read_at: Optional[dt.datetime] = None
    is_deleted: bool = False
    created_at: dt.datetime


class MessageListOut(BaseModel):
    data: List[MessageOut]


class MessageCreateIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class AdminMessageToMeIn(BaseModel):
    sender_id: int
    text: str = Field(min_length=1, max_length=4000)


class GroupCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    member_ids: List[int] = Field(default_factory=list)


class GroupMembersAddIn(BaseModel):
    member_ids: List[int] = Field(default_factory=list)


class UserListItem(BaseModel):
    id: int
    nombre_completo: str = ""
    photo_url: Optional[str] = None
    photo_object_name: Optional[str] = None


class UserListOut(BaseModel):
    data: List[UserListItem]
