from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, Dict, Optional

import requests
from fastapi import HTTPException
from google.auth.transport.requests import Request
from google.oauth2 import service_account


_GCP_SA_B64 = os.getenv("GCP_SA_KEY_B64", "").strip()
_GCP_PROJECT_ID = "politicomap"
_GCP_REGION = "northamerica-south1"
_GCP_ZONE = "northamerica-south1-a"
_GCP_MACHINE = "e2-small"
_GCP_NETWORK = "global/networks/default"
_GCP_SUBNETWORK = f"regions/{_GCP_REGION}/subnetworks/default"
_GCP_INSTANCE_SA_EMAIL = ""

_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _require_config() -> None:
    if not _GCP_SA_B64:
        raise HTTPException(503, "GCP no configurado (falta GCP_SA_KEY_B64)")


def _load_sa_info() -> Dict[str, Any]:
    _require_config()
    try:
        return json.loads(base64.b64decode(_GCP_SA_B64).decode("utf-8"))
    except Exception:
        raise HTTPException(503, "GCP no configurado (GCP_SA_KEY_B64 invalido)")


def service_account_email() -> str:
    info = _load_sa_info()
    return info.get("client_email", "")


def _credentials():
    info = _load_sa_info()
    return service_account.Credentials.from_service_account_info(info, scopes=[_SCOPE])


def _access_token() -> str:
    creds = _credentials()
    creds.refresh(Request())
    if not creds.token:
        raise HTTPException(503, "No se pudo obtener token de GCP")
    return creds.token


def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {_access_token()}"}


def _request(method: str, url: str, params: Optional[Dict[str, Any]] = None,
             body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    resp = requests.request(method, url, headers=_headers(), params=params, json=body, timeout=30)
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"GCP error: {resp.text}")
    if not resp.text:
        return {}
    return resp.json()


def _region_url(project: str, region: str, path: str) -> str:
    return f"https://compute.googleapis.com/compute/v1/projects/{project}/regions/{region}/{path}"


def _zone_url(project: str, zone: str, path: str) -> str:
    return f"https://compute.googleapis.com/compute/v1/projects/{project}/zones/{zone}/{path}"


def _global_url(project: str, path: str) -> str:
    return f"https://compute.googleapis.com/compute/v1/projects/{project}/global/{path}"


def wait_region_op(project: str, region: str, op_name: str, timeout_s: int = 120) -> None:
    start = time.time()
    while True:
        op = _request("GET", _region_url(project, region, f"operations/{op_name}"))
        if op.get("status") == "DONE":
            err = op.get("error")
            if err:
                raise HTTPException(500, f"GCP operation error: {err}")
            return
        if time.time() - start > timeout_s:
            raise HTTPException(504, "GCP operation timeout")
        time.sleep(2)


def wait_zone_op(project: str, zone: str, op_name: str, timeout_s: int = 180) -> None:
    start = time.time()
    while True:
        op = _request("GET", _zone_url(project, zone, f"operations/{op_name}"))
        if op.get("status") == "DONE":
            err = op.get("error")
            if err:
                raise HTTPException(500, f"GCP operation error: {err}")
            return
        if time.time() - start > timeout_s:
            raise HTTPException(504, "GCP operation timeout")
        time.sleep(2)


def wait_global_op(project: str, op_name: str, timeout_s: int = 120) -> None:
    start = time.time()
    while True:
        op = _request("GET", _global_url(project, f"operations/{op_name}"))
        if op.get("status") == "DONE":
            err = op.get("error")
            if err:
                raise HTTPException(500, f"GCP operation error: {err}")
            return
        if time.time() - start > timeout_s:
            raise HTTPException(504, "GCP operation timeout")
        time.sleep(2)


def defaults(project_id: Optional[str] = None) -> Dict[str, str]:
    project_id = project_id or _GCP_PROJECT_ID
    return {
        "project_id": project_id,
        "region": _GCP_REGION,
        "zone": _GCP_ZONE,
        "machine_type": _GCP_MACHINE,
        "network": _GCP_NETWORK,
        "subnetwork": _GCP_SUBNETWORK,
        "instance_sa_email": _GCP_INSTANCE_SA_EMAIL,
    }


def get_address(name: str, region: Optional[str] = None, project_id: Optional[str] = None) -> Dict[str, Any]:
    cfg = defaults(project_id)
    region = region or cfg["region"]
    url = _region_url(cfg["project_id"], region, f"addresses/{name}")
    return _request("GET", url)


def create_address(name: str, region: Optional[str] = None,
                   description: Optional[str] = None, project_id: Optional[str] = None) -> Dict[str, Any]:
    cfg = defaults(project_id)
    region = region or cfg["region"]
    url = _region_url(cfg["project_id"], region, "addresses")
    body: Dict[str, Any] = {
        "name": name,
        "addressType": "EXTERNAL",
    }
    if description:
        body["description"] = description
    op = _request("POST", url, body=body)
    wait_region_op(cfg["project_id"], region, op.get("name", ""))
    return get_address(name, region, project_id=cfg["project_id"])


