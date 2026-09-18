"""``Desktop`` — a handle to one live GUI session.

Extends the shared :class:`~solari_desktop.handle.SessionHandle` (commands,
pty, run_code, files, metrics, snapshot, revert, pause/resume, set_timeout, env,
volumes, kill) and adds the computer-use GUI surface (CONTRACTS §4 +
CONTRACTS-V2 §4): screenshot, mouse.*, keyboard.*, display.*, clipboard.*, open,
stream.*, record.*. Mirrors ``sdk/src/desktop.ts``.

Back-compat: the v1 :class:`~solari_desktop.client.DesktopClient` constructs
``Desktop(session, DesktopConfig(on_pause=...))`` where ``on_pause(session_id)``
calls ``POST /desktops/:id/pause``. That path is preserved (folded into the
unified hooks), and the older convenience surfaces (``exec``, ``exec_stream``,
``health``, ``process``, ``ports``, ``pkg``, ``fs``) are kept.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Union

from .handle import SessionConfig, SessionHandle, SessionHooks
from .types import (
    CreateDesktopResponse,
    ExecResult,
    ExecStreamChunk,
    FsEntry,
    FsStat,
    HealthResult,
    KeyAction,
    MouseAction,
    MouseButton,
    PackageManager,
    PkgInstallResult,
    PortInfo,
    ProcessInfo,
    ScreenshotFormat,
)

#: Callback invoked for each streamed exec output chunk.
ExecStreamHandler = Callable[[ExecStreamChunk], None]


@dataclass
class DesktopConfig(SessionConfig):
    """Construction config passed by :class:`~solari_desktop.client.DesktopClient`.

    Adds the legacy ``on_pause`` shim (folded into ``hooks.pause``).
    """

    on_pause: Optional[Callable[[str], Any]] = None


class Desktop(SessionHandle):
    """A live desktop session. Construct via :class:`~solari_desktop.client.DesktopClient`."""

    def __init__(
        self,
        session: CreateDesktopResponse,
        config: Optional[DesktopConfig] = None,
    ) -> None:
        config = config or DesktopConfig()
        hooks = config.hooks or SessionHooks()
        # Fold the legacy on_pause shim into the unified hooks.pause.
        if config.on_pause is not None and hooks.pause is None:
            _on_pause = config.on_pause

            async def _pause(session_id: str) -> None:
                res = _on_pause(session_id)
                if hasattr(res, "__await__"):
                    await res

            hooks.pause = _pause

        base = SessionConfig(callTimeoutMs=config.callTimeoutMs, headers=config.headers, hooks=hooks)
        super().__init__(session.sessionId, session.controlUrl, session.expiresAt, base)
        self.streamUrl: str = session.streamUrl
        #: Presigned playback URL for the session recording — set when the session
        #: was created with ``record=True``, ``None`` otherwise. The guest uploads
        #: the mp4 on ``record.stop()``, so it only resolves once a recording has
        #: been started and stopped.
        self.recordingUrl: Optional[str] = session.recordingUrl

        self.fs = _Fs(self)
        self.mouse = _Mouse(self)
        self.keyboard = _Keyboard(self)
        self.display = _Display(self)
        self.clipboard = _Clipboard(self)
        self.process = _Process(self)
        self.ports = _Ports(self)
        self.pkg = _Pkg(self)
        self.record = _Record(self)
        self.stream = _Stream(self)

    @property
    def sessionId(self) -> str:
        """Alias of :attr:`SessionHandle.id` (back-compat)."""
        return self.id

    async def _call(self, method: str, params: Any) -> Any:
        return await self._channel.call(method, params)

    # --- readiness ------------------------------------------------------------

    async def health(self) -> HealthResult:
        r = await self._call("health", {})
        return HealthResult(
            ready=bool(r.get("ready")), display=bool(r.get("display")), vnc=bool(r.get("vnc"))
        )

    # --- exec (v1 convenience; prefer commands.run) ---------------------------

    async def exec(
        self,
        cmd: str,
        *,
        args: Optional[List[str]] = None,
        cwd: Optional[str] = None,
        timeout_ms: Optional[int] = None,
        stream: Optional[bool] = None,
    ) -> ExecResult:
        r = await self._call(
            "exec",
            {"cmd": cmd, "args": args or [], "cwd": cwd, "timeoutMs": timeout_ms, "stream": stream},
        )
        return ExecResult(
            exitCode=int(r.get("exitCode", 0)),
            stdout=str(r.get("stdout", "")),
            stderr=str(r.get("stderr", "")),
        )

    async def exec_stream(
        self,
        cmd: str,
        on_chunk: ExecStreamHandler,
        *,
        args: Optional[List[str]] = None,
        cwd: Optional[str] = None,
        timeout_ms: Optional[int] = None,
    ) -> ExecResult:
        def _on_stream(stream: str, data: Optional[str]) -> None:
            raw = base64.b64decode(data) if data else b""
            on_chunk(ExecStreamChunk(stream=stream, text=raw.decode("utf-8", "replace"), bytes=raw))

        r = await self._channel.call(
            "exec",
            {"cmd": cmd, "args": args or [], "cwd": cwd, "timeoutMs": timeout_ms, "stream": True},
            on_stream=_on_stream,
        )
        return ExecResult(
            exitCode=int(r.get("exitCode", 0)),
            stdout=str(r.get("stdout", "")),
            stderr=str(r.get("stderr", "")),
        )

    # --- screenshot -----------------------------------------------------------

    async def screenshot(
        self, *, format: ScreenshotFormat = "png", quality: Optional[int] = None
    ) -> bytes:
        r = await self._call("screenshot", {"format": format, "quality": quality})
        return base64.b64decode(r["base64"])

    # --- open an app ----------------------------------------------------------

    async def open(self, name: str, args: Optional[List[str]] = None) -> int:
        """Launch a GUI app by name (``app.open``); returns its pid."""
        r = await self._call("app.open", {"name": name, "args": args})
        return int(r["pid"])


# ---------------------------------------------------------------------------
# Grouped action namespaces
# ---------------------------------------------------------------------------


class _Fs:
    """Filesystem actions: ``desktop.fs`` (v1 alias of ``desktop.files``)."""

    def __init__(self, d: Desktop) -> None:
        self._d = d

    async def read(self, path: str) -> bytes:
        return await self._d.files.read(path)

    async def read_text(self, path: str) -> str:
        return await self._d.files.read_text(path)

    async def write(self, path: str, data: Union[bytes, str], mode: Optional[int] = None) -> None:
        await self._d.files.write(path, data, mode)

    async def list(self, path: str) -> List[FsEntry]:
        return await self._d.files.list(path)

    async def stat(self, path: str) -> FsStat:
        return await self._d.files.stat(path)

    async def remove(self, path: str, recursive: bool = False) -> None:
        await self._d.files.remove(path, recursive)

    async def mkdir(self, path: str) -> None:
        await self._d.files.mkdir(path)


_BUTTON_CODES = {"left": 1, "middle": 2, "right": 3}


def _button_to_code(button: Optional[MouseButton]) -> Optional[int]:
    """Map a named button to the X11/xdotool code the guest agent expects
    (``button`` is an ``int``: left=1, middle=2, right=3). ``None`` passes
    through so the guest applies its default (left). Without this the wire
    carries a raw string and the guest's JSON decode fails with
    ``cannot unmarshal string into ... button of type int``."""
    if button is None:
        return None
    return _BUTTON_CODES[button]


class _Mouse:
    """Mouse actions: ``desktop.mouse``."""

    def __init__(self, d: Desktop) -> None:
        self._d = d

    async def _mouse_call(
        self,
        x: int,
        y: int,
        action: MouseAction,
        button: Optional[MouseButton] = None,
        humanize: Optional[bool] = None,
    ) -> None:
        await self._d._call(
            "input.mouse",
            {"x": x, "y": y, "action": action, "button": _button_to_code(button), "humanize": humanize},
        )

    async def move(self, x: int, y: int, *, humanize: Optional[bool] = None) -> None:
        await self._mouse_call(x, y, "move", None, humanize)

    async def click(
        self, x: int, y: int, *, button: Optional[MouseButton] = None, humanize: Optional[bool] = None
    ) -> None:
        await self._mouse_call(x, y, "click", button, humanize)

    async def double_click(self, x: int, y: int, *, button: Optional[MouseButton] = None) -> None:
        await self._d._call("input.doubleClick", {"x": x, "y": y, "button": _button_to_code(button)})

    async def down(self, x: int, y: int, button: MouseButton = "left") -> None:
        await self._mouse_call(x, y, "down", button)

    async def up(self, x: int, y: int, button: MouseButton = "left") -> None:
        await self._mouse_call(x, y, "up", button)

    async def scroll(
        self, x: int, y: int, *, button: Optional[MouseButton] = None, humanize: Optional[bool] = None
    ) -> None:
        await self._mouse_call(x, y, "scroll", button, humanize)

    async def drag(
        self,
        frm: Dict[str, int],
        to: Dict[str, int],
        button: MouseButton = "left",
    ) -> None:
        await self._d._call("input.drag", {"from": frm, "to": to, "button": _button_to_code(button)})


class _Keyboard:
    """Keyboard actions: ``desktop.keyboard``."""

    def __init__(self, d: Desktop) -> None:
        self._d = d

    async def _key_call(
        self,
        *,
        text: Optional[str] = None,
        keys: Optional[List[str]] = None,
        action: KeyAction = "press",
    ) -> None:
        params: Dict[str, Any] = {"action": action}
        if text is not None:
            params["text"] = text
        if keys is not None:
            params["keys"] = keys
        await self._d._call("input.key", params)

    async def type(self, text: str) -> None:
        await self._key_call(text=text, action="press")

    async def press(self, keys: Union[str, List[str]]) -> None:
        await self._key_call(keys=[keys] if isinstance(keys, str) else keys, action="press")

    async def hotkey(self, *keys: str) -> None:
        """Press a chord, e.g. ``hotkey("ctrl", "c")``."""
        await self._key_call(keys=list(keys), action="press")

    async def down(self, keys: Union[str, List[str]]) -> None:
        await self._key_call(keys=[keys] if isinstance(keys, str) else keys, action="down")

    async def up(self, keys: Union[str, List[str]]) -> None:
        await self._key_call(keys=[keys] if isinstance(keys, str) else keys, action="up")


class _Display:
    """Display actions: ``desktop.display``."""

    def __init__(self, d: Desktop) -> None:
        self._d = d

    async def set(self, w: int, h: int) -> None:
        await self._d._call("display.set", {"w": w, "h": h})

    async def size(self) -> Dict[str, int]:
        """Current display size ``{w, h}``."""
        return await self._d._call("display.size", {})

    async def cursor(self) -> Dict[str, int]:
        """Current cursor position ``{x, y}``."""
        return await self._d._call("display.cursor", {})


class _Clipboard:
    """Clipboard actions: ``desktop.clipboard``."""

    def __init__(self, d: Desktop) -> None:
        self._d = d

    async def get(self) -> str:
        r = await self._d._call("clipboard.get", {})
        return r.get("text") or ""

    async def set(self, text: str) -> None:
        await self._d._call("clipboard.set", {"text": text})


class _Process:
    """Process actions: ``desktop.process``."""

    def __init__(self, d: Desktop) -> None:
        self._d = d

    async def list(self) -> List[ProcessInfo]:
        r = await self._d._call("process.list", {})
        return [
            ProcessInfo(pid=int(p["pid"]), name=str(p.get("name", "")), cmd=p.get("cmd"))
            for p in r.get("processes", [])
        ]

    async def kill(self, pid: int) -> None:
        await self._d._call("process.kill", {"pid": pid})

    async def start(
        self, cmd: str, *, args: Optional[List[str]] = None, cwd: Optional[str] = None
    ) -> int:
        r = await self._d._call("process.start", {"cmd": cmd, "args": args or [], "cwd": cwd})
        return int(r["pid"])

    async def signal(self, pid: int, signal: Optional[int] = None) -> None:
        await self._d._call("process.signal", {"pid": pid, "signal": signal})


class _Ports:
    """Listening-port introspection: ``desktop.ports``."""

    def __init__(self, d: Desktop) -> None:
        self._d = d

    async def list(self) -> List[PortInfo]:
        r = await self._d._call("ports.list", {})
        return [
            PortInfo(
                port=int(p["port"]),
                addr=str(p.get("addr", "")),
                pid=int(p["pid"]) if p.get("pid") else None,
            )
            for p in r.get("ports", [])
        ]


class _Pkg:
    """Package installation: ``desktop.pkg``."""

    def __init__(self, d: Desktop) -> None:
        self._d = d

    async def install(self, manager: PackageManager, packages: List[str]) -> PkgInstallResult:
        r = await self._d._call("pkg.install", {"manager": manager, "packages": packages})
        return PkgInstallResult(
            exitCode=int(r.get("exitCode", 0)),
            stdout=str(r.get("stdout", "")),
            stderr=str(r.get("stderr", "")),
        )


class _Record:
    """Server-side session recording: ``desktop.record``."""

    def __init__(self, d: Desktop) -> None:
        self._d = d

    async def start(
        self,
        fps: Optional[int] = None,
        format: Optional[str] = None,
        path: Optional[str] = None,
    ) -> dict:
        params: dict = {}
        if fps is not None:
            params["fps"] = fps
        if format is not None:
            params["format"] = format
        if path is not None:
            params["path"] = path
        return await self._d._call("record.start", params)

    async def stop(self) -> dict:
        return await self._d._call("record.stop", {})


class _Stream:
    """Live VNC stream control: ``desktop.stream``."""

    def __init__(self, d: Desktop) -> None:
        self._d = d

    async def start(self) -> Dict[str, Any]:
        """Return the embeddable stream URL (minted at create time)."""
        return {"streamUrl": self._d.streamUrl}

    async def stop(self) -> None:
        """No-op: the RFB stream is a separate socket the caller owns."""
        return None


__all__ = ["Desktop", "DesktopConfig", "ExecStreamHandler"]
