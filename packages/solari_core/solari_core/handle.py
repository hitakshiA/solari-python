"""``SessionHandle`` — the shared base for
:class:`~solari_desktop.sandbox.Sandbox` and
:class:`~solari_desktop.desktop.Desktop`. Mirrors ``sdk/src/handle.ts``.

Exposes the CONTRACTS-V2 §4 primitives (commands, pty, run_code, files) plus the
gateway-routed lifecycle/admin calls (metrics, snapshot, revert, pause/resume,
set_timeout, env, volumes, kill). Async output from
``commands.start`` / ``pty.create`` / ``files.watch`` arrives as v2 frames
``{type:"cmd.data"|"cmd.exit"|"pty.data"|"fs.event", ...}`` dispatched by the
:class:`~solari_desktop.transport.ControlChannel` by ``type`` + the stream's
own id (cmdId/ptyId/watchId), NOT by the originating request id.
"""

from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from .transport import ControlChannel
from .types import (
    Chart,
    ChartAxis,
    CodeLanguage,
    CodeResultItem,
    CommandResult,
    FsEntry,
    FsSearchMatch,
    FsStat,
    FsWatchEvent,
    GitBranch,
    GitCommit,
    GitStatus,
    MetricsResult,
    RunCodeResult,
)


@dataclass
class SessionHooks:
    """Lifecycle/admin wiring supplied by the owning sub-client so handle calls
    reach the gateway without the handle holding an HTTP client. Each is an
    async callable; unset hooks raise when the corresponding method is used."""

    metrics: Optional[Callable[[str], Awaitable[MetricsResult]]] = None
    snapshot: Optional[Callable[[str, Optional[str]], Awaitable[str]]] = None
    revert: Optional[Callable[[str, str], Awaitable[None]]] = None
    pause: Optional[Callable[[str], Awaitable[None]]] = None
    resume: Optional[Callable[[str], Awaitable[str]]] = None  # -> controlUrl
    set_timeout: Optional[Callable[[str, int], Awaitable[Dict[str, Any]]]] = None
    download_url: Optional[Callable[[str, str], Awaitable[Dict[str, Any]]]] = None
    upload_url: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None
    preview_url: Optional[Callable[[str, int], Awaitable[Dict[str, Any]]]] = None
    kill: Optional[Callable[[str], Awaitable[None]]] = None


@dataclass
class SessionConfig:
    """Construction config passed by the owning sub-client."""

    callTimeoutMs: Optional[int] = None
    headers: Optional[Dict[str, str]] = None
    hooks: SessionHooks = field(default_factory=SessionHooks)


class _CommandHandle:
    """A started command (``commands.start``)."""

    def __init__(self, channel: ControlChannel, cmd_id: str) -> None:
        self._channel = channel
        self.cmdId = cmd_id
        self._data_cbs: List[Callable[[str, str], None]] = []
        # Frames can be flushed (transport orphan buffer) or arrive before the
        # caller attaches its accumulator via on_data(); buffer until a consumer
        # exists, then replay, so early stdout/stderr is never dropped.
        self._buffered: List[tuple] = []
        self._exit: "asyncio.Future[int]" = asyncio.get_running_loop().create_future()

        def _on_data(frame: Dict[str, Any]) -> None:
            stream = frame.get("stream") or "stdout"
            b64 = frame.get("base64")
            data = base64.b64decode(b64).decode("utf-8", "replace") if b64 else ""
            if not self._data_cbs:
                self._buffered.append((stream, data))
            else:
                for cb in self._data_cbs:
                    cb(stream, data)

        def _on_exit(frame: Dict[str, Any]) -> None:
            self._channel.off_frame("cmd.data", cmd_id)
            self._channel.off_frame("cmd.exit", cmd_id)
            self._channel.off_stream_close(cmd_id)
            if not self._exit.done():
                self._exit.set_result(int(frame.get("exitCode", 0)))

        def _on_closed(err: Exception) -> None:
            # Control channel dropped before cmd.exit — reject wait() instead of
            # hanging forever.
            if not self._exit.done():
                self._exit.set_exception(err)

        channel.on_frame("cmd.data", cmd_id, _on_data)
        channel.on_frame("cmd.exit", cmd_id, _on_exit)
        channel.on_stream_close(cmd_id, _on_closed)

    def on_data(self, cb: Callable[[str, str], None]) -> None:
        if self._buffered:
            for stream, data in self._buffered:
                cb(stream, data)
            self._buffered.clear()
        self._data_cbs.append(cb)

    async def stdin(self, data: Union[bytes, str]) -> None:
        await self._channel.call("cmd.stdin", {"cmdId": self.cmdId, "base64": _b64(data)})

    async def wait(self) -> int:
        return await self._exit

    async def kill(self, signal: Optional[int] = None) -> None:
        await self._channel.call("cmd.kill", {"cmdId": self.cmdId, "signal": signal})