def get_instance(name: str, zone: Optional[str] = None, project_id: Optional[str] = None) -> Dict[str, Any]:
    cfg = defaults(project_id)
    zone = zone or cfg["zone"]
    url = _zone_url(cfg["project_id"], zone, f"instances/{name}")
    return _request("GET", url)


def create_instance(
    name: str,
    zone: Optional[str] = None,
    machine_type: Optional[str] = None,
    address_name: Optional[str] = None,
    startup_script: Optional[str] = None,
    tags: Optional[list[str]] = None,
    disk_size_gb: Optional[int] = None,
    preemptible: bool = False,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    cfg = defaults(project_id)
    zone = zone or cfg["zone"]
    machine_type = machine_type or cfg["machine_type"]

    network_interface: Dict[str, Any] = {
        "network": cfg["network"],
        "subnetwork": cfg["subnetwork"],
        "accessConfigs": [
            {
                "name": "External NAT",
                "type": "ONE_TO_ONE_NAT",
            }
        ],
    }

    if address_name:
        address = get_address(address_name, cfg["region"], project_id=cfg["project_id"]).get("address")
        if not address:
            raise HTTPException(404, "IP estatica no encontrada")
        network_interface["accessConfigs"][0]["natIP"] = address

    instance: Dict[str, Any] = {
        "name": name,
        "machineType": f"zones/{zone}/machineTypes/{machine_type}",
        "canIpForward": True,
        "networkInterfaces": [network_interface],
        "disks": [
            {
                "boot": True,
                "autoDelete": True,
                "initializeParams": {
                    "sourceImage": "projects/debian-cloud/global/images/family/debian-12",
                    "diskSizeGb": disk_size_gb or 10,
                },
            }
        ],
    }

    if tags:
        instance["tags"] = {"items": tags}

    if startup_script:
        instance["metadata"] = {
            "items": [
                {"key": "startup-script", "value": startup_script}
            ]
        }

    if preemptible:
        instance["scheduling"] = {
            "preemptible": True,
            "automaticRestart": False,
            "onHostMaintenance": "TERMINATE",
        }

    if cfg.get("instance_sa_email"):
        instance["serviceAccounts"] = [
            {
                "email": cfg["instance_sa_email"],
                "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
            }
        ]

    url = _zone_url(cfg["project_id"], zone, "instances")
    op = _request("POST", url, body=instance)
    wait_zone_op(cfg["project_id"], zone, op.get("name", ""))
    return get_instance(name, zone, project_id=cfg["project_id"])


def set_startup_script(
    project_id: str,
    zone: str,
    instance_name: str,
    startup_script: str,
) -> Dict[str, Any]:
    inst = get_instance(instance_name, zone, project_id=project_id)
    metadata = inst.get("metadata") or {}
    items = metadata.get("items") or []
    items = [i for i in items if i.get("key") != "startup-script"]
    items.append({"key": "startup-script", "value": startup_script})
    body = {"fingerprint": metadata.get("fingerprint"), "items": items}
    url = _zone_url(project_id, zone, f"instances/{instance_name}/setMetadata")
    op = _request("POST", url, body=body)
    wait_zone_op(project_id, zone, op.get("name", ""))
    return get_instance(instance_name, zone, project_id=project_id)


def get_firewall(name: str, project_id: Optional[str] = None) -> Dict[str, Any]:
    cfg = defaults(project_id)
    url = _global_url(cfg["project_id"], f"firewalls/{name}")
    return _request("GET", url)


def create_firewall_rule(
    name: str,
    network: Optional[str] = None,
    target_tags: Optional[list[str]] = None,
    allowed: Optional[list[Dict[str, Any]]] = None,
    description: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    cfg = defaults(project_id)
    url = _global_url(cfg["project_id"], "firewalls")
    body: Dict[str, Any] = {
        "name": name,
        "network": network or cfg["network"],
        "direction": "INGRESS",
        "priority": 1000,
        "allowed": allowed or [{"IPProtocol": "udp", "ports": ["51820"]}],
    }
    if target_tags:
        body["targetTags"] = target_tags
    if description:
        body["description"] = description
    op = _request("POST", url, body=body)
    wait_global_op(cfg["project_id"], op.get("name", ""))
    return get_firewall(name, project_id=cfg["project_id"])


def get_region_quotas(region: Optional[str] = None, project_id: Optional[str] = None) -> Dict[str, Any]:
    cfg = defaults(project_id)
    region = region or cfg["region"]
    url = _region_url(cfg["project_id"], region, "")
    region_data = _request("GET", url)
    quotas = region_data.get("quotas") or []
    out = {}
    for q in quotas:
        metric = q.get("metric")
        if metric:
            out[metric] = {"limit": q.get("limit"), "usage": q.get("usage")}
    return out
