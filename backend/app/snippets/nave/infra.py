from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field

from .gcp_client import (
    defaults,
    create_address,
    create_instance,
    get_address,
    get_instance,
)

router = APIRouter(prefix="/infra", tags=["nave-infra"])

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/finalize")


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
