from __future__ import annotations

import re
import secrets
from typing import Optional, Any
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Header, BackgroundTasks
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
    set_startup_script,
)
# gcp_admin kept for future automation (org/folder scenarios)
from .gcp_admin import (
    ensure_navigator_folder,
    create_project,
    set_billing,
    enable_service,
    add_project_iam_member,
    enable_core_services,
    get_service_status,
)
from .db import get_db, now_utc, new_session
from .models import NaveExit, NaveProfile, NaveProject, NaveProvision
from .schemas import (
    AgentBootstrapIn,
    AgentBootstrapOut,
    AgentRegisterIn,
    AgentDesiredIn,
    AgentDesiredOut,
    AgentStatusIn,
    AgentStatusOut,
    AgentStatusGetOut,
    ExitNodeCheckNameIn,
    ExitNodeCheckNameOut,
    ExitNodeRegisterIn,
    ExitNodeRegisterOut,
    ExitNodeHeartbeatIn,
    ExitNodeHeartbeatOut,
    ExitNodeListItem,
    ExitNodeListOut,
    ProvisionIn,
    ProvisionOut,
    ProvisionStartOut,
    ProvisionStatusOut,
    ProjectRegisterIn,
    ProjectListOut,
    ProjectItem,
    OpsCreateIpIn,
    OpsCreateVmIn,
    OpsStartupScriptOut,
    OpsSetStartupIn,
    OpsProjectStatusItem,
    OpsProjectStatusOut,
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
_EXIT_NODE_SANITIZE_RE = re.compile(r"[^a-z0-9-]+")
_EXIT_NODE_REGISTER_PASSWORD = "admin"
_EXIT_NODE_ONLINE_WINDOW_SECONDS = 90
_EXIT_NODE_HEARTBEAT_INTERVAL_SECONDS = 30


def _ensure_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise HTTPException(400, "Nombre invalido (solo letras minusculas, numeros y '-')")


def _normalize_exit_node_label(label: str) -> str:
    value = (label or "").strip().lower()
    value = _EXIT_NODE_SANITIZE_RE.sub("-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    if value and not value[0].isalpha():
        value = f"n-{value}"
    value = value[:63].rstrip("-")
    return value


def _ensure_exit_node_password(password: str) -> None:
    if password != _EXIT_NODE_REGISTER_PASSWORD:
        raise HTTPException(401, "Clave de registro invalida")


def _find_exit_node_by_name(db: Session, node_id: str) -> Optional[NaveExit]:
    stmt = select(NaveExit).where(NaveExit.address_name == node_id)
    return db.execute(stmt).scalar_one_or_none()


def _extract_public_ip(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("public_ip", "external_ip", "ip"):
        value = payload.get(key)
        if value:
            return str(value).strip()
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in ("public_ip", "external_ip", "ip"):
            value = metadata.get(key)
            if value:
                return str(value).strip()
    return None


def _exit_node_online(last_seen_at) -> bool:
    if not last_seen_at:
        return False
    delta = now_utc() - last_seen_at
    return delta.total_seconds() <= _EXIT_NODE_ONLINE_WINDOW_SECONDS


def _exit_node_list_item(node: NaveExit) -> ExitNodeListItem:
    status_json = node.status_json if isinstance(node.status_json, dict) else {}
    instance_json = node.instance_json if isinstance(node.instance_json, dict) else {}
    desired_json = node.desired_json if isinstance(node.desired_json, dict) else {}
    wireguard = (
        status_json.get("wireguard")
        or desired_json.get("wireguard")
        or instance_json.get("wireguard")
    )
    proxy_rule = ""
    for source in (status_json, desired_json, instance_json):
        value = source.get("proxy_rule") if isinstance(source, dict) else None
        if value:
            proxy_rule = str(value).strip()
            break
    return ExitNodeListItem(
        id=str(node.address_name or node.vm_name or node.id),
        label=str(node.vm_name or node.address_name or f"exit-{node.id}"),
        public_ip=node.public_ip,
        online=_exit_node_online(node.last_seen_at),
        last_seen_at=node.last_seen_at,
        wireguard=wireguard,
        proxy_rule=proxy_rule,
    )


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


def _create_provision(db: Session, vm_name: str, profile_id: Optional[int]) -> NaveProvision:
    prov = NaveProvision(
        vm_name=vm_name,
        profile_id=profile_id,
        status="starting",
        timeline_json=[],
    )
    db.add(prov)
    db.commit()
    db.refresh(prov)
    return prov


def _update_provision(
    db: Session,
    prov: NaveProvision,
    status: Optional[str] = None,
    timeline: Optional[list[dict[str, Any]]] = None,
    result: Optional[Any] = None,
    error: Optional[Any] = None,
) -> None:
    if status:
        prov.status = status
    if timeline is not None:
        prov.timeline_json = timeline
    if result is not None:
        prov.result_json = result
    if error is not None:
        prov.error_json = error
    db.add(prov)
    db.commit()


def _project_has_quota(project_id: str) -> bool:
    quotas = get_region_quotas(project_id=project_id)
    info = quotas.get("IN_USE_ADDRESSES")
    if not info:
        return True
    usage = info.get("usage") or 0
    limit = info.get("limit") or 0
    return usage < limit


def _retry_if_compute_disabled(
    project_id: str,
    fn,
    timeline: Optional[list[dict[str, Any]]] = None,
    db: Optional[Session] = None,
    provision: Optional[NaveProvision] = None,
):
    try:
        return fn()
    except HTTPException as err:
        detail = str(err.detail)
        if "SERVICE_DISABLED" in detail or "accessNotConfigured" in detail or "compute.googleapis.com" in detail:
            if timeline is not None:
                _log_step(timeline, "enable_compute_api", {"project_id": project_id}, db=db, provision=provision)
            enable_service(project_id, "compute.googleapis.com")
            if timeline is not None:
                _log_step(timeline, "compute_api_enabled", {"project_id": project_id}, db=db, provision=provision)
            return fn()
        raise


def _log_step(
    timeline: list[dict[str, Any]],
    action: str,
    info: Optional[Any] = None,
    db: Optional[Session] = None,
    provision: Optional[NaveProvision] = None,
    status: Optional[str] = None,
) -> None:
    timeline.append({"ts": now_utc().isoformat(), "action": action, "info": info})
    if db and provision:
        _update_provision(db, provision, status=status, timeline=timeline)


def _pick_project(
    db: Session,
    timeline: list[dict[str, Any]],
    provision: Optional[NaveProvision],
) -> NaveProject:
    projects = db.execute(select(NaveProject).order_by(NaveProject.id.asc())).scalars().all()
    if not projects:
        defaults_list = [
            "bonube-cloud",
            "plan-guerrero",
            "politicomap",
            "programarmexico-285403",
            "vppn-478611",
        ]
        for pid in defaults_list:
            proj = NaveProject(project_id=pid, is_active=True)
            db.add(proj)
        db.commit()
        projects = db.execute(select(NaveProject).order_by(NaveProject.id.asc())).scalars().all()
    if not projects:
        raise HTTPException(503, "No hay proyectos registrados")
    chosen = None
    for proj in projects:
        _log_step(timeline, "check_project", {"project_id": proj.project_id}, db=db, provision=provision)
        try:
            _log_step(timeline, "enable_compute_api", {"project_id": proj.project_id}, db=db, provision=provision)
            enable_service(proj.project_id, "compute.googleapis.com")
            _log_step(timeline, "compute_api_enabled", {"project_id": proj.project_id}, db=db, provision=provision)
        except Exception as err:
            _log_step(
                timeline,
                "compute_enable_error",
                {"project_id": proj.project_id, "error": str(err)},
                db=db,
                provision=provision,
            )
        try:
            has_quota = _project_has_quota(proj.project_id)
        except HTTPException as err:
            _log_step(
                timeline,
                "quota_error",
                {"project_id": proj.project_id, "error": err.detail},
                db=db,
                provision=provision,
            )
            has_quota = False
        if has_quota:
            if not proj.is_active:
                proj.is_active = True
            if not chosen:
                chosen = proj
                _log_step(
                    timeline,
                    "project_selected",
                    {"project_id": proj.project_id},
                    db=db,
                    provision=provision,
                )
        else:
            if proj.is_active:
                proj.is_active = False
    db.commit()
    if not chosen:
        raise HTTPException(503, {"message": "No hay proyectos con cuota de IP", "timeline": timeline})
    return chosen


def _run_provision(
    db: Session,
    inp: ProvisionIn,
    provision: NaveProvision,
    timeline: list[dict[str, Any]],
    address_name: str,
) -> ProvisionOut:
    agent = _create_agent(db, inp.profile_id, inp.name)
    _log_step(timeline, "agent_created", {"agent_id": agent.id}, db=db, provision=provision)
    script = _startup_script(_API_BASE, agent.id, agent.agent_token, inp.name)

    try:
        project = _pick_project(db, timeline, provision)
    except HTTPException as err:
        detail = err.detail if isinstance(err.detail, dict) else {"message": err.detail}
        detail["timeline"] = detail.get("timeline") or timeline
        _update_provision(db, provision, status="error", timeline=timeline, error=detail)
        raise HTTPException(err.status_code, detail)

    tags = ["nave-wg"]
    try:
        _retry_if_compute_disabled(
            project.project_id,
            lambda: get_firewall("nave-wg-udp-51820", project_id=project.project_id),
            timeline=timeline,
            db=db,
            provision=provision,
        )
    except HTTPException as err:
        if err.status_code != 404:
            _log_step(timeline, "firewall_error", {"project_id": project.project_id, "error": err.detail}, db=db, provision=provision)
            _update_provision(db, provision, status="error", timeline=timeline, error=err.detail)
            raise
        _retry_if_compute_disabled(
            project.project_id,
            lambda: create_firewall_rule(
                "nave-wg-udp-51820",
                target_tags=tags,
                allowed=[{"IPProtocol": "udp", "ports": ["51820"]}],
                description="Nave WireGuard UDP 51820",
                project_id=project.project_id,
            ),
            timeline=timeline,
            db=db,
            provision=provision,
        )
    _log_step(timeline, "firewall_ready", {"project_id": project.project_id}, db=db, provision=provision)

    if inp.create_ip:
        try:
            _retry_if_compute_disabled(
                project.project_id,
                lambda: create_address(address_name, description=f"nave:{inp.name}", project_id=project.project_id),
                timeline=timeline,
                db=db,
                provision=provision,
            )
            _log_step(timeline, "address_created", {"address_name": address_name}, db=db, provision=provision)
        except HTTPException as err:
            _log_step(timeline, "address_error", {"error": err.detail}, db=db, provision=provision)
            _update_provision(db, provision, status="error", timeline=timeline, error=err.detail)
            raise

    try:
        instance = _retry_if_compute_disabled(
            project.project_id,
            lambda: create_instance(
                name=inp.name,
                address_name=address_name,
                machine_type=inp.machine_type,
                startup_script=script,
                tags=tags,
                disk_size_gb=inp.disk_size_gb,
                preemptible=inp.preemptible,
                project_id=project.project_id,
            ),
            timeline=timeline,
            db=db,
            provision=provision,
        )
        _log_step(timeline, "instance_created", {"project_id": project.project_id}, db=db, provision=provision)
    except HTTPException as err:
        _log_step(
            timeline,
            "instance_error",
            {"project_id": project.project_id, "error": err.detail},
            db=db,
            provision=provision,
        )
        _update_provision(db, provision, status="error", timeline=timeline, error=err.detail)
        raise

    public_ip = None
    try:
        nics = instance.get("networkInterfaces") or []
        if nics and nics[0].get("accessConfigs"):
            public_ip = nics[0]["accessConfigs"][0].get("natIP")
    except Exception:
        public_ip = None

    network_json = {
        "vm_name": inp.name,
        "address_name": address_name,
        "public_ip": public_ip,
        "zone": defaults().get("zone"),
        "region": defaults().get("region"),
        "project_id": project.project_id,
        "agent_id": agent.id,
        "agent_token": agent.agent_token,
        "instance_json": instance,
        "timeline": timeline,
    }

    agent.vm_name = inp.name
    agent.address_name = address_name
    agent.public_ip = public_ip
    agent.instance_json = instance
    db.add(agent)
    db.commit()

    _update_provision(db, provision, status="done", timeline=timeline, result=network_json)

    if inp.profile_id:
        profile = db.get(NaveProfile, inp.profile_id)
        if profile:
            profile.network_json = network_json
            db.add(profile)
            db.commit()

    return ProvisionOut(
        agent_id=agent.id,
        token=agent.agent_token,
        vm_name=inp.name,
        address_name=address_name,
        instance=instance,
        timeline=timeline,
        network_json=network_json,
    )

@router.post("/provision/start", response_model=ProvisionStartOut)
def provision_start(
    inp: ProvisionIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _=Depends(_auth),
) -> ProvisionStartOut:
    _ensure_name(inp.name)
    address_name = inp.address_name or f"{inp.name}-ip"
    _ensure_name(address_name)
    timeline: list[dict[str, Any]] = []
    provision = _create_provision(db, inp.name, inp.profile_id)
    _log_step(
        timeline,
        "start_provision",
        {"vm_name": inp.name, "profile_id": inp.profile_id},
        db=db,
        provision=provision,
        status="running",
    )
    background_tasks.add_task(_provision_background, provision.id, inp, address_name)
    return ProvisionStartOut(provision_id=provision.id)


@router.get("/provision/{provision_id}", response_model=ProvisionStatusOut)
def provision_status(
    provision_id: int,
    db: Session = Depends(get_db),
    _=Depends(_auth),
) -> ProvisionStatusOut:
    prov = db.get(NaveProvision, provision_id)
    if not prov:
        raise HTTPException(404, "Provision no encontrado")
    return ProvisionStatusOut(
        provision_id=prov.id,
        status=prov.status,
        timeline=prov.timeline_json or [],
        result_json=prov.result_json,
        error_json=prov.error_json,
    )



def _provision_background(provision_id: int, inp: ProvisionIn, address_name: str) -> None:
    db = new_session()
    try:
        prov = db.get(NaveProvision, provision_id)
        if not prov:
            return
        timeline = list(prov.timeline_json or [])
        _run_provision(db, inp, prov, timeline, address_name)
    except HTTPException as err:
        prov = db.get(NaveProvision, provision_id)
        if prov:
            _update_provision(db, prov, status="error", error=err.detail)
    except Exception as exc:
        prov = db.get(NaveProvision, provision_id)
        if prov:
            _update_provision(db, prov, status="error", error=str(exc))
    finally:
        db.close()



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


@router.post("/exit-nodes/check-name", response_model=ExitNodeCheckNameOut)
def check_exit_node_name(
    inp: ExitNodeCheckNameIn,
    db: Session = Depends(get_db),
) -> ExitNodeCheckNameOut:
    _ensure_exit_node_password(inp.register_password)
    normalized = _normalize_exit_node_label(inp.label)
    if len(normalized) < 2:
        return ExitNodeCheckNameOut(
            available=False,
            normalized_label=normalized,
            reason="Nombre invalido",
        )
    try:
        _ensure_name(normalized)
    except HTTPException:
        return ExitNodeCheckNameOut(
            available=False,
            normalized_label=normalized,
            reason="Nombre invalido",
        )
    existing = _find_exit_node_by_name(db, normalized)
    if existing is not None:
        return ExitNodeCheckNameOut(
            available=False,
            normalized_label=normalized,
            reason="Nombre ocupado",
        )
    return ExitNodeCheckNameOut(available=True, normalized_label=normalized)


@router.post("/exit-nodes/register", response_model=ExitNodeRegisterOut)
def register_exit_node(
    inp: ExitNodeRegisterIn,
    db: Session = Depends(get_db),
) -> ExitNodeRegisterOut:
    _ensure_exit_node_password(inp.register_password)
    normalized = _normalize_exit_node_label(inp.label)
    if len(normalized) < 2:
        raise HTTPException(400, "Nombre invalido")
    _ensure_name(normalized)
    if _find_exit_node_by_name(db, normalized) is not None:
        raise HTTPException(409, "Nombre ocupado")

    metadata_payload = inp.metadata if isinstance(inp.metadata, dict) else {}
    capabilities_payload = inp.capabilities if isinstance(inp.capabilities, dict) else {}
    wireguard_payload = inp.wireguard if isinstance(inp.wireguard, dict) else {}
    public_ip = _extract_public_ip(metadata_payload)

    node = NaveExit(
        profile_id=None,
        vm_name=normalized,
        address_name=normalized,
        public_ip=public_ip,
        agent_token=secrets.token_urlsafe(32),
        desired_json=None,
        instance_json={
            "metadata": metadata_payload,
            "capabilities": capabilities_payload,
            "wireguard": wireguard_payload,
            "registered_at": now_utc().isoformat(),
        },
        status_json={
            "status": "registered",
            "metadata": metadata_payload,
            "wireguard": wireguard_payload,
            "observed_at": now_utc().isoformat(),
        },
        last_seen_at=now_utc(),
    )
    db.add(node)
    db.commit()
    db.refresh(node)

    desired_wireguard = None
    if isinstance(node.desired_json, dict):
        desired_wireguard = node.desired_json.get("wireguard")

    return ExitNodeRegisterOut(
        node_id=normalized,
        node_secret=node.agent_token,
        label=node.vm_name or normalized,
        heartbeat_interval_seconds=_EXIT_NODE_HEARTBEAT_INTERVAL_SECONDS,
        wireguard=desired_wireguard,
    )


@router.post("/exit-nodes/{node_id}/heartbeat", response_model=ExitNodeHeartbeatOut)
def heartbeat_exit_node(
    node_id: str,
    inp: ExitNodeHeartbeatIn,
    db: Session = Depends(get_db),
    node_secret: Optional[str] = Header(default=None, alias="X-Nave-Node-Secret"),
) -> ExitNodeHeartbeatOut:
    normalized = _normalize_exit_node_label(node_id)
    node = _find_exit_node_by_name(db, normalized)
    if node is None:
        raise HTTPException(404, "Exit node no encontrado")
    if not node_secret or node_secret != node.agent_token:
        raise HTTPException(401, "node_secret invalido")

    metadata_payload = inp.metadata if isinstance(inp.metadata, dict) else {}
    wireguard_payload = inp.wireguard if isinstance(inp.wireguard, dict) else {}
    current_status = node.status_json if isinstance(node.status_json, dict) else {}

    node.public_ip = _extract_public_ip(metadata_payload) or node.public_ip
    node.last_seen_at = now_utc()
    node.status_json = {
        **current_status,
        "status": inp.status,
        "label": node.vm_name,
        "metadata": metadata_payload,
        "wireguard": wireguard_payload,
        "observed_at": (inp.observed_at or now_utc()).isoformat(),
    }
    db.add(node)
    db.commit()
    return ExitNodeHeartbeatOut(
        ok=True,
        heartbeat_interval_seconds=_EXIT_NODE_HEARTBEAT_INTERVAL_SECONDS,
    )


@router.get("/exit-nodes", response_model=ExitNodeListOut)
def list_exit_nodes(
    db: Session = Depends(get_db),
    _=Depends(_auth),
) -> ExitNodeListOut:
    stmt = select(NaveExit).order_by(
        NaveExit.last_seen_at.desc().nullslast(),
        NaveExit.id.desc(),
    )
    nodes = db.execute(stmt).scalars().all()
    return ExitNodeListOut(data=[_exit_node_list_item(node) for node in nodes])


@router.get("/exits", response_model=ExitNodeListOut)
def list_exit_nodes_alias(
    db: Session = Depends(get_db),
    _=Depends(_auth),
) -> ExitNodeListOut:
    return list_exit_nodes(db)


@router.get("/agents", response_model=ExitNodeListOut)
def list_exit_nodes_agents_alias(
    db: Session = Depends(get_db),
    _=Depends(_auth),
) -> ExitNodeListOut:
    return list_exit_nodes(db)


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

    timeline: list[dict[str, Any]] = []
    provision = _create_provision(db, inp.name, inp.profile_id)
    _log_step(
        timeline,
        "start_provision",
        {"vm_name": inp.name, "profile_id": inp.profile_id},
        db=db,
        provision=provision,
        status="running",
    )
    return _run_provision(db, inp, provision, timeline, address_name)


@router.get("/agent.py", response_class=PlainTextResponse)
def get_agent_script():
    template_path = Path(__file__).with_name("agent_template.py")
    script = template_path.read_text(encoding="utf-8")
    return script.replace("__API_BASE__", _API_BASE)


@router.post("/projects/enable-compute", response_model=ProjectListOut)
def enable_compute_projects(
    db: Session = Depends(get_db),
    _=Depends(_auth),
) -> ProjectListOut:
    projects = db.execute(select(NaveProject).order_by(NaveProject.id.asc())).scalars().all()
    out = []
    for p in projects:
        try:
            enable_service(p.project_id, "compute.googleapis.com")
        except Exception:
            pass
        out.append(ProjectItem(project_id=p.project_id, is_active=bool(p.is_active)))
    return ProjectListOut(data=out)


@router.post("/ops/create-ip")
def ops_create_ip(inp: OpsCreateIpIn, _=Depends(_auth)):
    _ensure_name(inp.name)
    return create_address(inp.name, description=inp.description, project_id=inp.project_id)


@router.post("/ops/create-vm")
def ops_create_vm(inp: OpsCreateVmIn, _=Depends(_auth)):
    _ensure_name(inp.name)
    if inp.address_name:
        _ensure_name(inp.address_name)
    zone = defaults(project_id=inp.project_id).get("zone")
    return create_instance(
        name=inp.name,
        address_name=inp.address_name,
        machine_type=inp.machine_type,
        startup_script=inp.startup_script,
        disk_size_gb=inp.disk_size_gb,
        project_id=inp.project_id,
        zone=zone,
    )


@router.post("/ops/startup-script", response_model=OpsStartupScriptOut)
def ops_startup_script(vm_name: str, agent_id: int, agent_token: str) -> OpsStartupScriptOut:
    return OpsStartupScriptOut(startup_script=_startup_script(_API_BASE, agent_id, agent_token, vm_name))


@router.post("/ops/set-startup")
def ops_set_startup(inp: OpsSetStartupIn, _=Depends(_auth)):
    zone = defaults(project_id=inp.project_id).get("zone")
    return set_startup_script(inp.project_id, zone, inp.instance_name, inp.startup_script)


@router.get("/ops/projects-status", response_model=OpsProjectStatusOut)
def ops_projects_status(db: Session = Depends(get_db), _=Depends(_auth)) -> OpsProjectStatusOut:
    projects = db.execute(select(NaveProject).order_by(NaveProject.id.asc())).scalars().all()
    out = []
    for p in projects:
        compute_enabled = None
        quota_in_use = None
        quota_limit = None
        try:
            status = get_service_status(p.project_id, "compute.googleapis.com")
            compute_enabled = status.get("state") == "ENABLED"
        except Exception:
            compute_enabled = None
        try:
            quotas = get_region_quotas(project_id=p.project_id)
            info = quotas.get("IN_USE_ADDRESSES") or {}
            quota_in_use = info.get("usage")
            quota_limit = info.get("limit")
        except Exception:
            pass
        out.append(OpsProjectStatusItem(
            project_id=p.project_id,
            compute_enabled=compute_enabled,
            quota_in_use=quota_in_use,
            quota_limit=quota_limit,
        ))
    return OpsProjectStatusOut(data=out)


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
