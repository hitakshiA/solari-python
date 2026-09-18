"""``DesktopClient`` — the entry point.

Talks the SDK <-> Gateway HTTP API (CONTRACTS §1) and hands back
:class:`~solari_desktop.desktop.Desktop` handles for the control plane.
Mirrors ``sdk/src/client.ts``.

The API is async (built on :mod:`httpx`). A thin synchronous wrapper,
:class:`SyncDesktopClient`, is provided for callers not running an event loop.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import httpx

from urllib.parse import quote

from solari_core._http import HttpTransport, new_idempotency_key
from solari_core.desktop import Desktop, DesktopConfig
from solari_core.errors import SolariError
from solari_core.handle import SessionHooks
from solari_core.volume_client import SyncVolumeClient, VolumeClient
from solari_core.types import (
    CreateDesktopResponse,
    DeleteDesktopResponse,
    DesktopLifecycleResponse,
    GetDesktopResponse,
    MetricsResult,
)


class DesktopClient:
    """Async client for the Solari Desktop gateway.

    :param api_key: Customer API key, sent as ``Authorization: Bearer <api_key>``.
    :param base_url: Gateway base URL, e.g. ``https://api.getsolari.com``.
    :param http: Optional :class:`httpx.AsyncClient` to reuse (e.g. for tests or
        custom transports). When omitted, one is created lazily and owned by
        this client.
    :param call_timeout_ms: Per-call RPC timeout (ms) passed to created
        :class:`~solari_desktop.desktop.Desktop` handles.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        http: Optional[httpx.AsyncClient] = None,
        call_timeout_ms: Optional[int] = None,
    ) -> None:
        if not api_key:
            raise SolariError("DesktopClient requires an api_key")
        if not base_url:
            raise SolariError("DesktopClient requires a base_url")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._call_timeout_ms = call_timeout_ms
        self._t = HttpTransport(api_key=api_key, base_url=base_url, http=http)
        #: Persistent volumes CRUD (``/volumes``). Attach via ``create(volumes=...)``.
        self.volumes = VolumeClient(api_key=api_key, base_url=base_url, http=http)

    async def create(
        self,
        *,
        template: str = "default",
        ttl_seconds: Optional[int] = None,
        resolution: Optional[str] = None,
        cpu: Optional[int] = None,
        mem_mb: Optional[int] = None,
        metadata: Optional[Dict[str, str]] = None,
        record: Optional[bool] = None,
        timeout_ms: Optional[int] = None,
        lifecycle: Optional[Dict[str, Any]] = None,
        volumes: Optional[list] = None,
    ) -> Desktop:
        """Create a new desktop session (``POST /desktops``).

        Pass ``record=True`` to record the session; the resulting
        :class:`Desktop` carries a presigned ``recordingUrl`` for playback
        (populated once the guest uploads the mp4 on ``record.stop()``).
        ``lifecycle`` is the idle policy, e.g. ``{"onTimeout": "pause",
        "autoResume": True}`` (wire keys are camelCase). ``timeout_ms`` sets the
        rolling idle window (resets on every use), overriding ``ttl_seconds``
        and the 30-minute desktop default. ``cpu`` (1–16) and ``mem_mb``
        (2048–65536) size the desktop; the host grows a small warm clone to that
        size on assign via live CH resize.
        """
        body: Dict[str, Any] = {
            "template": template,
            "ttlSeconds": ttl_seconds,
            "resolution": resolution,
            "cpu": cpu,
            "memMb": mem_mb,
            "metadata": metadata,
            "record": record,
            "timeoutMs": timeout_ms,
            "lifecycle": lifecycle,
            "volumes": volumes,
        }
        # Drop unset fields so we don't wire JSON null (TS omits undefined) — the
        # gateway treats a null timeoutMs/ttlSeconds as "not provided" only when
        # the key is absent.
        body = {k: v for k, v in body.items() if v is not None}
        data = await self._request(
            "POST", "/desktops", body, idempotency_key=new_idempotency_key()
        )
        return Desktop(_parse_create_response(data), self._desktop_config())

    async def get(self, session_id: str) -> GetDesktopResponse:
        """Fetch the current status of a session (``GET /desktops/:sessionId``).

        Returns the raw status record (not a :class:`Desktop` handle, since the
        gateway does not re-issue control/stream URLs here).
        """
        data = await self._request(
            "GET", f"/desktops/{_encode(session_id)}"
        )
        return GetDesktopResponse(
            sessionId=data["sessionId"],
            status=data["status"],
            expiresAt=data["expiresAt"],
            recordingUrl=data.get("recordingUrl"),
        )

    async def destroy(self, session_id: str) -> DeleteDesktopResponse:
        """Destroy a session (``DELETE /desktops/:sessionId``). Idempotent."""
        data = await self._request(
            "DELETE", f"/desktops/{_encode(session_id)}"
        )
        ok = True if not isinstance(data, dict) else bool(data.get("ok", True))
        return DeleteDesktopResponse(ok=ok)

    async def pause(self, session_id: str) -> DesktopLifecycleResponse:
        """Pause a running session (``POST /desktops/:sessionId/pause``).

        The host snapshots the full RAM+disk state and frees the host slot. The
        session can be resumed later with :meth:`resume`. Prefer
        :meth:`Desktop.pause` when you hold a live handle.
        """
        data = await self._request(
            "POST", f"/desktops/{_encode(session_id)}/pause"
        )
        return _parse_lifecycle(data, session_id, "paused")

    async def resume(self, session_id: str) -> Desktop:
        """Resume a paused session (``POST /desktops/:sessionId/resume``).

        The host provisions a fresh slot and restores the full RAM+disk state.
        Returns a new :class:`Desktop` handle re-using the original session URLs
        (control/stream routing follows the session, not the underlying slot).
        """
        return await self._resume_with(session_id, None)

    async def _resume_with(
        self, session_id: str, recording_url: Optional[str]
    ) -> Desktop:
        """``resume`` + the playback URL when the caller already knows it.

        ``POST /resume`` does not echo ``recordingUrl``, so a bare :meth:`resume`
        leaves it ``None`` rather than spend a ``GET`` to find it; :meth:`connect`
        has already fetched the status record and threads it through so a
        connected handle exposes the same ``recordingUrl`` whether the session was
        paused or ready.
        """
        await self._request("POST", f"/desktops/{_encode(session_id)}/resume")
        origin = self._t.ws_origin()
        enc = _encode(session_id)
        session = CreateDesktopResponse(
            sessionId=session_id,
            streamUrl=f"{origin}/stream/{enc}",
            controlUrl=f"{origin}/control/{enc}",
            expiresAt="",
            recordingUrl=recording_url,
        )
        return Desktop(session, self._desktop_config())

    async def connect(self, session_id: str) -> Desktop:
        """Canonical re-attach by session id: looks up the session and, if it is
        paused, resumes it first (e2b-style ``connect``), then returns a live
        :class:`Desktop`. ``resume`` (always resumes) and ``attach`` (from a
        saved create response) remain for their explicit cases.
        """
        status = await self.get(session_id)
        if status.status == "paused":
            return await self._resume_with(session_id, status.recordingUrl)
        origin = self._t.ws_origin()
        enc = _encode(session_id)
        session = CreateDesktopResponse(
            sessionId=session_id,
            streamUrl=f"{origin}/stream/{enc}",
            controlUrl=f"{origin}/control/{enc}",
            expiresAt=status.expiresAt,
            # GET /desktops/:id carries the playback URL for a recorded session,
            # so a connect()-ed handle exposes it just like a create()-ed one.
            recordingUrl=status.recordingUrl,
        )
        return Desktop(session, self._desktop_config())

    def attach(self, session: CreateDesktopResponse) -> Desktop:
        """Re-attach to an existing session given the URLs returned at create time.

        Handy when persisting a session across processes.
        """
        return Desktop(session, self._desktop_config())

    async def aclose(self) -> None:
        """Close the underlying HTTP client (only if this client owns it)."""
        await self._t.aclose()
        await self.volumes.aclose()

    async def __aenter__(self) -> "DesktopClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    # --- internals ------------------------------------------------------------

    def _desktop_config(self) -> DesktopConfig:
        cfg = DesktopConfig(
            headers=self._t.auth_headers(),
            hooks=self._hooks(),
        )
        if self._call_timeout_ms is not None:
            cfg.callTimeoutMs = self._call_timeout_ms
        return cfg

    def _hooks(self) -> SessionHooks:
        """Full lifecycle/admin wiring for a :class:`Desktop` handle.

        Points at the canonical ``/sandboxes/:id/*`` routes — valid because the
        consolidated gateway writes a ``SandboxRecord`` for every desktop, so a
        desktop id resolves there. This gives ``desktop.pause()/resume()/
        set_timeout()/metrics()/snapshot()/…`` the same behavior as a sandbox.
        The explicit :meth:`pause`/:meth:`resume`/:meth:`get`/:meth:`destroy`
        methods stay on ``/desktops/:id/*`` for v1 back-compat.
        """
        return SessionHooks(
            metrics=self._hook_metrics,
            snapshot=self._hook_snapshot,
            revert=self._hook_revert,
            pause=self._hook_pause,
            resume=self._hook_resume,
            set_timeout=self._hook_set_timeout,
            download_url=self._hook_download_url,
            upload_url=self._hook_upload_url,
            preview_url=self._hook_preview_url,
            kill=self._hook_kill,
        )

    async def _hook_metrics(self, session_id: str) -> MetricsResult:
        d = await self._request("GET", f"/sandboxes/{_encode(session_id)}/metrics")
        return MetricsResult(
            cpuPct=float(d.get("cpuPct", 0)),
            memBytes=int(d.get("memBytes", 0)),
            memTotalBytes=int(d.get("memTotalBytes", 0)),
            diskBytes=int(d.get("diskBytes", 0)),
        )

    async def _hook_snapshot(self, session_id: str, name: Optional[str]) -> str:
        body = {"name": name} if name else {}
        d = await self._request("POST", f"/sandboxes/{_encode(session_id)}/snapshots", body)
        return d["snapshotId"]

    async def _hook_revert(self, session_id: str, snapshot_id: str) -> None:
        await self._request(
            "POST", f"/sandboxes/{_encode(session_id)}/revert", {"snapshotId": snapshot_id}
        )

    async def _hook_pause(self, session_id: str) -> None:
        await self._request("POST", f"/sandboxes/{_encode(session_id)}/pause")

    async def _hook_resume(self, session_id: str) -> str:
        d = await self._request("POST", f"/sandboxes/{_encode(session_id)}/resume")
        origin = self._t.ws_origin()
        return (d or {}).get("controlUrl") or f"{origin}/control/{_encode(session_id)}"

    async def _hook_set_timeout(self, session_id: str, timeout_ms: int) -> Dict[str, Any]:
        return await self._request(
            "POST", f"/sandboxes/{_encode(session_id)}/timeout", {"timeoutMs": timeout_ms}
        )

    async def _hook_download_url(self, session_id: str, path: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/sandboxes/{_encode(session_id)}/files/download-url?path={quote(path, safe='')}",
        )

    async def _hook_upload_url(self, session_id: str, path: Optional[str] = None) -> Dict[str, Any]:
        suffix = f"?path={quote(path, safe='')}" if path else ""
        return await self._request(
            "GET", f"/sandboxes/{_encode(session_id)}/files/upload-url{suffix}"
        )

    async def _hook_preview_url(self, session_id: str, port: int) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/sandboxes/{_encode(session_id)}/ports/{_encode(str(port))}"
        )

    async def _hook_kill(self, session_id: str) -> None:
        # Unified path (mirrors TS): handle lifecycle hooks all go through
        # /sandboxes/:id; explicit DesktopClient.destroy() keeps /desktops/:id.
        await self._request("DELETE", f"/sandboxes/{_encode(session_id)}")

    async def _request(
        self,
        method: str,
        path: str,
        body: Optional[Any] = None,
        *,
        idempotency_key: Optional[str] = None,
    ) -> Any:
        return await self._t.request(method, path, body, idempotency_key=idempotency_key)


