"""``SandboxClient`` — talks the SDK <-> Gateway ``/sandboxes`` + ``/snapshots``
HTTP API (CONTRACTS-V2 §1/§2) and hands back
:class:`~solari_desktop.sandbox.Sandbox` handles. Mirrors
``sdk/src/sandbox-client.ts``.

Async (httpx) with a thin :class:`SyncSandboxClient` wrapper, matching the
desktop client split.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

import httpx

from solari_core._http import HttpTransport, new_idempotency_key
from solari_core.desktop import Desktop, DesktopConfig
from solari_core.errors import SolariError
from solari_core.handle import SessionConfig, SessionHooks
from solari_core.sandbox import Sandbox
from solari_core.volume_client import SyncVolumeClient, VolumeClient
from solari_core.types import (
    CreateDesktopResponse,
    CreateSandboxResponse,
    MetricsResult,
    SandboxKind,
    SandboxState,
    SandboxView,
    SnapshotView,
)


class SandboxClient:
    """Async client for the Solari sandbox gateway."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        http: Optional[httpx.AsyncClient] = None,
        call_timeout_ms: Optional[int] = None,
        kind: SandboxKind = "sandbox",
    ) -> None:
        if not api_key:
            raise SolariError("SandboxClient requires an api_key")
        if not base_url:
            raise SolariError("SandboxClient requires a base_url")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._call_timeout_ms = call_timeout_ms
        self._kind = kind
        self._t = HttpTransport(api_key=api_key, base_url=base_url, http=http)
        #: Persistent volumes CRUD (``/volumes``). Attach via ``create(volumes=...)``.
        self.volumes = VolumeClient(api_key=api_key, base_url=base_url, http=http)

    async def create(
        self,
        *,
        template: Optional[str] = None,
        cpu: Optional[int] = None,
        mem_mb: Optional[int] = None,
        disk_gb: Optional[int] = None,
        envs: Optional[Dict[str, str]] = None,
        metadata: Optional[Dict[str, str]] = None,
        timeout_ms: Optional[int] = None,
        from_snapshot: Optional[str] = None,
        lifecycle: Optional[Dict[str, Any]] = None,
        volumes: Optional[List[Dict[str, str]]] = None,
    ) -> Sandbox:
        """Create a new sandbox (``POST /sandboxes``). ``lifecycle`` is the idle
        policy, e.g. ``{"onTimeout": "pause", "autoResume": True}``. ``volumes``
        attaches persistent volumes, e.g.
        ``[{"volumeId": "vol_x", "path": "/data"}]``."""
        body: Dict[str, Any] = {
            "template": template,
            "kind": self._kind,
            "cpu": cpu,
            "memMb": mem_mb,
            "diskGb": disk_gb,
            "envs": envs,
            "metadata": metadata,
            "timeoutMs": timeout_ms,
            "fromSnapshot": from_snapshot,
            "lifecycle": lifecycle,
            "volumes": volumes,
        }
        # Drop unset fields so we don't send JSON null (TS omits undefined). The
        # gateway rejects e.g. `fromSnapshot: null` ("must be a snapshot id").
        body = {k: v for k, v in body.items() if v is not None}
        data = await self._request(
            "POST", "/sandboxes", body, idempotency_key=new_idempotency_key()
        )
        return Sandbox(_parse_create(data), self._handle_config())

    async def create_desktop(
        self,
        *,
        template: Optional[str] = None,
        cpu: Optional[int] = None,
        mem_mb: Optional[int] = None,
        disk_gb: Optional[int] = None,
        envs: Optional[Dict[str, str]] = None,
        metadata: Optional[Dict[str, str]] = None,
        timeout_ms: Optional[int] = None,
        from_snapshot: Optional[str] = None,
        resolution: Optional[str] = None,
        record: Optional[bool] = None,
        lifecycle: Optional[Dict[str, Any]] = None,
        volumes: Optional[List[Dict[str, str]]] = None,
    ) -> Desktop:
        """Create a GUI desktop via the unified route (``POST /sandboxes`` with
        ``kind:"desktop"``). Mirrors TS ``createDesktop``."""
        body: Dict[str, Any] = {
            "template": template,
            "kind": "desktop",
            "cpu": cpu,
            "memMb": mem_mb,
            "diskGb": disk_gb,
            "envs": envs,
            "metadata": metadata,
            "timeoutMs": timeout_ms,
            "fromSnapshot": from_snapshot,
            "resolution": resolution,
            "record": record,
            "lifecycle": lifecycle,
            "volumes": volumes,
        }
        body = {k: v for k, v in body.items() if v is not None}
        data = await self._request(
            "POST", "/sandboxes", body, idempotency_key=new_idempotency_key()
        )
        base = self._handle_config()
        cfg = DesktopConfig(headers=base.headers, hooks=base.hooks)
        if base.callTimeoutMs is not None:
            cfg.callTimeoutMs = base.callTimeoutMs
        session = CreateDesktopResponse(
            sessionId=data["sandboxId"],
            controlUrl=data["controlUrl"],
            streamUrl=data.get("streamUrl", ""),
            expiresAt=data["expiresAt"],
            # The unified /sandboxes create echoes recordingUrl for a
            # record=True desktop; carry it onto the handle (was dropped here,
            # so Desktop.recordingUrl was always None on this lane).
            recordingUrl=data.get("recordingUrl"),
        )
        return Desktop(session, cfg)

    async def connect(self, sandbox_id: str) -> Sandbox:
        """Re-attach to a running sandbox by id."""
        view = await self.get(sandbox_id)
        origin = self._t.ws_origin()
        session = CreateSandboxResponse(
            sandboxId=view.sandboxId,
            kind=view.kind,
            controlUrl=f"{origin}/control/{quote(sandbox_id, safe='')}",
            expiresAt=view.expiresAt,
        )
        return Sandbox(session, self._handle_config())

    async def get(self, sandbox_id: str) -> SandboxView:
        """``GET /sandboxes/:id``."""
        data = await self._request("GET", f"/sandboxes/{quote(sandbox_id, safe='')}")
        return _parse_view(data)

    async def list(
        self,
        *,
        metadata: Optional[Dict[str, str]] = None,
        state: Optional[SandboxState] = None,
        kind: Optional[SandboxKind] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``GET /sandboxes`` — filter + paginate. Returns ``{sandboxes, nextCursor?}``."""
        params: List[tuple] = []
        for k, v in (metadata or {}).items():
            params.append((f"metadata.{k}", v))
        if state:
            params.append(("state", state))
        if kind:
            params.append(("kind", kind))
        if limit is not None:
            params.append(("limit", str(limit)))
        if cursor:
            params.append(("cursor", cursor))
        qs = ("?" + urlencode(params)) if params else ""
        data = await self._request("GET", f"/sandboxes{qs}")
        return {
            "sandboxes": [_parse_view(s) for s in data.get("sandboxes", [])],
            "nextCursor": data.get("nextCursor"),
        }

    async def list_all(
        self,
        *,
        metadata: Optional[Dict[str, str]] = None,
        state: Optional[SandboxState] = None,
        kind: Optional[SandboxKind] = None,
        limit: Optional[int] = None,
    ):
        """Auto-paginate :meth:`list`: async-iterate every matching sandbox
        across all pages (follows ``nextCursor``). Mirrors TS ``listAll``.

            async for s in sbxs.list_all(state="running"):
                ...
        """
        cursor: Optional[str] = None
        while True:
            page = await self.list(
                metadata=metadata, state=state, kind=kind, limit=limit, cursor=cursor
            )
            for s in page["sandboxes"]:
                yield s
            cursor = page.get("nextCursor")
            if not cursor:
                break

    async def kill(self, sandbox_id: str) -> None:
        """``DELETE /sandboxes/:id``. Idempotent."""
        await self._request("DELETE", f"/sandboxes/{quote(sandbox_id, safe='')}")

    # --- snapshots ------------------------------------------------------------

    async def list_snapshots(
        self,
        *,
        template: Optional[str] = None,
        kind: Optional[SandboxKind] = None,
        limit: Optional[int] = None,
    ) -> List[SnapshotView]:
        """``GET /snapshots``."""
        params: List[tuple] = []
        if template:
            params.append(("template", template))
        if kind:
            params.append(("kind", kind))
        if limit is not None:
            params.append(("limit", str(limit)))
        qs = ("?" + urlencode(params)) if params else ""
        data = await self._request("GET", f"/snapshots{qs}")
        return [_parse_snapshot(s) for s in data.get("snapshots", [])]

    async def get_snapshot(self, snapshot_id: str) -> SnapshotView:
        """``GET /snapshots/:id``."""
        data = await self._request("GET", f"/snapshots/{quote(snapshot_id, safe='')}")
        return _parse_snapshot(data)

    async def delete_snapshot(self, snapshot_id: str) -> None:
        """``DELETE /snapshots/:id`` (refused if it has live children)."""
        await self._request("DELETE", f"/snapshots/{quote(snapshot_id, safe='')}")

    async def promote_snapshot(self, snapshot_id: str, name: str) -> Dict[str, Any]:
        """``POST /snapshots/:id/promote`` → template."""
        return await self._request(
            "POST", f"/snapshots/{quote(snapshot_id, safe='')}/promote", {"name": name}
        )

    async def aclose(self) -> None:
        await self._t.aclose()
        await self.volumes.aclose()

    async def __aenter__(self) -> "SandboxClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    # --- handle wiring --------------------------------------------------------

    def _handle_config(self) -> SessionConfig:
        hooks = SessionHooks(
            metrics=self._hook_metrics,
            snapshot=self._hook_snapshot,
            revert=self._hook_revert,
            pause=self._hook_pause,
            resume=self._hook_resume,
            set_timeout=self._hook_set_timeout,
            download_url=self._hook_download_url,
            upload_url=self._hook_upload_url,
            preview_url=self._hook_preview_url,
            kill=self.kill,
        )
        cfg = SessionConfig(headers=self._t.auth_headers(), hooks=hooks)
        if self._call_timeout_ms is not None:
            cfg.callTimeoutMs = self._call_timeout_ms
        return cfg

    async def _hook_metrics(self, sandbox_id: str) -> MetricsResult:
        d = await self._request("GET", f"/sandboxes/{quote(sandbox_id, safe='')}/metrics")
        return MetricsResult(
            cpuPct=float(d.get("cpuPct", 0)),
            memBytes=int(d.get("memBytes", 0)),
            memTotalBytes=int(d.get("memTotalBytes", 0)),
            diskBytes=int(d.get("diskBytes", 0)),
        )

    async def _hook_snapshot(self, sandbox_id: str, name: Optional[str]) -> str:
        body = {"name": name} if name else {}
        d = await self._request("POST", f"/sandboxes/{quote(sandbox_id, safe='')}/snapshots", body)
        return d["snapshotId"]

    async def _hook_revert(self, sandbox_id: str, snapshot_id: str) -> None:
        await self._request(
            "POST", f"/sandboxes/{quote(sandbox_id, safe='')}/revert", {"snapshotId": snapshot_id}
        )

    async def _hook_pause(self, sandbox_id: str) -> None:
        await self._request("POST", f"/sandboxes/{quote(sandbox_id, safe='')}/pause")

    async def _hook_resume(self, sandbox_id: str) -> str:
        d = await self._request("POST", f"/sandboxes/{quote(sandbox_id, safe='')}/resume")
        origin = self._t.ws_origin()
        return (d or {}).get("controlUrl") or f"{origin}/control/{quote(sandbox_id, safe='')}"

    async def _hook_set_timeout(self, sandbox_id: str, timeout_ms: int) -> Dict[str, Any]:
        return await self._request(
            "POST", f"/sandboxes/{quote(sandbox_id, safe='')}/timeout", {"timeoutMs": timeout_ms}
        )

    async def _hook_download_url(self, sandbox_id: str, path: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/sandboxes/{quote(sandbox_id, safe='')}/files/download-url?path={quote(path, safe='')}",
        )

    async def _hook_upload_url(self, sandbox_id: str, path: Optional[str] = None) -> Dict[str, Any]:
        suffix = f"?path={quote(path, safe='')}" if path else ""
        return await self._request(
            "GET", f"/sandboxes/{quote(sandbox_id, safe='')}/files/upload-url{suffix}"
        )

    async def _hook_preview_url(self, sandbox_id: str, port: int) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/sandboxes/{quote(sandbox_id, safe='')}/ports/{quote(str(port), safe='')}"
        )

    # --- transport ------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        body: Optional[Any] = None,
        *,
        idempotency_key: Optional[str] = None,
    ) -> Any:
        return await self._t.request(method, path, body, idempotency_key=idempotency_key)


class SyncSandboxClient:
    """Thin synchronous wrapper over :class:`SandboxClient`."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        call_timeout_ms: Optional[int] = None,
        kind: SandboxKind = "sandbox",
    ) -> None:
        self._inner = SandboxClient(
            api_key=api_key, base_url=base_url, call_timeout_ms=call_timeout_ms, kind=kind
        )
        #: Persistent volumes CRUD (``/volumes``), sync flavour.
        self.volumes = SyncVolumeClient(api_key=api_key, base_url=base_url)
        self._loop = asyncio.new_event_loop()

    def _run(self, coro: Any) -> Any:
        return self._loop.run_until_complete(coro)

    def create(self, **kwargs: Any) -> Sandbox:
        return self._run(self._inner.create(**kwargs))

    def create_desktop(self, **kwargs: Any) -> Desktop:
        """Create a GUI desktop via the unified ``/sandboxes`` {kind:desktop} route."""
        return self._run(self._inner.create_desktop(**kwargs))

    def connect(self, sandbox_id: str) -> Sandbox:
        return self._run(self._inner.connect(sandbox_id))

    def get(self, sandbox_id: str) -> SandboxView:
        return self._run(self._inner.get(sandbox_id))

    def list(self, **kwargs: Any) -> Dict[str, Any]:
        return self._run(self._inner.list(**kwargs))

    def list_all(self, **kwargs: Any) -> List[SandboxView]:
        """Auto-paginate ``list`` and return every matching sandbox as a list
        (the sync analogue of the async generator)."""
        async def _collect() -> List[SandboxView]:
            return [s async for s in self._inner.list_all(**kwargs)]

        return self._run(_collect())

    def kill(self, sandbox_id: str) -> None:
        return self._run(self._inner.kill(sandbox_id))

    def list_snapshots(self, **kwargs: Any) -> List[SnapshotView]:
        return self._run(self._inner.list_snapshots(**kwargs))

    def get_snapshot(self, snapshot_id: str) -> SnapshotView:
        return self._run(self._inner.get_snapshot(snapshot_id))

    def delete_snapshot(self, snapshot_id: str) -> None:
        return self._run(self._inner.delete_snapshot(snapshot_id))

    def promote_snapshot(self, snapshot_id: str, name: str) -> Dict[str, Any]:
        return self._run(self._inner.promote_snapshot(snapshot_id, name))

    def close(self) -> None:
        self._run(self._inner.aclose())
        self.volumes.close()
        self._loop.close()

    def __enter__(self) -> "SyncSandboxClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def _parse_create(data: Any) -> CreateSandboxResponse:
    return CreateSandboxResponse(
        sandboxId=data["sandboxId"],
        kind=data["kind"],
        controlUrl=data["controlUrl"],
        expiresAt=data["expiresAt"],
        streamUrl=data.get("streamUrl"),
        recordingUrl=data.get("recordingUrl"),
    )


def _parse_view(data: Any) -> SandboxView:
    return SandboxView(
        sandboxId=data["sandboxId"],
        kind=data["kind"],
        state=data["state"],
        metadata=data.get("metadata", {}),
        expiresAt=data["expiresAt"],
        cpu=int(data.get("cpu", 0)),
        memMb=int(data.get("memMb", 0)),
        diskGb=int(data.get("diskGb", 0)),
        recordingUrl=data.get("recordingUrl"),
    )


def _parse_snapshot(data: Any) -> SnapshotView:
    return SnapshotView(
        id=data["id"],
        parent=data.get("parent"),
        name=data.get("name"),
        sizeBytes=int(data.get("sizeBytes", 0)),
        createdAt=data.get("createdAt", ""),
        kind=data.get("kind", "sandbox"),
        template=data.get("template", ""),
    )


__all__ = ["SandboxClient", "SyncSandboxClient"]
