# backend/app/snippets/hello.py
from fastapi import APIRouter, HTTPException

ENABLED = True  # puedes poner False para apagar este snippet
router = APIRouter(prefix="/hello", tags=["hello"])

@router.get("")
def say_hello(name: str = "mundo"):
    return {"message": f"Hola, {name}!"}

@router.get("/{item_id}")
def get_item(item_id: int):
    if item_id <= 0:
        raise HTTPException(404, "No existe")
    return {"id": item_id, "ok": True}
