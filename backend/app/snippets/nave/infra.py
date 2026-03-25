from __future__ import annotations

import asyncio
import base64
import json
import re
import secrets
from typing import Optional, Any
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Header, BackgroundTasks, Query, WebSocket, WebSocketDisconnect
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
    ExitNodeDesiredIn,
    ExitNodeDesiredOut,
    ExitNodeListItem,
    ExitNodeListOut,
    ExitNodeConnectOut,
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
_EXIT_NODE_WG_SUBNET_PREFIX = "10.44"
_EXIT_NODE_WG_DEFAULT_ALLOWED_IPS = ["0.0.0.0/0", "::/0"]
_EXIT_NODE_WG_DEFAULT_DNS = ["1.1.1.1", "1.0.0.1"]
_EXIT_NODE_WG_DEFAULT_MTU = 1280
_EXIT_NODE_WG_DEFAULT_KEEPALIVE = 25

_RELAY_NODE_SOCKETS: dict[str, WebSocket] = {}
_RELAY_NODE_SEND_LOCKS: dict[str, asyncio.Lock] = {}
_RELAY_STREAM_CLIENTS: dict[str, WebSocket] = {}
_RELAY_STREAM_NODE_IDS: dict[str, str] = {}
_RELAY_OPEN_WAITERS: dict[str, asyncio.Future] = {}
_RELAY_STATE_LOCK = asyncio.Lock()


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


def _normalize_proxy_payload(value: Any, fallback_host: Optional[str] = None) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    proxy = payload.get("proxy") if isinstance(payload.get("proxy"), dict) else payload
    if not isinstance(proxy, dict):
        return {}
    host = str(proxy.get("host") or proxy.get("hostname") or fallback_host or "").strip()
    port_raw = proxy.get("port") or proxy.get("proxy_port") or 0
    try:
        port = int(port_raw)
    except Exception:
        port = 0
    scheme = str(proxy.get("type") or proxy.get("scheme") or "http").strip().lower() or "http"
    username = str(proxy.get("username") or proxy.get("user") or "").strip()
    password = str(proxy.get("password") or proxy.get("pass") or "").strip()
    auth = {"username": username, "password": password} if username else None
    proxy_rule = str(payload.get("proxy_rule") or proxy.get("proxy_rule") or "").strip()
    if not proxy_rule and host and port:
        scheme_for_rule = "socks5" if scheme.startswith("socks") else "http"
        proxy_rule = f"{scheme_for_rule}://{host}:{port}"
    if not host and not port and not proxy_rule:
        return {}
    return {
        "type": scheme,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "auth": auth,
        "proxy_rule": proxy_rule,
    }