class SyncDesktopClient:
    """Thin synchronous wrapper over :class:`DesktopClient`.

    Each method drives the async client to completion on a private event loop.
    Provided for callers not already running asyncio; the returned
    :class:`~solari_desktop.desktop.Desktop` handle is async, so prefer
    :class:`DesktopClient` when driving the control channel.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        call_timeout_ms: Optional[int] = None,
    ) -> None:
        self._inner = DesktopClient(
            api_key=api_key, base_url=base_url, call_timeout_ms=call_timeout_ms
        )
        #: Persistent volumes CRUD (``/volumes``), sync flavour.
        self.volumes = SyncVolumeClient(api_key=api_key, base_url=base_url)
        self._loop = asyncio.new_event_loop()

    def _run(self, coro: Any) -> Any:
        return self._loop.run_until_complete(coro)

    def create(
        self,
        *,
        template: str = "default",
        ttl_seconds: Optional[int] = None,
        resolution: Optional[str] = None,
        cpu: Optional[int] = None,
        mem_mb: Optional[int] = None,
        metadata: Optional[Dict[str, str]] = None,
        record: Optional[bool] = None,
        timeout_ms: Optional[int] = None,
        lifecycle: Optional[Dict[str, Any]] = None,
        volumes: Optional[list] = None,
    ) -> Desktop:
        """Create a new desktop session (``POST /desktops``)."""
        return self._run(
            self._inner.create(
                template=template,
                ttl_seconds=ttl_seconds,
                resolution=resolution,
                cpu=cpu,
                mem_mb=mem_mb,
                metadata=metadata,
                record=record,
                timeout_ms=timeout_ms,
                lifecycle=lifecycle,
                volumes=volumes,
            )
        )

    def get(self, session_id: str) -> GetDesktopResponse:
        """Fetch the current status of a session (``GET /desktops/:sessionId``)."""
        return self._run(self._inner.get(session_id))

    def destroy(self, session_id: str) -> DeleteDesktopResponse:
        """Destroy a session (``DELETE /desktops/:sessionId``). Idempotent."""
        return self._run(self._inner.destroy(session_id))

    def pause(self, session_id: str) -> DesktopLifecycleResponse:
        """Pause a running session (``POST /desktops/:sessionId/pause``)."""
        return self._run(self._inner.pause(session_id))

    def resume(self, session_id: str) -> Desktop:
        """Resume a paused session (``POST /desktops/:sessionId/resume``)."""
        return self._run(self._inner.resume(session_id))

    def connect(self, session_id: str) -> Desktop:
        """Canonical re-attach by id; auto-resumes if the session is paused."""
        return self._run(self._inner.connect(session_id))

    def attach(self, session: CreateDesktopResponse) -> Desktop:
        """Re-attach to an existing session given the URLs returned at create time."""
        return self._inner.attach(session)

    def close(self) -> None:
        """Close the underlying HTTP client and event loop."""
        self._run(self._inner.aclose())
        self.volumes.close()
        self._loop.close()

    def __enter__(self) -> "SyncDesktopClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def _encode(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def _parse_lifecycle(
    data: Any, session_id: str, default_status: str
) -> DesktopLifecycleResponse:
    if isinstance(data, dict):
        return DesktopLifecycleResponse(
            sessionId=data.get("sessionId", session_id),
            status=data.get("status", default_status),
        )
    return DesktopLifecycleResponse(sessionId=session_id, status=default_status)


def _parse_create_response(data: Any) -> CreateDesktopResponse:
    return CreateDesktopResponse(
        sessionId=data["sessionId"],
        streamUrl=data["streamUrl"],
        controlUrl=data["controlUrl"],
        expiresAt=data["expiresAt"],
        recordingUrl=data.get("recordingUrl"),
    )


__all__ = ["DesktopClient", "SyncDesktopClient"]
