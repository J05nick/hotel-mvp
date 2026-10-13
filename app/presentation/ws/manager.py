"""WebSocket connection registry with per-client back-pressure.

Each connection owns a bounded outbound queue drained by its own task, so one
slow or stalled browser can never delay delivery to the others. A client whose
queue overflows is disconnected (close code 1013 "try again later"); on
reconnect it refetches the REST snapshot.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect

from app.infrastructure.events.wire import system_message

logger = logging.getLogger(__name__)

_SEND_TIMEOUT_SECONDS: Final[float] = 10.0
CLOSE_TRY_AGAIN_LATER: Final[int] = 1013


@dataclass(eq=False)
class _Client:
    socket: WebSocket
    room_ids: frozenset[UUID] | None  # None => subscribed to every room
    queue: asyncio.Queue[str]
    overflowed: bool = field(default=False)
    pump: asyncio.Task[None] | None = field(default=None)

    def wants(self, room_id: UUID | None) -> bool:
        # System messages (room_id None) go to everybody.
        return self.room_ids is None or room_id is None or room_id in self.room_ids


class ConnectionManager:
    def __init__(self, max_queue: int) -> None:
        self._max_queue = max_queue
        self._clients: set[_Client] = set()
        self._active_sessions = 0

    @property
    def connection_count(self) -> int:
        """Clients currently receiving broadcasts."""
        return len(self._clients)

    @property
    def active_sessions(self) -> int:
        """`serve()` calls still running, including their final cleanup.

        Reaches 0 only when every handler is completely done: a graceful shutdown (or a
        test) can wait on it.
        """
        return self._active_sessions

    def broadcast(self, raw: str, room_id: UUID | None) -> None:
        """Enqueue `raw` for every interested client. Never awaits, never blocks."""
        for client in tuple(self._clients):
            if not client.wants(room_id):
                continue
            try:
                client.queue.put_nowait(raw)
            except asyncio.QueueFull:
                self._drop_slow_client(client)

    def broadcast_system(self, kind: Literal["system.connected", "system.resync"]) -> None:
        self.broadcast(system_message(kind, datetime.now(UTC)), room_id=None)

    def _drop_slow_client(self, client: _Client) -> None:
        logger.warning("ws.client_too_slow", extra={"queued": client.queue.qsize()})
        client.overflowed = True
        # The pump may be stuck inside `send_text` on a stalled TCP connection, so a
        # queued "close" marker would never be read. Cancel it; `serve()` then closes.
        if client.pump is not None:
            client.pump.cancel()

    async def serve(self, websocket: WebSocket, room_ids: frozenset[UUID] | None) -> None:
        """Run until the peer disconnects or is dropped. `websocket` is already accepted."""
        client = _Client(websocket, room_ids, asyncio.Queue(maxsize=self._max_queue))
        self._clients.add(client)
        self._active_sessions += 1
        logger.info("ws.connected", extra={"clients": len(self._clients)})
        pump = client.pump = asyncio.create_task(self._pump(client))
        drain = asyncio.create_task(self._drain(client))
        try:
            client.queue.put_nowait(system_message("system.connected", datetime.now(UTC)))
            await asyncio.wait({pump, drain}, return_when=asyncio.FIRST_COMPLETED)
            send_failed = pump.done() and not pump.cancelled() and pump.exception() is not None
            if client.overflowed or send_failed:
                # Tell the browser to reconnect (and refetch the snapshot) rather than
                # silently leaving it with a gap in the event stream.
                with contextlib.suppress(Exception):
                    await websocket.close(code=CLOSE_TRY_AGAIN_LATER)
        finally:
            # Stop broadcasting to this client first, then reap its tasks.
            self._clients.discard(client)
            try:
                for task in (pump, drain):
                    task.cancel()
                await asyncio.gather(pump, drain, return_exceptions=True)
                logger.info("ws.disconnected", extra={"clients": len(self._clients)})
            finally:
                self._active_sessions -= 1

    async def close_all(self, code: int = 1001) -> None:
        """Graceful shutdown (12-Factor IX): tell clients to reconnect elsewhere."""
        await asyncio.gather(
            *(client.socket.close(code=code) for client in tuple(self._clients)),
            return_exceptions=True,
        )

    @staticmethod
    async def _pump(client: _Client) -> None:
        """Queue -> socket."""
        while True:
            message = await client.queue.get()
            await asyncio.wait_for(client.socket.send_text(message), _SEND_TIMEOUT_SECONDS)

    @staticmethod
    async def _drain(client: _Client) -> None:
        """Consume (and ignore) inbound frames so disconnects are noticed promptly.

        The channel is server -> client only; the browser's own ping/pong frames are
        answered by the protocol layer.
        """
        try:
            while True:
                await client.socket.receive_text()
        except WebSocketDisconnect:
            return