def _build_exit_node_proxy(node: NaveExit, public_ip: Optional[str], proxy_payload: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_proxy_payload(proxy_payload, public_ip)
    if not normalized:
        return {}
    host = str(normalized.get("host") or public_ip or "").strip()
    port = normalized.get("port") if isinstance(normalized.get("port"), int) else 0
    if port <= 0:
        port = 8888
    username = str(normalized.get("username") or node.address_name or node.vm_name or f"exit-{node.id}").strip()
    password = str(normalized.get("password") or node.agent_token or "").strip()
    scheme = str(normalized.get("type") or "http").strip().lower() or "http"
    scheme_for_rule = "socks5" if scheme.startswith("socks") else "http"
    proxy_rule = f"{scheme_for_rule}://{host}:{port}" if host and port else ""
    return {
        "type": scheme,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "auth": {"username": username, "password": password} if username else None,
        "proxy_rule": proxy_rule,
    }


def _extract_exit_node_proxy(node: NaveExit) -> tuple[str, Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    status_json = node.status_json if isinstance(node.status_json, dict) else {}
    instance_json = node.instance_json if isinstance(node.instance_json, dict) else {}
    desired_json = node.desired_json if isinstance(node.desired_json, dict) else {}
    for source in (status_json, desired_json, instance_json):
        normalized = _normalize_proxy_payload(source, node.public_ip)
        if normalized:
            auth = normalized.get("auth") if isinstance(normalized.get("auth"), dict) else None
            proxy = {
                "type": normalized.get("type") or "http",
                "host": normalized.get("host") or "",
                "port": normalized.get("port") or 0,
            }
            if auth:
                proxy["username"] = auth.get("username") or ""
                proxy["password"] = auth.get("password") or ""
            return str(normalized.get("proxy_rule") or ""), proxy, auth
    return "", None, None


def _relay_node_payload(node_id: str) -> dict[str, Any]:
    return {
        "mode": "ws_reverse_connect",
        "node_id": node_id,
        "connect_path": f"/nave/infra/relay/connect/{node_id}",
        "node_path": f"/nave/infra/relay/node/{node_id}",
    }


def _relay_node_connected(node_id: str) -> bool:
    return node_id in _RELAY_NODE_SOCKETS


async def _relay_register_node_socket(node_id: str, websocket: WebSocket) -> None:
    previous = None
    async with _RELAY_STATE_LOCK:
        previous = _RELAY_NODE_SOCKETS.get(node_id)
        _RELAY_NODE_SOCKETS[node_id] = websocket
        _RELAY_NODE_SEND_LOCKS[node_id] = asyncio.Lock()
    if previous is not None and previous is not websocket:
        try:
            await previous.close(code=1012)
        except Exception:
            pass


async def _relay_unregister_node_socket(node_id: str, websocket: WebSocket) -> None:
    stale_clients: list[WebSocket] = []
    async with _RELAY_STATE_LOCK:
        current = _RELAY_NODE_SOCKETS.get(node_id)
        if current is websocket:
            _RELAY_NODE_SOCKETS.pop(node_id, None)
            _RELAY_NODE_SEND_LOCKS.pop(node_id, None)
        for stream_id, stream_node_id in list(_RELAY_STREAM_NODE_IDS.items()):
            if stream_node_id != node_id:
                continue
            client = _RELAY_STREAM_CLIENTS.pop(stream_id, None)
            _RELAY_STREAM_NODE_IDS.pop(stream_id, None)
            waiter = _RELAY_OPEN_WAITERS.pop(stream_id, None)
            if waiter is not None and not waiter.done():
                waiter.set_result({"type": "open_error", "error": "relay node disconnected"})
            if client is not None:
                stale_clients.append(client)
    for client in stale_clients:
        try:
            await client.close(code=1012)
        except Exception:
            pass


async def _relay_register_stream(stream_id: str, node_id: str, websocket: WebSocket, waiter: asyncio.Future) -> None:
    async with _RELAY_STATE_LOCK:
        _RELAY_STREAM_CLIENTS[stream_id] = websocket
        _RELAY_STREAM_NODE_IDS[stream_id] = node_id
        _RELAY_OPEN_WAITERS[stream_id] = waiter


async def _relay_pop_open_waiter(stream_id: str) -> Optional[asyncio.Future]:
    async with _RELAY_STATE_LOCK:
        waiter = _RELAY_OPEN_WAITERS.pop(stream_id, None)
    return waiter


async def _relay_get_client_socket(stream_id: str) -> Optional[WebSocket]:
    async with _RELAY_STATE_LOCK:
        client = _RELAY_STREAM_CLIENTS.get(stream_id)
    return client


async def _relay_cleanup_stream(stream_id: str) -> None:
    async with _RELAY_STATE_LOCK:
        _RELAY_STREAM_CLIENTS.pop(stream_id, None)
        _RELAY_STREAM_NODE_IDS.pop(stream_id, None)
        waiter = _RELAY_OPEN_WAITERS.pop(stream_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result({"type": "open_error", "error": "relay stream closed"})


async def _relay_send_to_node(node_id: str, payload: dict[str, Any]) -> None:
    async with _RELAY_STATE_LOCK:
        websocket = _RELAY_NODE_SOCKETS.get(node_id)
        send_lock = _RELAY_NODE_SEND_LOCKS.get(node_id)
    if websocket is None or send_lock is None:
        raise RuntimeError("relay node offline")
    async with send_lock:
        await websocket.send_text(json.dumps(payload, ensure_ascii=False))


def _exit_node_connect_out(node: NaveExit) -> ExitNodeConnectOut:
    item = _exit_node_list_item(node)
    relay = _relay_node_payload(item.id) if _relay_node_connected(item.id) else None
    transport = "relay" if relay else ("proxy" if item.proxy_rule else ("wireguard" if item.wireguard else "unknown"))
    return ExitNodeConnectOut(
        node_id=item.id,
        label=item.label,
        public_ip=item.public_ip,
        online=item.online,
        last_seen_at=item.last_seen_at,
        transport=transport,
        wireguard=item.wireguard,
        proxy_rule=item.proxy_rule,
        proxy=item.proxy,
        proxy_auth=item.proxy_auth,
        relay=relay,
    )


def _exit_node_list_item(node: NaveExit) -> ExitNodeListItem:
    status_json = node.status_json if isinstance(node.status_json, dict) else {}
    instance_json = node.instance_json if isinstance(node.instance_json, dict) else {}
    desired_json = node.desired_json if isinstance(node.desired_json, dict) else {}
    desired_wireguard = desired_json.get("wireguard") if isinstance(desired_json.get("wireguard"), dict) else {}
    instance_wireguard = instance_json.get("wireguard") if isinstance(instance_json.get("wireguard"), dict) else {}
    status_wireguard = status_json.get("wireguard") if isinstance(status_json.get("wireguard"), dict) else {}
    if desired_wireguard or instance_wireguard or status_wireguard:
        wireguard = {
            **instance_wireguard,
            **desired_wireguard,
            **status_wireguard,
        }
    else:
        wireguard = None
    proxy_rule, proxy, proxy_auth = _extract_exit_node_proxy(node)
    item_id = str(node.address_name or node.vm_name or node.id)
    return ExitNodeListItem(
        id=item_id,
        label=str(node.vm_name or node.address_name or f"exit-{node.id}"),
        public_ip=node.public_ip,
        online=_exit_node_online(node.last_seen_at) or _relay_node_connected(item_id),
        last_seen_at=node.last_seen_at,
        wireguard=wireguard,
        proxy_rule=proxy_rule,
        proxy=proxy,
        proxy_auth=proxy_auth,
    )


def _exit_node_subnet_octet(node: NaveExit) -> int:
    try:
        raw_id = int(node.id or 1)
    except Exception:
        raw_id = 1
    return ((raw_id - 1) % 250) + 1


def _build_exit_node_wireguard(node: NaveExit, public_ip: Optional[str], wireguard_payload: dict[str, Any]) -> dict[str, Any]:
    interface_name = str(wireguard_payload.get("interface") or "naveexit").strip() or "naveexit"
    listen_port_raw = wireguard_payload.get("listen_port")
    listen_port = int(listen_port_raw) if isinstance(listen_port_raw, int) or str(listen_port_raw).isdigit() else 51820
    node_public_key = str(wireguard_payload.get("public_key") or "").strip()
    egress_iface = str(wireguard_payload.get("default_egress_iface") or "").strip()
    subnet_octet = _exit_node_subnet_octet(node)
    server_address = f"{_EXIT_NODE_WG_SUBNET_PREFIX}.{subnet_octet}.1/24"
    client_address = f"{_EXIT_NODE_WG_SUBNET_PREFIX}.{subnet_octet}.2/32"
    endpoint = f"{public_ip}:{listen_port}" if public_ip else ""

    lines = [
        "[Interface]",
        "PrivateKey = __PRIVATE_KEY__",
        f"Address = {server_address}",
        f"ListenPort = {listen_port}",
    ]
    if egress_iface:
        lines.append(f"PostUp = /opt/nave-exit-node/wg-nat.sh up %i {egress_iface}")
        lines.append(f"PostDown = /opt/nave-exit-node/wg-nat.sh down %i {egress_iface}")
    wg_conf = "\n".join(lines) + "\n"

    return {
        "role": "server",
        "interface": interface_name,
        "addresses": [server_address],
        "listen_port": listen_port,
        "public_key": node_public_key,
        "endpoint": endpoint,
        "egress_iface": egress_iface,
        "client_address": client_address,
        "allowed_ips": list(_EXIT_NODE_WG_DEFAULT_ALLOWED_IPS),
        "dns": list(_EXIT_NODE_WG_DEFAULT_DNS),
        "mtu": _EXIT_NODE_WG_DEFAULT_MTU,
        "persistent_keepalive": _EXIT_NODE_WG_DEFAULT_KEEPALIVE,
        "wg_conf": wg_conf,
    }


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
    proxy_payload = inp.proxy if isinstance(inp.proxy, dict) else {}
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
            "proxy": proxy_payload,
            "registered_at": now_utc().isoformat(),
        },
        status_json={
            "status": "registered",
            "metadata": metadata_payload,
            "wireguard": wireguard_payload,
            "proxy": proxy_payload,
            "observed_at": now_utc().isoformat(),
        },
        last_seen_at=now_utc(),
    )
    db.add(node)
    db.commit()
    db.refresh(node)

    desired_wireguard = _build_exit_node_wireguard(node, public_ip, wireguard_payload)
    desired_proxy = _build_exit_node_proxy(node, public_ip, proxy_payload)
    node.desired_json = {
        "wg_conf": desired_wireguard.get("wg_conf") or "",
        "wireguard": desired_wireguard,
        "proxy": desired_proxy,
        "proxy_rule": desired_proxy.get("proxy_rule") or "",
    }
    db.add(node)
    db.commit()
    db.refresh(node)

    return ExitNodeRegisterOut(
        node_id=normalized,
        node_secret=node.agent_token,
        label=node.vm_name or normalized,
        heartbeat_interval_seconds=_EXIT_NODE_HEARTBEAT_INTERVAL_SECONDS,
        wireguard=desired_wireguard,
        proxy=desired_proxy,
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
    proxy_payload = inp.proxy if isinstance(inp.proxy, dict) else {}
    current_status = node.status_json if isinstance(node.status_json, dict) else {}
    desired_json = node.desired_json if isinstance(node.desired_json, dict) else {}

    node.public_ip = _extract_public_ip(metadata_payload) or node.public_ip
    desired_wireguard = desired_json.get("wireguard") if isinstance(desired_json.get("wireguard"), dict) else {}
    next_desired_payload = dict(desired_json)
    if node.public_ip and desired_wireguard:
        listen_port = desired_wireguard.get("listen_port")
        if not isinstance(listen_port, int):
            try:
                listen_port = int(listen_port or 51820)
            except Exception:
                listen_port = 51820
        desired_wireguard = {
            **desired_wireguard,
            "endpoint": f"{node.public_ip}:{listen_port}",
        }
        next_desired_payload["wireguard"] = desired_wireguard
    desired_proxy = _build_exit_node_proxy(node, node.public_ip, proxy_payload)
    next_desired_payload["proxy"] = desired_proxy
    next_desired_payload["proxy_rule"] = desired_proxy.get("proxy_rule") or ""
    node.desired_json = next_desired_payload
    node.last_seen_at = now_utc()
    node.status_json = {
        **current_status,
        "status": inp.status,
        "label": node.vm_name,
        "metadata": metadata_payload,
        "wireguard": wireguard_payload,
        "proxy": desired_proxy,
        "proxy_rule": desired_proxy.get("proxy_rule") or "",
        "observed_at": (inp.observed_at or now_utc()).isoformat(),
    }
    db.add(node)
    db.commit()
    return ExitNodeHeartbeatOut(
        ok=True,
        heartbeat_interval_seconds=_EXIT_NODE_HEARTBEAT_INTERVAL_SECONDS,
    )


@router.get("/exit-nodes/{node_id}/desired", response_model=ExitNodeDesiredOut)
def get_exit_node_desired(
    node_id: str,
    db: Session = Depends(get_db),
    node_secret: Optional[str] = Header(default=None, alias="X-Nave-Node-Secret"),
) -> ExitNodeDesiredOut:
    normalized = _normalize_exit_node_label(node_id)
    node = _find_exit_node_by_name(db, normalized)
    if node is None:
        raise HTTPException(404, "Exit node no encontrado")
    if not node_secret or node_secret != node.agent_token:
        raise HTTPException(401, "node_secret invalido")
    node.last_seen_at = now_utc()
    db.add(node)
    db.commit()
    return ExitNodeDesiredOut(
        node_id=normalized,
        desired_json=node.desired_json or {},
    )


@router.post("/exit-nodes/{node_id}/desired", response_model=ExitNodeDesiredOut)
def set_exit_node_desired(
    node_id: str,
    inp: ExitNodeDesiredIn,
    db: Session = Depends(get_db),
    _=Depends(_auth),
) -> ExitNodeDesiredOut:
    normalized = _normalize_exit_node_label(node_id)
    node = _find_exit_node_by_name(db, normalized)
    if node is None:
        raise HTTPException(404, "Exit node no encontrado")

    desired_payload = inp.desired_json if isinstance(inp.desired_json, dict) else {}
    if inp.wg_conf:
        desired_payload = {
            **desired_payload,
            "wg_conf": inp.wg_conf,
        }
    if isinstance(inp.wireguard, dict):
        desired_payload = {
            **desired_payload,
            "wireguard": inp.wireguard,
        }
    if isinstance(inp.proxy, dict):
        normalized_proxy = _build_exit_node_proxy(node, node.public_ip, inp.proxy)
        desired_payload = {
            **desired_payload,
            "proxy": normalized_proxy,
            "proxy_rule": normalized_proxy.get("proxy_rule") or "",
        }
    node.desired_json = desired_payload
    db.add(node)
    db.commit()
    return ExitNodeDesiredOut(
        node_id=normalized,
        desired_json=node.desired_json or {},
    )


@router.get("/exit-nodes/{node_id}/connect", response_model=ExitNodeConnectOut)
def connect_exit_node(
    node_id: str,
    db: Session = Depends(get_db),
    _=Depends(_auth),
) -> ExitNodeConnectOut:
    normalized = _normalize_exit_node_label(node_id)
    node = _find_exit_node_by_name(db, normalized)
    if node is None:
        raise HTTPException(404, "Exit node no encontrado")
    payload = _exit_node_connect_out(node)
    if not payload.online:
        raise HTTPException(409, "Exit node offline")
    if not payload.proxy_rule and not payload.wireguard and not payload.relay:
        raise HTTPException(409, "Exit node sin transporte disponible")
    return payload


@router.websocket("/relay/node/{node_id}")
async def relay_node_socket(
    websocket: WebSocket,
    node_id: str,
    secret: str = Query(default=""),
) -> None:
    normalized = _normalize_exit_node_label(node_id)
    db = new_session()
    try:
        node = _find_exit_node_by_name(db, normalized)
    finally:
        db.close()
    if node is None or not secret or secret != node.agent_token:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    await _relay_register_node_socket(normalized, websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            payload = json.loads(raw)
            msg_type = str(payload.get("type") or "").strip().lower()
            stream_id = str(payload.get("stream_id") or "").strip()
            if msg_type in {"open_ok", "open_error"}:
                waiter = await _relay_pop_open_waiter(stream_id)
                if waiter is not None and not waiter.done():
                    waiter.set_result(payload)
                continue
            if msg_type == "data":
                if not stream_id:
                    continue
                data_b64 = str(payload.get("data_b64") or "")
                if not data_b64:
                    continue
                client = await _relay_get_client_socket(stream_id)
                if client is None:
                    continue
                try:
                    await client.send_bytes(base64.b64decode(data_b64))
                except Exception:
                    await _relay_cleanup_stream(stream_id)
                continue
            if msg_type == "close":
                client = await _relay_get_client_socket(stream_id)
                await _relay_cleanup_stream(stream_id)
                if client is not None:
                    try:
                        await client.close(code=1000)
                    except Exception:
                        pass
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        await _relay_unregister_node_socket(normalized, websocket)


@router.websocket("/relay/connect/{node_id}")
async def relay_connect_socket(
    websocket: WebSocket,
    node_id: str,
    token: str = Query(default=""),
    target_host: str = Query(default=""),
    target_port: int = Query(default=0),
    tunnel_id: str = Query(default=""),
) -> None:
    normalized = _normalize_exit_node_label(node_id)
    if not token:
        await websocket.close(code=4401)
        return
    if not target_host or target_port <= 0 or target_port > 65535:
        await websocket.close(code=4400)
        return
    if not _relay_node_connected(normalized):
        await websocket.close(code=4404)
        return

    await websocket.accept()
    stream_id = tunnel_id or secrets.token_urlsafe(12)
    waiter = asyncio.get_running_loop().create_future()
    await _relay_register_stream(stream_id, normalized, websocket, waiter)
    try:
        await _relay_send_to_node(normalized, {
            "type": "open",
            "stream_id": stream_id,
            "host": target_host,
            "port": int(target_port),
        })
        opened = await asyncio.wait_for(waiter, timeout=12)
        if not isinstance(opened, dict) or str(opened.get("type") or "") != "open_ok":
            error = "relay open failed"
            if isinstance(opened, dict) and opened.get("error"):
                error = str(opened.get("error"))
            await websocket.send_text(json.dumps({"type": "error", "error": error}, ensure_ascii=False))
            await websocket.close(code=1011)
            return

        await websocket.send_text(json.dumps({"type": "ready"}, ensure_ascii=False))
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data is None:
                continue
            await _relay_send_to_node(normalized, {
                "type": "data",
                "stream_id": stream_id,
                "data_b64": base64.b64encode(data).decode("ascii"),
            })
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_text(json.dumps({"type": "error", "error": str(exc)}, ensure_ascii=False))
        except Exception:
            pass
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        try:
            await _relay_send_to_node(normalized, {"type": "close", "stream_id": stream_id})
        except Exception:
            pass
        await _relay_cleanup_stream(stream_id)


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
