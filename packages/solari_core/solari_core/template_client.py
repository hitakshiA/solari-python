"""``TemplateClient`` — SDK <-> Gateway ``/templates`` HTTP API. Turns a
declarative :class:`~solari_desktop.image.Image` into a reusable template a
session can launch. Mirrors ``sdk/src/template-client.ts``.

::

    img = Image.base("ubuntu:22.04").apt_install(["ffmpeg"]).kind("sandbox")
    tpl = await pt.templates.build(img, name="media")
    sbx = await pt.sandboxes.create(template=tpl["templateId"])

``build()`` POSTs the compiled recipe (async build on the gateway) then polls to
completion. See docs/TEMPLATE-BUILD-PIPELINE.md.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import quote

import httpx

from ._http import HttpTransport, new_idempotency_key
from .errors import SolariError
from .image import Image

DEFAULT_BUILD_TIMEOUT_MS = 900_000  # 15 min
DEFAULT_POLL_INTERVAL_MS = 3_000


def _compiled_dict(image: Any) -> Dict[str, Any]:
    """Normalize an Image | CompiledImage | dict to the wire dict."""
    if isinstance(image, Image):
        image = image.compile()
    if is_dataclass(image) and not isinstance(image, type):
        d = asdict(image)
        # Drop unset optionals so we don't emit JSON null (TS omits undefined).
        return {k: v for k, v in d.items() if v is not None}
    if isinstance(image, dict):
        return image
    raise SolariError("templates.build requires an Image, CompiledImage, or dict")


class TemplateClient:
    """Async client for the Solari ``/templates`` gateway API."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        http: Optional[httpx.AsyncClient] = None,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    ) -> None:
        if not api_key:
            raise SolariError("TemplateClient requires an api_key")
        if not base_url:
            raise SolariError("TemplateClient requires a base_url")
        self._sleep = sleep or (lambda ms: asyncio.sleep(ms / 1000.0))
        self._t = HttpTransport(api_key=api_key, base_url=base_url, http=http)

    async def build(
        self,
        image: Any,
        *,
        name: str,
        kind: Optional[str] = None,
        cpu: Optional[int] = None,
        mem_mb: Optional[int] = None,
        timeout_ms: int = DEFAULT_BUILD_TIMEOUT_MS,
        poll_interval_ms: int = DEFAULT_POLL_INTERVAL_MS,
    ) -> Dict[str, Any]:
        """Build a custom template and wait until it is ``ready`` (raises on
        ``failed``/timeout). Returns the final template view dict."""
        res = await self.build_raw(image, name=name, kind=kind, cpu=cpu, mem_mb=mem_mb)
        return await self.wait_until_ready(
            res["templateId"], timeout_ms=timeout_ms, poll_interval_ms=poll_interval_ms
        )

    async def build_raw(
        self,
        image: Any,
        *,
        name: str,
        kind: Optional[str] = None,
        cpu: Optional[int] = None,
        mem_mb: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Start a build without waiting (``POST /templates``). Returns ids."""
        if not name:
            raise SolariError("templates.build requires a name")
        compiled = _compiled_dict(image)
        body: Dict[str, Any] = {
            "name": name,
            "kind": kind or compiled.get("kind", "sandbox"),
            "cpu": cpu,
            "memMb": mem_mb,
            "compiled": compiled,
        }
        body = {k: v for k, v in body.items() if v is not None}
        return await self._request(
            "POST", "/templates", body, idempotency_key=new_idempotency_key()
        )

    async def list(self) -> List[Dict[str, Any]]:
        """List templates — built-ins + this org's custom templates."""
        data = await self._request("GET", "/templates")
        return (data or {}).get("templates", [])

    async def get(self, template_id: str) -> Dict[str, Any]:
        """Get one template's status/metadata (``GET /templates/:id``)."""
        return await self._request("GET", f"/templates/{quote(template_id, safe='')}")

    async def delete(self, template_id: str) -> None:
        """Delete a custom template (refused while live sessions depend on it)."""
        await self._request("DELETE", f"/templates/{quote(template_id, safe='')}")

    async def wait_until_ready(
        self,
        template_id: str,
        *,
        timeout_ms: int = DEFAULT_BUILD_TIMEOUT_MS,
        poll_interval_ms: int = DEFAULT_POLL_INTERVAL_MS,
    ) -> Dict[str, Any]:
        """Poll until the template leaves ``building``. Resolves on ``ready``,
        raises on ``failed`` or timeout."""
        waited = 0
        while True:
            view = await self.get(template_id)
            status = view.get("status")
            if status == "ready":
                return view
            if status == "failed":
                raise SolariError(
                    f"template build failed for {template_id}: {view.get('error', 'unknown error')}"
                )
            if waited >= timeout_ms:
                raise SolariError(
                    f"template build for {template_id} did not complete within {timeout_ms}ms"
                )
            await self._sleep(poll_interval_ms)
            waited += poll_interval_ms

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

    async def aclose(self) -> None:
        await self._t.aclose()

    async def __aenter__(self) -> "TemplateClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()


class SyncTemplateClient:
    """Thin synchronous wrapper over :class:`TemplateClient`."""

    def __init__(self, *, api_key: str, base_url: str) -> None:
        self._inner = TemplateClient(api_key=api_key, base_url=base_url)
        self._loop = asyncio.new_event_loop()

    def _run(self, coro: Any) -> Any:
        return self._loop.run_until_complete(coro)

    def build(self, image: Any, **kwargs: Any) -> Dict[str, Any]:
        return self._run(self._inner.build(image, **kwargs))

    def build_raw(self, image: Any, **kwargs: Any) -> Dict[str, Any]:
        return self._run(self._inner.build_raw(image, **kwargs))

    def list(self) -> List[Dict[str, Any]]:
        return self._run(self._inner.list())

    def get(self, template_id: str) -> Dict[str, Any]:
        return self._run(self._inner.get(template_id))

    def delete(self, template_id: str) -> None:
        return self._run(self._inner.delete(template_id))

    def wait_until_ready(self, template_id: str, **kwargs: Any) -> Dict[str, Any]:
        return self._run(self._inner.wait_until_ready(template_id, **kwargs))

    def close(self) -> None:
        self._run(self._inner.aclose())
        self._loop.close()

    def __enter__(self) -> "SyncTemplateClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()
