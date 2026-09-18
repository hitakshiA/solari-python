"""Shared async HTTP transport (mirrors the TypeScript ``src/http.ts``).

Both :class:`DesktopClient` and :class:`SandboxClient` delegate here so there is
ONE place owning auth headers, error mapping, retries/backoff, idempotency keys,
and timeouts.

Retry policy: idempotent requests (GET, DELETE, or any carrying an
Idempotency-Key) are retried on network errors, HTTP 5xx, and bodies flagged
``retryable``, with exponential backoff + jitter. Non-idempotent writes are
never silently retried — pass ``idempotency_key`` to opt a create into safe
retries. 429 is NOT retried (it is our ConcurrencyLimitError).
"""
from __future__ import annotations

import asyncio
import random
import uuid
from typing import Any, Dict, Optional

import httpx

from .errors import ConnectionError as PtConnectionError
from .errors import SolariError, map_gateway_error
from .types import GatewayErrorBody


def new_idempotency_key() -> str:
    """A fresh idempotency key (UUID)."""
    return str(uuid.uuid4())


class HttpTransport:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        http: Optional[httpx.AsyncClient] = None,
        max_retries: int = 5,
        request_timeout_ms: int = 300_000,
        retry_delay_ms: Optional[int] = None,
    ) -> None:
        if not api_key:
            raise SolariError("HttpTransport requires an api_key")
        if not base_url:
            raise SolariError("HttpTransport requires a base_url")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._http = http
        self._owns_http = http is None
        self._max_retries = max_retries
        self._timeout = request_timeout_ms / 1000.0
        self._retry_delay_ms = retry_delay_ms

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient()
        return self._http

    def auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def ws_origin(self) -> str:
        if self._base_url.startswith("https"):
            return "wss" + self._base_url[len("https"):]
        if self._base_url.startswith("http"):
            return "ws" + self._base_url[len("http"):]
        return self._base_url

    async def request(
        self,
        method: str,
        path: str,
        body: Optional[Any] = None,
        *,
        idempotency_key: Optional[str] = None,
    ) -> Any:
        idempotent = method in ("GET", "DELETE") or idempotency_key is not None
        headers: Dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        attempt = 0
        while True:
            try:
                res = await self._client().request(
                    method,
                    f"{self._base_url}{path}",
                    headers=headers,
                    json=body if body is not None else None,
                    timeout=self._timeout,
                )
            except httpx.HTTPError as exc:
                if idempotent and attempt < self._max_retries:
                    await asyncio.sleep(self._backoff(attempt))
                    attempt += 1
                    continue
                raise PtConnectionError(f"{method} {path} failed: {exc}") from exc

            if res.is_error:
                err_body: Optional[GatewayErrorBody] = None
                try:
                    parsed = res.json()
                    if isinstance(parsed, dict):
                        err_body = GatewayErrorBody(
                            code=parsed.get("code"),
                            error=parsed.get("error"),
                            message=parsed.get("message"),
                            retryable=parsed.get("retryable"),
                        )
                except Exception:  # noqa: BLE001 - body may be empty/non-JSON
                    err_body = None
                # 5xx or explicit retryable hint; NOT 429 (ConcurrencyLimitError).
                retryable = res.status_code >= 500 or (
                    err_body is not None and err_body.retryable is True
                )
                if idempotent and retryable and attempt < self._max_retries:
                    await asyncio.sleep(self._backoff(attempt))
                    attempt += 1
                    continue
                raise map_gateway_error(res.status_code, err_body)

            if not res.content:
                return None
            return res.json()

    def _backoff(self, attempt: int) -> float:
        if self._retry_delay_ms is not None:
            return self._retry_delay_ms / 1000.0
        # Base 150ms (was 1000): a create bounced by a transient host no_capacity
        # during a burst re-picks a different host in ~150ms, not a full second.
        return min(0.15 * 2 ** attempt, 8.0) + random.random() * 0.25

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
