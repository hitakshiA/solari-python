"""A tiny WebSocket abstraction so ``desktop.py`` has a minimal, swappable
transport — mirroring ``sdk/src/ws.ts``.

The TS file abstracts over Node's ``ws`` package and the browser's native
``WebSocket``. Python has a single canonical async client (``websockets``), so
this module wraps it behind the same minimal surface: open/close, send text,
and the three lifecycle callbacks (``on_open`` is implied by the open coroutine
resolving). ``headers`` are forwarded to the upgrade request.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Dict, Optional, Protocol

import websockets
from websockets.client import WebSocketClientProtocol

from .errors import ConnectionError as SolariConnectionError


class WsLike(Protocol):
    """Minimal WebSocket surface used by :class:`~solari_desktop.desktop.Desktop`."""

    async def send(self, data: str) -> None: ...

    async def close(self) -> None: ...

    @property
    def is_open(self) -> bool: ...


class WsCallbacks:
    """Lifecycle callbacks for the control socket.

    Mirrors the TS ``WsCallbacks`` (minus ``on_open``, which in Python is the
    point at which :func:`open_websocket` returns).
    """

    def __init__(
        self,
        on_message: Callable[[str], None],
        on_close: Callable[[int, str], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        self.on_message = on_message
        self.on_close = on_close
        self.on_error = on_error


class _WebsocketsAdapter:
    """Adapts a connected ``websockets`` client to :class:`WsLike` and pumps
    inbound frames into the supplied callbacks via a background read task."""

    def __init__(self, sock: WebSocketClientProtocol, cb: WsCallbacks) -> None:
        self._sock = sock
        self._cb = cb
        self._open = True
        self._reader: asyncio.Task[None] = asyncio.ensure_future(self._read_loop())

    async def send(self, data: str) -> None:
        await self._sock.send(data)

    async def close(self) -> None:
        self._open = False
        await self._sock.close()

    @property
    def is_open(self) -> bool:
        # websockets <14 exposes `.open` (bool); >=14 removed it in favor of
        # `.state` (State.OPEN). Support both without an AttributeError.
        sock_open = getattr(self._sock, "open", None)
        if sock_open is None:
            state = getattr(self._sock, "state", None)
            sock_open = state is not None and getattr(state, "name", "") == "OPEN"
        return self._open and bool(sock_open)

    async def _read_loop(self) -> None:
        try:
            async for message in self._sock:
                # The control channel is text (newline-delimited JSON). Decode
                # any binary frames defensively.
                if isinstance(message, bytes):
                    message = message.decode("utf-8", "replace")
                self._cb.on_message(message)
        except websockets.ConnectionClosed as exc:
            self._open = False
            self._cb.on_close(exc.code, exc.reason or "")
        except Exception as exc:  # noqa: BLE001 - surface any transport error
            self._open = False
            self._cb.on_error(exc)
        else:
            self._open = False
            self._cb.on_close(1000, "")


async def open_websocket(
    url: str,
    headers: Optional[Dict[str, str]],
    cb: WsCallbacks,
) -> WsLike:
    """Open a control WebSocket to ``url`` and start pumping frames into ``cb``.

    ``headers`` (e.g. ``Authorization``) are sent on the upgrade request.
    Returns once the socket is open; raises
    :class:`~solari_desktop.errors.ConnectionError` on failure.
    """
    hdrs = list((headers or {}).items())
    # The connect timeout is enforced by the caller (asyncio.wait_for), so we do
    # NOT pass open_timeout — it doesn't exist before websockets v10 and leaks to
    # create_connection there. The header kwarg was also renamed extra_headers ->
    # additional_headers in v14, so try both for cross-version support.
    try:
        try:
            sock = await websockets.connect(url, extra_headers=hdrs)
        except TypeError as te:
            # websockets >=14 renamed extra_headers -> additional_headers. Only
            # fall back for THAT specific rename; re-raise any other TypeError.
            if "extra_headers" not in str(te):
                raise
            sock = await websockets.connect(url, additional_headers=hdrs)
    except Exception as exc:  # noqa: BLE001 - normalize to a typed error
        raise SolariConnectionError(f"WebSocket connect failed: {exc}") from exc
    return _WebsocketsAdapter(sock, cb)


__all__ = ["WsLike", "WsCallbacks", "open_websocket"]
