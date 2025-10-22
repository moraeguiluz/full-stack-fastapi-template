from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/auth-demo", tags=["auth-demo"])

@router.post("/login")
def login(user: str, password: str):
    if user != "demo" or password != "123":
        raise HTTPException(401, "Credenciales inválidas")
    return {"access_token": "fake-token"}
