from __future__ import annotations

import time
from typing import Any, Dict, Optional

from fastapi import HTTPException

from .gcp_client import _request

_BILLING_ACCOUNT = "01E919-7DD19C-D007C0"
_FOLDER_NAME = "navegador-ele"


def _crm_url(path: str) -> str:
    return f"https://cloudresourcemanager.googleapis.com/v3/{path}"


def _billing_url(path: str) -> str:
    return f"https://cloudbilling.googleapis.com/v1/{path}"


def _serviceusage_url(path: str) -> str:
    return f"https://serviceusage.googleapis.com/v1/{path}"


def get_project(project_id: str) -> Dict[str, Any]:
    return _request("GET", _crm_url(f"projects/{project_id}"))


def get_project_parent(project_id: str) -> Optional[str]:
    proj = get_project(project_id)
    return proj.get("parent")


def list_folders(parent: str) -> Dict[str, Any]:
    return _request("GET", _crm_url("folders"), params={"parent": parent})


def find_or_create_folder(parent: str, display_name: str) -> str:
    data = list_folders(parent)
    for folder in data.get("folders") or []:
        if folder.get("displayName") == display_name:
            return folder.get("name")
    body = {"displayName": display_name, "parent": parent}
    op = _request("POST", _crm_url("folders"), body=body)
    return _wait_folder_op(op.get("name"))


def _wait_folder_op(op_name: str, timeout_s: int = 120) -> str:
    start = time.time()
    while True:
        op = _request("GET", _crm_url(op_name))
        if op.get("done"):
            res = op.get("response") or {}
            return res.get("name")
        if time.time() - start > timeout_s:
            raise HTTPException(504, "Folder operation timeout")
        time.sleep(2)


def create_project(project_id: str, display_name: str, parent: Optional[str] = None) -> Dict[str, Any]:
    body = {"projectId": project_id, "displayName": display_name}
    if parent:
        body["parent"] = parent
    op = _request("POST", _crm_url("projects"), body=body)
    return _wait_project_op(op.get("name"))


def _wait_project_op(op_name: str, timeout_s: int = 180) -> Dict[str, Any]:
    start = time.time()
    while True:
        op = _request("GET", _crm_url(op_name))
        if op.get("done"):
            return op.get("response") or {}
        if time.time() - start > timeout_s:
            raise HTTPException(504, "Project operation timeout")
        time.sleep(3)


def set_billing(project_id: str) -> None:
    body = {"billingAccountName": f"billingAccounts/{_BILLING_ACCOUNT}", "billingEnabled": True}
    _request("PUT", _billing_url(f"projects/{project_id}/billingInfo"), body=body)


def get_iam_policy(project_id: str) -> Dict[str, Any]:
    return _request("POST", _crm_url(f"projects/{project_id}:getIamPolicy"))


def set_iam_policy(project_id: str, policy: Dict[str, Any]) -> None:
    _request("POST", _crm_url(f"projects/{project_id}:setIamPolicy"), body={"policy": policy})


def add_project_iam_member(project_id: str, member: str, role: str) -> None:
    policy = get_iam_policy(project_id)
    bindings = policy.get("bindings") or []
    for b in bindings:
        if b.get("role") == role:
            members = set(b.get("members") or [])
            if member not in members:
                members.add(member)
                b["members"] = sorted(members)
            break
    else:
        bindings.append({"role": role, "members": [member]})
    policy["bindings"] = bindings
    set_iam_policy(project_id, policy)


def enable_service(project_id: str, service: str, timeout_s: int = 300) -> None:
    start = time.time()
    backoff = 2
    while True:
        try:
            op = _request(
                "POST",
                _serviceusage_url(f"projects/{project_id}/services/{service}:enable"),
            )
            if op.get("done") is True:
                return
            name = op.get("name")
            if not name or name.endswith("DONE_OPERATION"):
                return
            while True:
                status = _request("GET", _serviceusage_url(name))
                if status.get("done"):
                    return
                if time.time() - start > timeout_s:
                    raise HTTPException(504, "Service enable timeout")
                time.sleep(2)
        except HTTPException as exc:
            msg = str(exc.detail) if hasattr(exc, "detail") else str(exc)
            if "SERVICE_DISABLED" in msg or "has not been used" in msg or "PERMISSION_DENIED" in msg:
                if time.time() - start > timeout_s:
                    raise
                time.sleep(backoff)
                backoff = min(backoff * 2, 20)
                continue
            raise


def enable_core_services(project_id: str) -> None:
    # Required for folders/projects/billing flows
    enable_service(project_id, "cloudresourcemanager.googleapis.com")
    enable_service(project_id, "cloudbilling.googleapis.com")
    enable_service(project_id, "serviceusage.googleapis.com")


def ensure_navigator_folder(project_id: str) -> Optional[str]:
    parent = get_project_parent(project_id)
    if not parent:
        return None
    return find_or_create_folder(parent, _FOLDER_NAME)
