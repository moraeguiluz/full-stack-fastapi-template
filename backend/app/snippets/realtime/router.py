import os

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from .manager import connection_manager

router = APIRouter(prefix="/ws", tags=["realtime"])

_SECRET = os.getenv("SECRET_KEY", "dev-change-me")
_ALG = "HS256"


def _decode_uid(token: str) -> int:
    try:
        data = jwt.decode(token, _SECRET, algorithms=[_ALG])
        uid = data.get("sub")
        if not uid:
            return 0
        return int(uid)
    except jwt.PyJWTError:
        return 0


@router.websocket("")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.query_params.get("token", "")
    if token.startswith("Bearer "):
        token = token[len("Bearer ") :].strip()
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    user_id = _decode_uid(token)
    if not user_id:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await connection_manager.connect(user_id, websocket)
    try:
        while True:
            msg = await websocket.receive_text()
            if msg.strip().lower() == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await connection_manager.disconnect(user_id, websocket)
