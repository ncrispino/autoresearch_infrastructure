"""Bounded bidirectional JSON RPC used only over an isolated worker's stdio."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from .parsing import _parse_json

MAX_MESSAGE_BYTES = 1_000_000


class RPCError(RuntimeError):
    pass


def encode(value: dict) -> bytes:
    raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(raw) > MAX_MESSAGE_BYTES:
        raise RPCError("Program RPC message exceeds its limit")
    return raw


def decode(raw: bytes) -> dict:
    if not raw or len(raw) > MAX_MESSAGE_BYTES or not raw.endswith(b"\n"):
        raise RPCError("Invalid or oversized program RPC frame")
    try:
        value = _parse_json(raw.decode("utf-8"), max_bytes=MAX_MESSAGE_BYTES)
    except ValueError as exc:
        raise RPCError("Malformed program RPC frame") from exc
    if not isinstance(value, dict):
        raise RPCError("Program RPC frame must be an object")
    return value


class Peer:
    def __init__(
        self,
        read: Callable[[], Awaitable[bytes]],
        write: Callable[[bytes], Awaitable[None]],
        handler: Callable[[str, dict], Awaitable[Any]],
        *,
        prefix: str,
    ):
        self.read, self.write, self.handler = read, write, handler
        self.prefix = prefix
        self.counter = 0
        self.pending: dict[str, asyncio.Future] = {}
        self.tasks: set[asyncio.Task] = set()
        self.write_lock = asyncio.Lock()
        self.closed = False
        self.received: set[str] = set()

    async def send(self, value: dict) -> None:
        data = encode(value)
        async with self.write_lock:
            if self.closed:
                raise RPCError("Program RPC is closed")
            await self.write(data)

    async def request(self, method: str, params: dict) -> Any:
        self.counter += 1
        request_id = f"{self.prefix}-{self.counter}"
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"type": "request", "id": request_id, "method": method, "params": params})
            return await future
        finally:
            self.pending.pop(request_id, None)

    async def _handle(self, value: dict) -> None:
        request_id = value["id"]
        try:
            result = await self.handler(value["method"], value["params"])
            await self.send({"type": "response", "id": request_id, "result": result})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.send({"type": "response", "id": request_id, "error": str(exc)[:2000]})

    async def run(self) -> None:
        try:
            while True:
                raw = await self.read()
                if not raw:
                    raise RPCError("Program RPC worker disconnected")
                value = decode(raw)
                kind = value.get("type")
                request_id = value.get("id")
                if not isinstance(request_id, str) or not request_id:
                    raise RPCError("Program RPC ID is missing")
                if kind == "response":
                    if set(value) not in ({"type", "id", "result"}, {"type", "id", "error"}):
                        raise RPCError("Malformed program RPC response")
                    future = self.pending.get(request_id)
                    if future is None or future.done():
                        raise RPCError("Unexpected program RPC response")
                    if "error" in value:
                        if not isinstance(value["error"], str):
                            raise RPCError("Malformed program RPC error")
                        future.set_exception(RPCError(value["error"]))
                    else:
                        future.set_result(value["result"])
                elif kind == "request":
                    if request_id in self.received or len(self.received) >= 10_000:
                        raise RPCError("Repeated or excessive program RPC request IDs")
                    self.received.add(request_id)
                    if (
                        set(value) != {"type", "id", "method", "params"}
                        or not isinstance(value["method"], str)
                        or not isinstance(value["params"], dict)
                    ):
                        raise RPCError("Malformed program RPC request")
                    if len(self.tasks) >= 64:
                        raise RPCError("Too many concurrent program RPC requests")
                    task = asyncio.create_task(self._handle(value))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
                else:
                    raise RPCError("Unknown program RPC frame type")
        except BaseException as exc:
            self.closed = True
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RPCError(str(exc)))
            raise

    async def close(self, *, timeout: float = 10, cleanup_timeout: float = 40) -> None:
        self.closed = True
        active = list(self.tasks)
        for task in active:
            task.cancel()
        try:
            if active:
                done, pending = await asyncio.wait(active, timeout=timeout)
                await asyncio.gather(*done, return_exceptions=True)
                for task in pending:
                    task.cancel()
                if pending:
                    # Native adapters need time to remove their containers after
                    # cancellation interrupts inference/evidence collection.
                    done, pending = await asyncio.wait(pending, timeout=cleanup_timeout)
                    await asyncio.gather(*done, return_exceptions=True)
                    if pending:
                        raise RPCError("Program RPC handlers did not finish cancellation")
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.cancel()