class _PtyHandle:
    """A PTY (``pty.create``)."""

    def __init__(self, channel: ControlChannel, pty_id: str) -> None:
        self._channel = channel
        self.ptyId = pty_id
        self._data_cbs: List[Callable[[bytes], None]] = []

        def _on_data(frame: Dict[str, Any]) -> None:
            b64 = frame.get("base64")
            raw = base64.b64decode(b64) if b64 else b""
            for cb in self._data_cbs:
                cb(raw)

        channel.on_frame("pty.data", pty_id, _on_data)

    def on_data(self, cb: Callable[[bytes], None]) -> None:
        self._data_cbs.append(cb)

    async def write(self, data: Union[bytes, str]) -> None:
        await self._channel.call("pty.input", {"ptyId": self.ptyId, "base64": _b64(data)})

    async def resize(self, cols: int, rows: int) -> None:
        await self._channel.call("pty.resize", {"ptyId": self.ptyId, "cols": cols, "rows": rows})

    async def kill(self) -> None:
        self._channel.off_frame("pty.data", self.ptyId)
        await self._channel.call("pty.kill", {"ptyId": self.ptyId})


class SessionHandle:
    """Shared base for sandbox + desktop handles."""

    def __init__(
        self,
        id: str,
        control_url: str,
        expires_at: str,
        config: Optional[SessionConfig] = None,
    ) -> None:
        config = config or SessionConfig()
        self.id = id
        self.controlUrl = control_url
        self.expiresAt = expires_at
        self._channel = ControlChannel(
            control_url,
            call_timeout_ms=config.callTimeoutMs,
            headers=config.headers,
        )
        self._hooks = config.hooks
        self.commands = _Commands(self)
        self.pty = _Pty(self)
        self.files = _Files(self)
        self.volumes = _Volumes(self)
        self.git = _Git(self)

    @property
    def connected(self) -> bool:
        return self._channel.connected

    async def connect(self) -> None:
        await self._channel.connect()

    async def reconnect(self) -> None:
        await self._channel.reconnect()

    async def close(self) -> None:
        await self._channel.close()

    # --- run_code -------------------------------------------------------------

    async def run_code(
        self,
        code: str,
        *,
        language: Optional[CodeLanguage] = None,
        context_id: Optional[str] = None,
        on_stdout: Optional[Callable[[str], None]] = None,
        on_stderr: Optional[Callable[[str], None]] = None,
    ) -> RunCodeResult:
        """Run code in a stateful kernel (``code.run``)."""
        r = await self._channel.call(
            "code.run", {"code": code, "language": language, "contextId": context_id}
        )
        raw = r.get("results") if isinstance(r, dict) else None
        items: List[CodeResultItem] = []
        for it in raw or []:
            item = CodeResultItem(
                type=it.get("type", "result"),
                text=it.get("text"),
                png=it.get("png"),
                jpeg=it.get("jpeg"),
                svg=it.get("svg"),
                html=it.get("html"),
                latex=it.get("latex"),
                json=it.get("json"),
                markdown=it.get("markdown"),
                chart=_parse_chart(it.get("chart")),
            )
            if item.type == "stdout" and item.text and on_stdout:
                on_stdout(item.text)
            if item.type == "stderr" and item.text and on_stderr:
                on_stderr(item.text)
            items.append(item)
        charts = [i.chart for i in items if i.chart is not None]
        return RunCodeResult(
            results=items,
            error=(r.get("error") if isinstance(r, dict) else None),
            charts=charts,
        )

    async def create_code_context(self, language: CodeLanguage = "python") -> str:
        """Create a fresh stateful kernel context (``code.context.create``)."""
        r = await self._channel.call("code.context.create", {"language": language})
        return r["contextId"]

    # --- env ------------------------------------------------------------------

    async def env(self, vars: Dict[str, str]) -> None:
        """Inject/replace per-session env vars in the guest (``setEnv``)."""
        await self._channel.call("setEnv", {"env": vars})

    async def download_url(self, path: str) -> Dict[str, Any]:
        """Signed, time-limited URL to download an in-guest file directly over
        HTTP (bypasses the control channel — good for large files)."""
        if self._hooks.download_url is None:
            raise RuntimeError("download_url requires a client-created handle")
        return await self._hooks.download_url(self.id, path)

    async def upload_url(self, path: Optional[str] = None) -> Dict[str, Any]:
        """Signed, time-limited URL to upload a file into the guest over HTTP."""
        if self._hooks.upload_url is None:
            raise RuntimeError("upload_url requires a client-created handle")
        return await self._hooks.upload_url(self.id, path)

    async def preview_url(self, port: int) -> Dict[str, Any]:
        """Public preview URL for an in-guest port (e.g. a dev server on 3000).
        Returns ``{"url": ..., "token"?: ...}``."""
        if self._hooks.preview_url is None:
            raise RuntimeError("preview_url requires a client-created handle")
        return await self._hooks.preview_url(self.id, port)

    # --- lifecycle / admin ----------------------------------------------------

    async def metrics(self) -> MetricsResult:
        if self._hooks.metrics is None:
            raise RuntimeError("metrics requires a client-created handle")
        return await self._hooks.metrics(self.id)

    async def snapshot(self, name: Optional[str] = None) -> str:
        if self._hooks.snapshot is None:
            raise RuntimeError("snapshot requires a client-created handle")
        return await self._hooks.snapshot(self.id, name)

    async def revert(self, snapshot_id: str) -> None:
        if self._hooks.revert is None:
            raise RuntimeError("revert requires a client-created handle")
        await self._hooks.revert(self.id, snapshot_id)

    async def pause(self) -> None:
        if self._hooks.pause is None:
            raise RuntimeError("pause requires a client-created handle")
        await self._hooks.pause(self.id)
        await self.close()

    async def resume(self) -> None:
        if self._hooks.resume is None:
            raise RuntimeError("resume requires a client-created handle")
        control_url = await self._hooks.resume(self.id)
        self._channel.set_control_url(control_url)
        await self._channel.reconnect()

    async def set_timeout(self, timeout_ms: int) -> Dict[str, Any]:
        if self._hooks.set_timeout is None:
            raise RuntimeError("set_timeout requires a client-created handle")
        return await self._hooks.set_timeout(self.id, timeout_ms)

    async def kill(self) -> None:
        if self._hooks.kill is not None:
            await self._hooks.kill(self.id)
        await self.close()


