from __future__ import annotations

import re
import secrets
from typing import Optional, Any
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Header
from fastapi.security import OAuth2PasswordBearer
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .gcp_client import (
    defaults,
    create_address,
    create_instance,
    get_address,
    get_instance,
    get_firewall,
    create_firewall_rule,
    get_region_quotas,
    service_account_email,
)
# gcp_admin kept for future automation (org/folder scenarios)
from .gcp_admin import (
    ensure_navigator_folder,
    create_project,
    set_billing,
    enable_service,
    add_project_iam_member,
    enable_core_services,
)
from .db import get_db, now_utc
from .models import NaveExit, NaveProfile, NaveProject
from .schemas import (
    AgentBootstrapIn,
    AgentBootstrapOut,
    AgentRegisterIn,
    AgentDesiredIn,
    AgentDesiredOut,
    AgentStatusIn,
    AgentStatusOut,
    AgentStatusGetOut,
    ProvisionIn,
    ProvisionOut,
    ProjectRegisterIn,
    ProjectListOut,
    ProjectItem,
)

router = APIRouter(prefix="/infra", tags=["nave-infra"])

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/finalize")
_API_BASE = "https://api.bonube.com/api/v1"


class AddressCreateIn(BaseModel):
    name: str = Field(..., min_length=3, max_length=63)
    description: Optional[str] = None


class InstanceCreateIn(BaseModel):
    name: str = Field(..., min_length=3, max_length=63)
    address_name: Optional[str] = None
    machine_type: Optional[str] = None
    startup_script: Optional[str] = None
    disk_size_gb: Optional[int] = Field(default=None, ge=10, le=200)
    preemptible: bool = False


_NAME_RE = re.compile(r"^[a-z]([-a-z0-9]{1,61}[a-z0-9])$")


def _ensure_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise HTTPException(400, "Nombre invalido (solo letras minusculas, numeros y '-')")


def _auth(_token: str = Depends(oauth2)) -> None:
    # Placeholder: usa el mismo auth que el resto (token requerido).
    return None


def _startup_script(api_base: str, agent_id: int, token: str, vm_name: str) -> str:
    api_base = api_base.rstrip("/")
    safe_name = vm_name.replace("\"", "")
    return f"""#!/usr/bin/env bash
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y wireguard curl python3 iptables iptables-persistent
echo "net.ipv4.ip_forward=1" >/etc/sysctl.d/99-nave.conf
sysctl -p /etc/sysctl.d/99-nave.conf
mkdir -p /opt/nave
curl -fsSL "{api_base}/nave/infra/agent.py" -o /opt/nave/agent.py
chmod +x /opt/nave/agent.py
PUBLIC_IP=$(curl -s https://ifconfig.me || true)
WAN_IF=$(ip route | awk '/default/ {{print $5; exit}}')
iptables -t nat -A POSTROUTING -o "${{WAN_IF}}" -j MASQUERADE
iptables -A FORWARD -i wg0 -j ACCEPT
iptables -A FORWARD -o wg0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
netfilter-persistent save || true
cat >/etc/systemd/system/nave-agent.service <<'SERVICE'
[Unit]
Description=Nave Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment="NAVE_API_BASE={api_base}"
Environment="NAVE_AGENT_TOKEN={token}"
Environment="NAVE_AGENT_ID={agent_id}"
Environment="NAVE_VM_NAME={safe_name}"
Environment="NAVE_PUBLIC_IP=${{PUBLIC_IP}}"
ExecStart=/usr/bin/env python3 /opt/nave/agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable --now nave-agent
"""


