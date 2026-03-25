import datetime as dt
from typing import Any, List, Optional

from pydantic import BaseModel, Field


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


class ProfileCreateIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    data_json: Optional[Any] = None
    network_json: Optional[Any] = None


class ProfileCreateOut(BaseModel):
    id: int
    name: str
    is_active: bool = True
    data_json: Optional[Any] = None
    network_json: Optional[Any] = None
    created_at: dt.datetime
    updated_at: Optional[dt.datetime] = None


class ProfileDeleteOut(BaseModel):
    ok: bool = True
    id: int


class LoginIn(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=3, max_length=128)


class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class BootstrapOut(BaseModel):
    ok: bool = True
    username: str


class CookiesOut(BaseModel):
    profile_id: int
    cookies_json: Any


class NetworkOut(BaseModel):
    profile_id: int
    network_json: Any


class AgentBootstrapIn(BaseModel):
    profile_id: Optional[int] = None
    name: Optional[str] = Field(default=None, max_length=120)


class AgentBootstrapOut(BaseModel):
    agent_id: int
    token: str


class AgentRegisterIn(BaseModel):
    vm_name: Optional[str] = Field(default=None, max_length=120)
    public_ip: Optional[str] = Field(default=None, max_length=64)


class AgentDesiredIn(BaseModel):
    wg_conf: str = Field(min_length=10)


class AgentDesiredOut(BaseModel):
    agent_id: int
    desired_json: Any


class AgentStatusIn(BaseModel):
    status_json: Any


class AgentStatusOut(BaseModel):
    ok: bool = True


class AgentStatusGetOut(BaseModel):
    agent_id: int
    vm_name: Optional[str] = None
    public_ip: Optional[str] = None
    last_seen_at: Optional[dt.datetime] = None
    status_json: Any


class ExitNodeCheckNameIn(BaseModel):
    label: str = Field(..., min_length=2, max_length=120)
    register_password: str = Field(..., min_length=1, max_length=120)


class ExitNodeCheckNameOut(BaseModel):
    available: bool
    normalized_label: str
    reason: str = ""


class ExitNodeRegisterIn(BaseModel):
    label: str = Field(..., min_length=2, max_length=120)
    register_password: str = Field(..., min_length=1, max_length=120)
    metadata: Optional[Any] = None
    capabilities: Optional[Any] = None
    wireguard: Optional[Any] = None


class ExitNodeRegisterOut(BaseModel):
    node_id: str
    node_secret: str
    label: str
    heartbeat_interval_seconds: int = 30
    wireguard: Optional[Any] = None


class ExitNodeHeartbeatIn(BaseModel):
    status: str = Field(default="online", min_length=2, max_length=32)
    observed_at: Optional[dt.datetime] = None
    metadata: Optional[Any] = None
    wireguard: Optional[Any] = None


class ExitNodeHeartbeatOut(BaseModel):
    ok: bool = True
    heartbeat_interval_seconds: int = 30


class ExitNodeDesiredIn(BaseModel):
    desired_json: Optional[Any] = None
    wg_conf: Optional[str] = Field(default=None, min_length=10)
    wireguard: Optional[Any] = None


class ExitNodeDesiredOut(BaseModel):
    node_id: str
    desired_json: Any


class ExitNodeListItem(BaseModel):
    id: str
    label: str
    public_ip: Optional[str] = None
    online: bool
    last_seen_at: Optional[dt.datetime] = None
    wireguard: Optional[Any] = None
    proxy_rule: str = ""


class ExitNodeListOut(BaseModel):
    data: List[ExitNodeListItem]


class ProvisionIn(BaseModel):
    name: str = Field(..., min_length=3, max_length=63)
    profile_id: Optional[int] = None
    address_name: Optional[str] = None
    create_ip: bool = True
    machine_type: Optional[str] = None
    disk_size_gb: Optional[int] = Field(default=None, ge=10, le=200)
    preemptible: bool = False


class ProvisionOut(BaseModel):
    agent_id: int
    token: str
    vm_name: str
    address_name: Optional[str] = None
    instance: Any
    timeline: Optional[List[Any]] = None
    network_json: Optional[Any] = None


class ProvisionStartOut(BaseModel):
    provision_id: int


class ProvisionStatusOut(BaseModel):
    provision_id: int
    status: str
    timeline: Optional[Any] = None
    result_json: Optional[Any] = None
    error_json: Optional[Any] = None


class ProjectRegisterIn(BaseModel):
    projects: List[str] = Field(default_factory=list)


class ProjectItem(BaseModel):
    project_id: str
    is_active: bool = True


class ProjectListOut(BaseModel):
    data: List[ProjectItem]


class OpsCreateIpIn(BaseModel):
    project_id: str
    name: str
    description: Optional[str] = None


class OpsCreateVmIn(BaseModel):
    project_id: str
    name: str
    address_name: Optional[str] = None
    machine_type: Optional[str] = None
    startup_script: Optional[str] = None
    disk_size_gb: Optional[int] = Field(default=None, ge=10, le=200)


class OpsStartupScriptOut(BaseModel):
    startup_script: str


class OpsSetStartupIn(BaseModel):
    project_id: str
    instance_name: str
    startup_script: str


class OpsProjectStatusItem(BaseModel):
    project_id: str
    compute_enabled: Optional[bool] = None
    quota_in_use: Optional[float] = None
    quota_limit: Optional[float] = None


class OpsProjectStatusOut(BaseModel):
    data: List[OpsProjectStatusItem]