class _Commands:
    """``handle.commands``."""

    def __init__(self, h: SessionHandle) -> None:
        self._h = h

    async def start(
        self,
        cmd: str,
        *,
        args: Optional[List[str]] = None,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        user: Optional[str] = None,
        on_stdout: Optional[Callable[[str], None]] = None,
        on_stderr: Optional[Callable[[str], None]] = None,
    ) -> _CommandHandle:
        """Start a command; returns a handle (does not wait for exit). The guest
        runs ``cmd`` with ``args`` (NOT via a shell) — for shell syntax use
        ``run("sh", args=["-c", "…"])``."""
        r = await self._h._channel.call(
            "cmd.start", {"cmd": cmd, "args": args, "cwd": cwd, "env": env, "user": user}
        )
        handle = _CommandHandle(self._h._channel, r["cmdId"])
        if on_stdout or on_stderr:
            def _route(stream: str, data: str) -> None:
                if stream == "stdout" and on_stdout:
                    on_stdout(data)
                elif stream == "stderr" and on_stderr:
                    on_stderr(data)

            handle.on_data(_route)
        return handle

    async def run(
        self,
        cmd: str,
        *,
        args: Optional[List[str]] = None,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        user: Optional[str] = None,
        timeout_ms: Optional[int] = None,
        background: bool = False,
        on_stdout: Optional[Callable[[str], None]] = None,
        on_stderr: Optional[Callable[[str], None]] = None,
    ) -> CommandResult:
        """Run a command to completion (or detached when ``background``)."""
        out: List[str] = []
        err: List[str] = []

        def _route(stream: str, data: str) -> None:
            (out if stream == "stdout" else err).append(data)
            if stream == "stdout" and on_stdout:
                on_stdout(data)
            elif stream == "stderr" and on_stderr:
                on_stderr(data)

        handle = await self.start(cmd, args=args, cwd=cwd, env=env, user=user)
        handle.on_data(_route)
        if background:
            return CommandResult(exitCode=0, stdout="", stderr="")
        exit_code = await handle.wait()
        return CommandResult(exitCode=exit_code, stdout="".join(out), stderr="".join(err))

    async def connect(self) -> None:
        await self._h._channel.reconnect()


