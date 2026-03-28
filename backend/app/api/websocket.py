"""WebSocket endpoint for real-time task status updates.

Provides a broadcast mechanism for notifying connected clients when
task/workflow status changes. Clients connect to /api/v1/ws/events
and receive JSON messages on state transitions.

Authentication is required: clients must provide a valid JWT token
as a query parameter (?token=<jwt>) or via the first message.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.core.auth.jwt import InvalidTokenError, TokenExpiredError, verify_token

logger = logging.getLogger(__name__)

router = APIRouter()


class ConnectionManager:
    """Manages active WebSocket connections and broadcasts events."""

    def __init__(self) -> None:
        self._active: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept and register a new WebSocket connection."""
        await websocket.accept()
        async with self._lock:
            self._active.append(websocket)
        logger.info("WebSocket client connected. Total: %d", len(self._active))

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection."""
        async with self._lock:
            if websocket in self._active:
                self._active.remove(websocket)
        logger.info("WebSocket client disconnected. Total: %d", len(self._active))

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Send a JSON message to all connected clients."""
        payload = json.dumps(message)
        disconnected: list[WebSocket] = []
        async with self._lock:
            for ws in self._active:
                try:
                    await ws.send_text(payload)
                except Exception:
                    disconnected.append(ws)
            for ws in disconnected:
                self._active.remove(ws)

    @property
    def active_count(self) -> int:
        return len(self._active)


# Global connection manager instance
manager = ConnectionManager()


def _authenticate_websocket(token: str | None) -> bool:
    """Validate a JWT token for WebSocket authentication.

    Returns True if the token is valid, False otherwise.
    """
    if not token:
        return False
    try:
        verify_token(token)
        return True
    except (TokenExpiredError, InvalidTokenError, Exception):
        return False


@router.websocket("/api/v1/ws/events")
async def websocket_events(
    websocket: WebSocket,
    token: str | None = Query(default=None),
) -> None:
    """WebSocket endpoint for real-time event updates.

    Requires JWT authentication via query parameter: ?token=<jwt>

    Clients connect here and receive JSON messages when tasks or
    workflows change status. Messages have the format:
        {
            "event": "task_status_changed",
            "data": {
                "task_id": "...",
                "agent_type": "eligibility",
                "status": "completed",
                ...
            }
        }

    Unauthenticated connections are rejected with close code 1008 (Policy Violation).
    """
    # Authenticate before accepting the connection
    if not _authenticate_websocket(token):
        # Reject the connection — close code 1008 = Policy Violation
        await websocket.close(code=1008, reason="Authentication required")
        return

    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive, listen for client messages (e.g. ping)
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text(json.dumps({"event": "pong"}))
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception:
        await manager.disconnect(websocket)


async def broadcast_task_update(
    task_id: str,
    agent_type: str,
    task_status: str,
    **extra: Any,
) -> None:
    """Broadcast a task status update to all connected WebSocket clients."""
    await manager.broadcast({
        "event": "task_status_changed",
        "data": {
            "task_id": task_id,
            "agent_type": agent_type,
            "status": task_status,
            **extra,
        },
    })
