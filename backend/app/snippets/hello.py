from fastapi import APIRouter
router = APIRouter(prefix="/hello", tags=["hello"])

@router.get("")
def say_hello(name: str = "mundo"):
    return {"message": f"Hola, {name}!"}
