import asyncio
from typing import Any, Dict

import anyio
from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[int, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, user_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.setdefault(user_id, set()).add(websocket)

    async def disconnect(self, user_id: int, websocket: WebSocket) -> None:
        async with self._lock:
            conns = self._connections.get(user_id)
            if not conns:
                return
            conns.discard(websocket)
            if not conns:
                self._connections.pop(user_id, None)

    async def has_user(self, user_id: int) -> bool:
        async with self._lock:
            return bool(self._connections.get(user_id))

    async def send_to_user(self, user_id: int, payload: Dict[str, Any]) -> None:
        async with self._lock:
            conns = list(self._connections.get(user_id, set()))
        if not conns:
            return
        for ws in conns:
            try:
                await ws.send_json(payload)
            except Exception:
                await self.disconnect(user_id, ws)

    def has_user_sync(self, user_id: int) -> bool:
        try:
            return anyio.from_thread.run(self.has_user, user_id)
        except RuntimeError:
            return False

    def send_to_user_sync(self, user_id: int, payload: Dict[str, Any]) -> None:
        try:
            anyio.from_thread.run(self.send_to_user, user_id, payload)
        except RuntimeError:
            return


connection_manager = ConnectionManager()
