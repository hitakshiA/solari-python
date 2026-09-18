"""``ControlChannel`` — the shared control-WebSocket transport used by both
:class:`~solari_desktop.sandbox.Sandbox` and
:class:`~solari_desktop.desktop.Desktop`. Mirrors ``sdk/src/transport.ts``.

THREE kinds of frame travel newline-delimited on the same socket and are
dispatched by SHAPE, not by assuming one-reply-per-request:

1. JSON-RPC reply       ``{id, ok, result?|error?}``  — resolves the call ``id``.
2. v1 streamed exec      ``{id, stream:"stdout"|"stderr", data}`` — routed to the
   per-``id`` stream handler; the terminal frame is a normal reply (1).
3. v2 async STREAM frame ``{type:"cmd.data"|"cmd.exit"|"pty.data"|"fs.event",
   cmdId|ptyId|watchId, ...}`` — NOT correlated to a request id; dispatched by
   ``type`` to a handler keyed on the stream's own id.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, Optional

from .errors import (
    ActionError,
    ConnectionError as SolariConnectionError,
    SolariError,
    TimeoutError as SolariTimeoutError,
)
from .ws import WsCallbacks, WsLike, open_websocket

DEFAULT_CALL_TIMEOUT_MS = 300_000
CONNECT_TIMEOUT_MS = 15_000
# A freshly created/restored session's control WS can transiently fail its
# upstream handshake: right after a snapshot restore the guest accepts only ONE
# vsock control connection at a time for a brief window, so the host can answer
# a concurrent /control upgrade with a 502 ``guest_unreachable`` while it's busy
# (e.g. tearing down sibling sessions). The VM is live and billable — a blip
# like that must not be fatal. Retry a fast-failing connect a couple of times
# with short backoff; the next attempt lands on a now-ready guest. We do NOT
# retry a full connect TIMEOUT (that's a genuinely hung dial, not a transient).
# Mirrors ``sdk/packages/core/src/transport.ts``.
CONNECT_MAX_ATTEMPTS = 3
CONNECT_RETRY_BASE_MS = 150

#: A v1 streamed-exec chunk handler: ``(stream, data)``.
RpcStreamHandler = Callable[[str, Optional[str]], None]
#: A v2 async-frame handler: ``(frame_dict)``.
AsyncFrameHandler = Callable[[Dict[str, Any]], None]


class _Pending:
    __slots__ = ("future", "method")

    def __init__(self, future: "asyncio.Future[Any]", method: str) -> None:
        self.future = future
        self.method = method


class ControlChannel:
    """The shared control-WS transport. One per live session handle."""

    def __init__(
        self,
        control_url: str,
        *,
        call_timeout_ms: Optional[int] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self._control_url = control_url
        self._call_timeout_ms = call_timeout_ms or DEFAULT_CALL_TIMEOUT_MS
        self._headers: Dict[str, str] = headers or {}
        self._ws: Optional[WsLike] = None
        self._connecting: Optional["asyncio.Future[None]"] = None
        self._rx_buffer = ""
        self._next_id = 1
        self._pending: Dict[str, _Pending] = {}
        self._stream_handlers: Dict[str, RpcStreamHandler] = {}
        #: ``type -> {stream_id -> handler}``.
        self._frame_handlers: Dict[str, Dict[str, AsyncFrameHandler]] = {}
        #: Per-stream "channel closed" callbacks keyed by stream id (cmdId/ptyId).
        #: A long-running command awaits cmd.exit as an async frame, not an RPC
        #: reply, so a drop must reject it through here or wait() hangs forever.
        self._stream_closers: Dict[str, Any] = {}
        #: Frames that arrived before their handler was registered, keyed by
        #: ``type \x1f stream_id``. The guest can stream ``cmd.data``/``cmd.exit``
        #: onto the wire BEFORE the ``cmd.start`` reply (its output pumps start
        #: before the reply is sent), so frames routinely beat the caller's
        #: on_frame() registration. Stash them here and flush on registration so
        #: no early output is lost. Bounded against handlers that never arrive.
        self._orphan_frames: Dict[str, list] = {}
        self._orphan_count = 0
        self._closed = False

    _MAX_ORPHAN_FRAMES = 1024

    @property
    def connected(self) -> bool:
        return bool(self._ws and self._ws.is_open)

    def set_control_url(self, url: str) -> None:
        self._control_url = url

    async def connect(self) -> None:
        """Open the control WebSocket. Idempotent: concurrent callers share one
        in-flight connect, and an already-open socket resolves immediately.
        Transient upstream-handshake failures are retried (see
        ``CONNECT_MAX_ATTEMPTS``)."""
        if self.connected:
            return
        if self._connecting is not None:
            await self._connecting
            return
        self._closed = False
        self._connecting = asyncio.ensure_future(self._connect_with_retry())
        await self._connecting

    async def _connect_with_retry(self) -> None:
        """Retry a fast-failing connect a few times; surface the last error otherwise."""
        last_err: Exception = SolariConnectionError("connect failed")
        for attempt in range(1, CONNECT_MAX_ATTEMPTS + 1):
            try:
                await self._connect_once()
                self._connecting = None
                return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                # Give up immediately if the caller closed the channel, or the
                # dial hung to the full timeout (not a transient — retrying just
                # re-hangs).
                if self._closed or isinstance(last_err, SolariTimeoutError):
                    break
                if attempt < CONNECT_MAX_ATTEMPTS:
                    await asyncio.sleep(CONNECT_RETRY_BASE_MS * attempt / 1000)
        self._connecting = None
        raise last_err

    async def _connect_once(self) -> None:
        """One connect attempt: open the socket, resolve on open, raise on error/timeout."""
        cb = WsCallbacks(
            on_message=self._on_message,
            on_close=self._on_close,
            on_error=self._on_error,
        )
        try:
            sock = await asyncio.wait_for(
                open_websocket(self._control_url, self._headers, cb),
                timeout=CONNECT_TIMEOUT_MS / 1000,
            )
        except asyncio.TimeoutError:
            raise SolariTimeoutError("connect", CONNECT_TIMEOUT_MS) from None
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, SolariError):
                raise
            raise SolariConnectionError(str(exc)) from exc

        self._ws = sock

    async def reconnect(self) -> None:
        if self.connected:
            return
        if self._ws is not None:
            await self._ws.close()
        self._ws = None
        self._connecting = None
        await self.connect()

    def _fail_all(self, err: Exception) -> None:
        """Reject every in-flight call + stream wait and drop all handler state."""
        for p in self._pending.values():
            if not p.future.done():
                p.future.set_exception(err)
        self._pending.clear()
        for cb in list(self._stream_closers.values()):
            try:
                cb(err)
            except Exception:  # noqa: BLE001 - a bad closer must not block teardown
                pass
        self._stream_closers.clear()
        self._stream_handlers.clear()
        self._frame_handlers.clear()
        self._orphan_frames.clear()
        self._orphan_count = 0

    async def close(self) -> None:
        self._closed = True
        self._fail_all(SolariConnectionError("Control channel closed"))
        if self._ws is not None:
            await self._ws.close()
        self._ws = None

    # --- async frame subscriptions (v2) --------------------------------------

    def on_frame(self, type_: str, id_: str, handler: AsyncFrameHandler) -> None:
        self._frame_handlers.setdefault(type_, {})[id_] = handler
        key = type_ + "\x1f" + id_
        pending = self._orphan_frames.pop(key, None)
        if pending:
            self._orphan_count -= len(pending)
            for f in pending:
                handler(f)

    def off_frame(self, type_: str, id_: str) -> None:
        self._frame_handlers.get(type_, {}).pop(id_, None)

    def on_stream_close(self, id_: str, cb) -> None:
        """Register a "channel closed" callback for a stream id so a drop rejects
        its in-flight wait. Cleared on the stream's terminal frame.

        If the channel is ALREADY torn down when this is called, fire the callback
        synchronously instead of storing it — otherwise the wakeup is lost. A
        caller registers this AFTER the ``cmd.start`` reply resolves; if the
        channel closed in that window ``_fail_all`` has already drained
        ``_stream_closers``, so a late store would sit unreferenced and ``wait()``
        would hang forever. (Real under asyncio's concurrent scheduling.)"""
        if self._closed or not self.connected:
            cb(SolariConnectionError("Control channel closed"))
            return
        self._stream_closers[id_] = cb

    def off_stream_close(self, id_: str) -> None:
        self._stream_closers.pop(id_, None)

    # --- transport -----------------------------------------------------------

    def _on_message(self, chunk: str) -> None:
        self._rx_buffer += chunk
        while True:
            nl = self._rx_buffer.find("\n")
            if nl == -1:
                break
            line = self._rx_buffer[:nl].strip()
            self._rx_buffer = self._rx_buffer[nl + 1 :]
            if line:
                self._dispatch(line)

    def _dispatch(self, line: str) -> None:
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(frame, dict):
            return

        # (3) v2 async STREAM frame.
        ftype = frame.get("type")
        if isinstance(ftype, str):
            stream_id = frame.get("cmdId") or frame.get("ptyId") or frame.get("watchId") or ""
            handler = self._frame_handlers.get(ftype, {}).get(stream_id)
            if handler is not None:
                handler(frame)
            elif self._orphan_count < self._MAX_ORPHAN_FRAMES:
                # Handler not registered yet — stash for flush on on_frame().
                key = ftype + "\x1f" + stream_id
                self._orphan_frames.setdefault(key, []).append(frame)
                self._orphan_count += 1
            return

        fid = frame.get("id")
        if not isinstance(fid, str):
            return

        # (2) v1 streamed-exec frame.
        stream = frame.get("stream")
        if isinstance(stream, str):
            h = self._stream_handlers.get(fid)
            if h is not None:
                h(stream, frame.get("data"))
            return

        # (1) JSON-RPC reply.
        p = self._pending.pop(fid, None)
        self._stream_handlers.pop(fid, None)
        if p is None or p.future.done():
            return
        if frame.get("ok"):
            p.future.set_result(frame.get("result"))
        else:
            message, code = _normalize_rpc_error(frame.get("error"))
            p.future.set_exception(ActionError(p.method, message, code))

    def _on_close(self, code: int, reason: str) -> None:
        self._ws = None
        # Always tear down handler state + reject in-flight calls AND stream
        # waits (a command's exit-wait is frame-based, not a pending RPC, so an
        # early-return-when-no-pending would hang it forever on a drop).
        suffix = f": {reason}" if reason else ""
        self._fail_all(SolariConnectionError(f"Control channel closed ({code}{suffix})"))

    def _on_error(self, exc: Exception) -> None:
        self._ws = None
        err = exc if isinstance(exc, SolariError) else SolariConnectionError(str(exc))
        self._fail_all(err)

    def new_id(self) -> str:
        cid = str(self._next_id)
        self._next_id += 1
        return cid

    async def call(
        self,
        method: str,
        params: Any,
        *,
        on_stream: Optional[RpcStreamHandler] = None,
        id: Optional[str] = None,
        timeout_ms: Optional[int] = None,
    ) -> Any:
        if self._closed:
            raise SolariConnectionError("Control channel is closed")
        if self._ws is None or not self._ws.is_open:
            raise SolariConnectionError("Not connected — call connect() first")

        call_id = id or self.new_id()
        frame = json.dumps({"id": call_id, "method": method, "params": params})
        timeout = (timeout_ms or self._call_timeout_ms) / 1000

        loop = asyncio.get_event_loop()
        future: "asyncio.Future[Any]" = loop.create_future()
        self._pending[call_id] = _Pending(future, method)
        if on_stream is not None:
            self._stream_handlers[call_id] = on_stream

        try:
            await self._ws.send(frame + "\n")
        except Exception as exc:  # noqa: BLE001
            self._pending.pop(call_id, None)
            self._stream_handlers.pop(call_id, None)
            if isinstance(exc, SolariError):
                raise
            raise SolariError(f'Failed to send "{method}"') from exc

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(call_id, None)
            self._stream_handlers.pop(call_id, None)
            raise SolariTimeoutError(method, int(timeout * 1000)) from None


def _normalize_rpc_error(err: Any) -> "tuple[str, Optional[str]]":
    if err is None:
        return "Action failed", None
    if isinstance(err, str):
        return err, None
    if isinstance(err, dict):
        return err.get("message") or err.get("code") or "Action failed", err.get("code")
    return "Action failed", None


__all__ = ["ControlChannel", "RpcStreamHandler", "AsyncFrameHandler"]
