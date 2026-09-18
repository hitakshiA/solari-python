"""``VolumeClient`` — SDK <-> Gateway ``/volumes`` HTTP API for durable,
session-independent storage (Part V.4). Mirrors ``sdk/packages/core/src/volume-client.ts``.

A volume is an S3-backed folder your org owns; attach it to a session at create
time and it persists across pause/resume/recreate::

    vol = await pt.volumes.create(name="datasets")
    sbx = await pt.sandboxes.create(
        volumes=[{"volumeId": vol["volumeId"], "path": "/data"}],
    )

CRUD here is metadata-only (create/list/get/delete); the physical mount is
performed host-side before the guest resumes.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from ._http import HttpTransport, new_idempotency_key
from .errors import SolariError


class VolumeClient:
    """Async client for the Solari ``/volumes`` gateway API."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        http: Optional[httpx.AsyncClient] = None,
    ) -> None:
        if not api_key:
            raise SolariError("VolumeClient requires an api_key")
        if not base_url:
            raise SolariError("VolumeClient requires a base_url")
        self._t = HttpTransport(api_key=api_key, base_url=base_url, http=http)

    async def create(
        self,
        *,
        name: Optional[str] = None,
        size_mb: Optional[int] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Create a persistent volume (``POST /volumes``)."""
        body: Dict[str, Any] = {
            "name": name,
            "sizeMb": size_mb,
            "metadata": metadata,
        }
        body = {k: v for k, v in body.items() if v is not None}
        return await self._t.request(
            "POST", "/volumes", body, idempotency_key=new_idempotency_key()
        )

    async def list(self) -> List[Dict[str, Any]]:
        """List this org's volumes, newest first (``GET /volumes``)."""
        data = await self._t.request("GET", "/volumes")
        return (data or {}).get("volumes", [])

    async def get(self, volume_id: str) -> Dict[str, Any]:
        """Get one volume's metadata (``GET /volumes/:id``)."""
        return await self._t.request("GET", f"/volumes/{quote(volume_id, safe='')}")

    async def delete(self, volume_id: str) -> None:
        """Delete a volume's metadata (``DELETE /volumes/:id``; idempotent)."""
        await self._t.request("DELETE", f"/volumes/{quote(volume_id, safe='')}")

    async def aclose(self) -> None:
        await self._t.aclose()

    async def __aenter__(self) -> "VolumeClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()


class SyncVolumeClient:
    """Thin synchronous wrapper over :class:`VolumeClient`."""

    def __init__(self, *, api_key: str, base_url: str) -> None:
        self._inner = VolumeClient(api_key=api_key, base_url=base_url)
        self._loop = asyncio.new_event_loop()

    def _run(self, coro: Any) -> Any:
        return self._loop.run_until_complete(coro)

    def create(self, **kwargs: Any) -> Dict[str, Any]:
        return self._run(self._inner.create(**kwargs))

    def list(self) -> List[Dict[str, Any]]:
        return self._run(self._inner.list())

    def get(self, volume_id: str) -> Dict[str, Any]:
        return self._run(self._inner.get(volume_id))

    def delete(self, volume_id: str) -> None:
        return self._run(self._inner.delete(volume_id))

    def close(self) -> None:
        self._run(self._inner.aclose())
        self._loop.close()

    def __enter__(self) -> "SyncVolumeClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()