def _create_agent(db: Session, profile_id: Optional[int], name: Optional[str]) -> NaveExit:
    token = secrets.token_urlsafe(32)
    agent = NaveExit(
        profile_id=profile_id,
        vm_name=name,
        agent_token=token,
        desired_json=None,
        status_json=None,
        last_seen_at=None,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent


def _project_has_quota(project_id: str) -> bool:
    quotas = get_region_quotas(project_id=project_id)
    info = quotas.get("IN_USE_ADDRESSES")
    if not info:
        return True
    usage = info.get("usage") or 0
    limit = info.get("limit") or 0
    return usage < limit


def _pick_project(db: Session) -> NaveProject:
    projects = db.execute(select(NaveProject).order_by(NaveProject.id.asc())).scalars().all()
    if not projects:
        raise HTTPException(503, "No hay proyectos registrados")
    chosen = None
    for proj in projects:
        has_quota = _project_has_quota(proj.project_id)
        if has_quota:
            if not proj.is_active:
                proj.is_active = True
            if not chosen:
                chosen = proj
        else:
            if proj.is_active:
                proj.is_active = False
    db.commit()
    if not chosen:
        raise HTTPException(503, "No hay proyectos con cuota de IP")
    return chosen


def _agent_from_token(
    db: Session, agent_id: int, token: Optional[str]
) -> NaveExit:
    if not token:
        raise HTTPException(401, "Token de agente requerido")
    stmt = select(NaveExit).where(NaveExit.id == agent_id, NaveExit.agent_token == token)
    agent = db.execute(stmt).scalar_one_or_none()
    if not agent:
        raise HTTPException(401, "Agente invalido")
    return agent


@router.get("/defaults")
def get_defaults(_=Depends(_auth)):
    return defaults()


@router.get("/addresses/{name}")
def get_ip(name: str, _=Depends(_auth)):
    _ensure_name(name)
    return get_address(name)


@router.post("/addresses")
def create_ip(inp: AddressCreateIn, _=Depends(_auth)):
    _ensure_name(inp.name)
    return create_address(inp.name, description=inp.description)


@router.get("/instances/{name}")
def get_vm(name: str, _=Depends(_auth)):
    _ensure_name(name)
    return get_instance(name)


@router.post("/instances")
def create_vm(inp: InstanceCreateIn, _=Depends(_auth)):
    _ensure_name(inp.name)
    if inp.address_name:
        _ensure_name(inp.address_name)
    return create_instance(
        name=inp.name,
        address_name=inp.address_name,
        machine_type=inp.machine_type,
        startup_script=inp.startup_script,
        disk_size_gb=inp.disk_size_gb,
        preemptible=inp.preemptible,
    )


@router.post("/provision", response_model=ProvisionOut)
def provision_vm(
    inp: ProvisionIn,
    db: Session = Depends(get_db),
    _=Depends(_auth),
) -> ProvisionOut:
    _ensure_name(inp.name)
    address_name = inp.address_name or f"{inp.name}-ip"
    _ensure_name(address_name)

    agent = _create_agent(db, inp.profile_id, inp.name)
    script = _startup_script(_API_BASE, agent.id, agent.agent_token, inp.name)

    project = _pick_project(db)

    tags = ["nave-wg"]
    try:
        get_firewall("nave-wg-udp-51820", project_id=project.project_id)
    except HTTPException as err:
        if err.status_code != 404:
            raise
        create_firewall_rule(
            "nave-wg-udp-51820",
            target_tags=tags,
            allowed=[{"IPProtocol": "udp", "ports": ["51820"]}],
            description="Nave WireGuard UDP 51820",
            project_id=project.project_id,
        )

    if inp.create_ip:
        create_address(address_name, description=f"nave:{inp.name}", project_id=project.project_id)

    instance = create_instance(
        name=inp.name,
        address_name=address_name,
        machine_type=inp.machine_type,
        startup_script=script,
        tags=tags,
        disk_size_gb=inp.disk_size_gb,
        preemptible=inp.preemptible,
        project_id=project.project_id,
    )

    public_ip = None
    try:
        nics = instance.get("networkInterfaces") or []
        if nics and nics[0].get("accessConfigs"):
            public_ip = nics[0]["accessConfigs"][0].get("natIP")
    except Exception:
        public_ip = None

    agent.vm_name = inp.name
    agent.address_name = address_name
    agent.public_ip = public_ip
    agent.instance_json = instance
    db.add(agent)
    db.commit()

    if inp.profile_id:
        profile = db.get(NaveProfile, inp.profile_id)
        if profile:
            profile.network_json = {
                "vm_name": inp.name,
                "address_name": address_name,
                "public_ip": public_ip,
                "zone": defaults().get("zone"),
                "region": defaults().get("region"),
                "project_id": project.project_id,
                "agent_id": agent.id,
                "instance_json": instance,
            }
            db.add(profile)
            db.commit()

    return ProvisionOut(
        agent_id=agent.id,
        token=agent.agent_token,
        vm_name=inp.name,
        address_name=address_name,
        instance=instance,
    )


@router.get("/agent.py", response_class=PlainTextResponse)
def get_agent_script():
    template_path = Path(__file__).with_name("agent_template.py")
    script = template_path.read_text(encoding="utf-8")
    return script.replace("__API_BASE__", _API_BASE)


@router.post("/projects/register", response_model=ProjectListOut)
def register_projects(
    inp: ProjectRegisterIn,
    db: Session = Depends(get_db),
    _=Depends(_auth),
) -> ProjectListOut:
    rows = []
    for pid in inp.projects:
        pid = pid.strip()
        if not pid:
            continue
        existing = db.execute(select(NaveProject).where(NaveProject.project_id == pid)).scalar_one_or_none()
        if existing:
            rows.append(existing)
            continue
        proj = NaveProject(project_id=pid, is_active=True)
        db.add(proj)
        db.commit()
        db.refresh(proj)
        rows.append(proj)
    out = [ProjectItem(project_id=r.project_id, is_active=bool(r.is_active)) for r in rows]
    return ProjectListOut(data=out)


@router.get("/projects", response_model=ProjectListOut)
def list_projects(
    db: Session = Depends(get_db),
    _=Depends(_auth),
) -> ProjectListOut:
    projects = db.execute(select(NaveProject).order_by(NaveProject.id.asc())).scalars().all()
    out = [ProjectItem(project_id=p.project_id, is_active=bool(p.is_active)) for p in projects]
    return ProjectListOut(data=out)


@router.post("/agents/bootstrap", response_model=AgentBootstrapOut)
def create_agent_bootstrap(
    inp: AgentBootstrapIn,
    db: Session = Depends(get_db),
    _=Depends(_auth),
) -> AgentBootstrapOut:
    token = secrets.token_urlsafe(32)
    agent = NaveExit(
        profile_id=inp.profile_id,
        vm_name=inp.name,
        agent_token=token,
        desired_json=None,
        status_json=None,
        last_seen_at=None,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return AgentBootstrapOut(agent_id=agent.id, token=token)


@router.post("/agents/register")
def register_agent(
    inp: AgentRegisterIn,
    db: Session = Depends(get_db),
    token: Optional[str] = Header(default=None, alias="X-Agent-Token"),
):
    stmt = select(NaveExit).where(NaveExit.agent_token == token)
    agent = db.execute(stmt).scalar_one_or_none()
    if not agent:
        raise HTTPException(401, "Agente invalido")
    if inp.vm_name:
        agent.vm_name = inp.vm_name
    if inp.public_ip:
        agent.public_ip = inp.public_ip
    agent.last_seen_at = now_utc()
    db.add(agent)
    db.commit()
    return {"agent_id": agent.id}


@router.get("/agents/{agent_id}/desired", response_model=AgentDesiredOut)
def get_agent_desired(
    agent_id: int,
    db: Session = Depends(get_db),
    token: Optional[str] = Header(default=None, alias="X-Agent-Token"),
) -> AgentDesiredOut:
    agent = _agent_from_token(db, agent_id, token)
    agent.last_seen_at = now_utc()
    db.add(agent)
    db.commit()
    return AgentDesiredOut(agent_id=agent.id, desired_json=agent.desired_json or {})


@router.post("/agents/{agent_id}/desired", response_model=AgentDesiredOut)
def set_agent_desired(
    agent_id: int,
    inp: AgentDesiredIn,
    db: Session = Depends(get_db),
    _=Depends(_auth),
) -> AgentDesiredOut:
    agent = db.get(NaveExit, agent_id)
    if not agent:
        raise HTTPException(404, "Agente no encontrado")
    agent.desired_json = {"wg_conf": inp.wg_conf}
    db.add(agent)
    db.commit()
    return AgentDesiredOut(agent_id=agent.id, desired_json=agent.desired_json)


@router.post("/agents/{agent_id}/status", response_model=AgentStatusOut)
def set_agent_status(
    agent_id: int,
    inp: AgentStatusIn,
    db: Session = Depends(get_db),
    token: Optional[str] = Header(default=None, alias="X-Agent-Token"),
) -> AgentStatusOut:
    agent = _agent_from_token(db, agent_id, token)
    agent.status_json = inp.status_json
    agent.last_seen_at = now_utc()
    db.add(agent)
    db.commit()
    return AgentStatusOut()


@router.get("/agents/{agent_id}/status", response_model=AgentStatusGetOut)
def get_agent_status(
    agent_id: int,
    db: Session = Depends(get_db),
    token: Optional[str] = Header(default=None, alias="X-Agent-Token"),
) -> AgentStatusGetOut:
    agent = _agent_from_token(db, agent_id, token)
    return AgentStatusGetOut(
        agent_id=agent.id,
        vm_name=agent.vm_name,
        public_ip=agent.public_ip,
        last_seen_at=agent.last_seen_at,
        status_json=agent.status_json or {},
    )