class _Pty:
    """``handle.pty``."""

    def __init__(self, h: SessionHandle) -> None:
        self._h = h

    async def create(
        self,
        *,
        cols: int,
        rows: int,
        cmd: Optional[str] = None,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> _PtyHandle:
        """Open a PTY; output streams as ``pty.data`` frames."""
        r = await self._h._channel.call(
            "pty.create", {"cols": cols, "rows": rows, "cmd": cmd, "cwd": cwd, "env": env}
        )
        return _PtyHandle(self._h._channel, r["ptyId"])


class _Files:
    """``handle.files``."""

    def __init__(self, h: SessionHandle) -> None:
        self._h = h

    async def read(self, path: str) -> bytes:
        r = await self._h._channel.call("fs.read", {"path": path})
        return base64.b64decode(r["base64"])

    async def read_text(self, path: str) -> str:
        return (await self.read(path)).decode("utf-8")

    async def write(self, path: str, data: Union[bytes, str], mode: Optional[int] = None) -> None:
        await self._h._channel.call("fs.write", {"path": path, "base64": _b64(data), "mode": mode})

    async def list(self, path: str) -> List[FsEntry]:
        r = await self._h._channel.call("fs.list", {"path": path})
        return [
            FsEntry(name=e["name"], dir=bool(e.get("dir")), size=int(e.get("size", 0)))
            for e in r.get("entries", [])
        ]

    async def stat(self, path: str) -> FsStat:
        r = await self._h._channel.call("fs.stat", {"path": path})
        return FsStat(
            name=str(r.get("name", "")),
            dir=bool(r.get("dir")),
            size=int(r.get("size", 0)),
            mode=int(r.get("mode", 0)),
            modTimeMs=int(r.get("modTimeMs", 0)),
        )

    async def rename(self, frm: str, to: str) -> None:
        await self._h._channel.call("fs.rename", {"from": frm, "to": to})

    async def remove(self, path: str, recursive: bool = False) -> None:
        await self._h._channel.call("fs.remove", {"path": path, "recursive": recursive})

    async def mkdir(self, path: str) -> None:
        await self._h._channel.call("fs.mkdir", {"path": path})

    async def search(self, path: str, query: str, max_results: Optional[int] = None) -> List[FsSearchMatch]:
        r = await self._h._channel.call(
            "fs.search", {"path": path, "query": query, "maxResults": max_results}
        )
        return [
            FsSearchMatch(path=m["path"], line=int(m.get("line", 0)), text=str(m.get("text", "")))
            for m in r.get("matches", [])
        ]

    async def watch(
        self,
        path: str,
        cb: Callable[[FsWatchEvent], None],
        recursive: bool = False,
    ) -> Callable[[], Awaitable[None]]:
        """Watch ``path``; ``cb`` runs per ``fs.event`` frame. Returns an unwatch."""
        r = await self._h._channel.call("fs.watch", {"path": path, "recursive": recursive})
        watch_id = r["watchId"]

        def _on_event(frame: Dict[str, Any]) -> None:
            cb(FsWatchEvent(type=str(frame.get("type", "change")), path=str(frame.get("path", ""))))

        self._h._channel.on_frame("fs.event", watch_id, _on_event)

        async def _unwatch() -> None:
            self._h._channel.off_frame("fs.event", watch_id)
            await self._h._channel.call("fs.unwatch", {"watchId": watch_id})

        return _unwatch

    async def upload(self, path: str, data: Union[bytes, str]) -> None:
        await self._h._channel.call("fs.upload", {"path": path, "base64": _b64(data)})

    async def download(self, path: str) -> bytes:
        r = await self._h._channel.call("fs.download", {"path": path})
        return base64.b64decode(r["base64"])


class _Volumes:
    """``handle.volumes``."""

    def __init__(self, h: SessionHandle) -> None:
        self._h = h

    async def mount(self, vol_id: str, path: str) -> None:
        """Mount a volume into the guest (thin guest RPC; gateway route may 501)."""
        await self._h._channel.call("volume.mount", {"volumeId": vol_id, "path": path})


class _Git:
    """``handle.git`` — a first-class version-control surface (Daytona-parity).

    Every method is a safe, non-shell ``git`` invocation over the command RPC
    (``cmd`` + ``args``, never a shell string), so there is no injection surface
    and it runs against any guest that has ``git`` on PATH (the base image ships
    it). No guest/gateway change required. Mirrors ``sdk/.../handle.ts`` ``git``.
    """

    def __init__(self, h: SessionHandle) -> None:
        self._h = h

    async def _run(
        self, args: List[str], cwd: Optional[str] = None
    ) -> CommandResult:
        return await self._h.commands.run("git", args=args, cwd=cwd)

    async def _must(self, args: List[str], cwd: Optional[str] = None) -> str:
        r = await self._run(args, cwd)
        if r.exitCode != 0:
            detail = (r.stderr or r.stdout or "").strip()
            raise RuntimeError(
                f"git {args[0]} failed (exit {r.exitCode}): {detail}"
            )
        return r.stdout

    @staticmethod
    def _auth_url(
        url: str, username: Optional[str], password: Optional[str]
    ) -> str:
        if not username and not password:
            return url
        try:
            from urllib.parse import quote, urlsplit, urlunsplit

            parts = urlsplit(url)
            if not parts.scheme or not parts.netloc:
                return url  # ssh/scp-style or malformed — leave untouched
            host = parts.netloc.rsplit("@", 1)[-1]
            cred = ""
            if username:
                cred = quote(username, safe="")
            if password:
                cred += ":" + quote(password, safe="")
            netloc = f"{cred}@{host}" if cred else host
            return urlunsplit(
                (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
            )
        except Exception:
            return url

    async def clone(
        self,
        url: str,
        *,
        path: Optional[str] = None,
        branch: Optional[str] = None,
        depth: Optional[int] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> None:
        """Clone ``url`` into ``path`` (or git's default dir) under ``cwd``."""
        args = ["clone"]
        if depth and depth > 0:
            args += ["--depth", str(depth)]
        if branch:
            args += ["--branch", branch]
        args.append(self._auth_url(url, username, password))
        if path:
            args.append(path)
        await self._must(args, cwd)

    async def status(self, cwd: Optional[str] = None) -> GitStatus:
        """Parsed working-tree status (branch, ahead/behind, staged/modified/untracked)."""
        out = await self._must(["status", "--porcelain=v1", "--branch"], cwd)
        st = GitStatus()
        for line in out.split("\n"):
            if not line:
                continue
            if line.startswith("## "):
                head = line[3:]
                if head.startswith("HEAD (no branch)"):
                    st.detached = True
                    continue
                no_commits = re.match(r"^No commits yet on (.+)$", head)
                body = no_commits.group(1) if no_commits else head
                st.branch = body.split("...")[0].split(" ")[0]
                ab = re.search(r"\[(.*)\]", head)
                if ab:
                    a = re.search(r"ahead (\d+)", ab.group(1))
                    b = re.search(r"behind (\d+)", ab.group(1))
                    if a:
                        st.ahead = int(a.group(1))
                    if b:
                        st.behind = int(b.group(1))
                continue
            xy = line[:2]
            p = line[3:]
            if xy == "??":
                st.untracked.append(p)
            else:
                index, work = xy[0], xy[1]
                if index not in (" ", "?"):
                    st.staged.append(p)
                if work not in (" ", "?"):
                    st.modified.append(p)
        st.clean = not (st.staged or st.modified or st.untracked)
        return st

    async def add(self, paths: List[str], cwd: Optional[str] = None) -> None:
        """Stage ``paths`` (use ``["."]`` for everything)."""
        if not paths:
            return
        await self._must(["add", "--", *paths], cwd)

    async def commit(
        self,
        message: str,
        *,
        cwd: Optional[str] = None,
        author: Optional[str] = None,
        email: Optional[str] = None,
        all: bool = False,
    ) -> Dict[str, str]:
        """Commit staged changes; returns ``{"hash": ...}``."""
        args: List[str] = []
        if author:
            args += ["-c", f"user.name={author}"]
        if email:
            args += ["-c", f"user.email={email}"]
        args += ["commit", "-m", message]
        if all:
            args.append("-a")
        await self._must(args, cwd)
        h = (await self._must(["rev-parse", "HEAD"], cwd)).strip()
        return {"hash": h}

    async def _push_pull(
        self,
        op: str,
        *,
        cwd: Optional[str] = None,
        remote: Optional[str] = None,
        branch: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        rem = remote or "origin"
        cfg: List[str] = []
        if username or password:
            plain = (await self._run(["remote", "get-url", rem], cwd)).stdout.strip()
            if plain:
                authed = self._auth_url(plain, username, password)
                if authed != plain:
                    cfg += ["-c", f"url.{authed}.insteadOf={plain}"]
        args = [*cfg, op, rem]
        if branch:
            args.append(branch)
        await self._must(args, cwd)

    async def push(
        self,
        *,
        cwd: Optional[str] = None,
        remote: Optional[str] = None,
        branch: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        """Push to a remote (default ``origin`` + current branch)."""
        await self._push_pull(
            "push", cwd=cwd, remote=remote, branch=branch, username=username, password=password
        )

    async def pull(
        self,
        *,
        cwd: Optional[str] = None,
        remote: Optional[str] = None,
        branch: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        """Pull from a remote (default ``origin`` + current branch)."""
        await self._push_pull(
            "pull", cwd=cwd, remote=remote, branch=branch, username=username, password=password
        )

    async def checkout(
        self, ref: str, *, cwd: Optional[str] = None, create: bool = False
    ) -> None:
        """Check out an existing ref, or create a branch with ``create=True``."""
        args = ["checkout"]
        if create:
            args.append("-b")
        args.append(ref)
        await self._must(args, cwd)

    async def branches(self, cwd: Optional[str] = None) -> "list[GitBranch]":
        """List local branches (name, short commit, whether it's HEAD)."""
        out = await self._must(
            ["branch", "--format=%(HEAD)%1f%(refname:short)%1f%(objectname:short)"],
            cwd,
        )
        branches: List[GitBranch] = []
        for line in out.split("\n"):
            if not line.strip():
                continue
            head, _, rest = line.partition("\x1f")
            name, _, commit = rest.partition("\x1f")
            branches.append(GitBranch(name=name, commit=commit, current=head == "*"))
        return branches

    async def log(
        self, *, cwd: Optional[str] = None, max_count: Optional[int] = None
    ) -> "list[GitCommit]":
        """Recent commits, newest first."""
        args = ["log", "--format=%H%x1f%an%x1f%ae%x1f%aI%x1f%s"]
        if max_count and max_count > 0:
            args.append(f"--max-count={max_count}")
        out = await self._must(args, cwd)
        commits: List[GitCommit] = []
        for line in out.split("\n"):
            if not line.strip():
                continue
            parts = line.split("\x1f")
            while len(parts) < 5:
                parts.append("")
            commits.append(
                GitCommit(
                    hash=parts[0],
                    author=parts[1],
                    email=parts[2],
                    date=parts[3],
                    message=parts[4],
                )
            )
        return commits


def _b64(data: Union[bytes, str]) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return base64.b64encode(raw).decode("ascii")


def _parse_chart(d: Any) -> Optional[Chart]:
    """Deserialize a wire ``chart`` dict into a :class:`Chart` (mirrors the TS
    pass-through). Returns None when absent/malformed."""
    if not isinstance(d, dict):
        return None

    def _axis(a: Any) -> Optional[ChartAxis]:
        if not isinstance(a, dict):
            return None
        return ChartAxis(label=a.get("label"), ticks=a.get("ticks"), scale=a.get("scale"))

    return Chart(
        type=d.get("type", "unknown"),
        title=d.get("title"),
        xLabel=d.get("xLabel"),
        yLabel=d.get("yLabel"),
        x=_axis(d.get("x")),
        y=_axis(d.get("y")),
        elements=d.get("elements"),
    )


__all__ = ["SessionHandle", "SessionConfig", "SessionHooks"]
