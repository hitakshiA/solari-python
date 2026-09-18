"""Solari Browser client — the async Python port of sdk/src/index.ts."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlsplit, urlunsplit

import httpx

from .errors import BROWSER_UNHEALTHY, SolariError, code_from_body
from .types import (
    DEFAULT_REGION,
    REGION_URLS,
    UNSET,
    Profile,
    ProxyRequest,
    ProxySpec,
    ReplayUrl,
    ResolvedProxyConfig,
    SaveResult,
    Session,
    StorageState,
)

log = logging.getLogger("solari_browser")

DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_BACKOFF_MS = 500
DEFAULT_TIMEOUT_MS = 90_000
_STORAGE_STATE_TIMEOUT_S = 8.0

# Only these are retried. Mirrors `isRetryableStatus` — note this is a FIXED backoff
# and a short attempt count, unlike the desktop SDK's exponential+jitter.
_RETRYABLE_STATUS = frozenset({502, 503, 504})

# Mirrors `isLikelyTransientConnect`: which connect failures are worth relaunching.
_TRANSIENT_CONNECT = re.compile(
    r"ECONNRESET|ECONNREFUSED|ETIMEDOUT|EPIPE|socket hang up|WebSocket|connect|"
    r"handshake|Browser closed|Target page, context or browser has been closed",
    re.IGNORECASE,
)


def derive_cdp_from_ws(ws_endpoint: str) -> str:
    """`/ws/<id>` -> `/cdp/<id>`. Mirrors `deriveCdpFromWs`.

    The gateway usually returns cdpEndpoint explicitly; this is the fallback for
    older gateways. Returns the input unchanged if it isn't a parseable /ws/ URL.
    """
    try:
        parts = urlsplit(ws_endpoint)
        if parts.path.startswith("/ws/"):
            new_path = "/cdp/" + parts.path[len("/ws/") :]
            return urlunsplit((parts.scheme, parts.netloc, new_path, parts.query, parts.fragment))
    except Exception:
        pass
    return ws_endpoint


class _Sessions:
    """`solari.sessions` — mirrors SessionsResource."""

    def __init__(self, client: "Solari") -> None:
        self._client = client

    async def create(
        self,
        *,
        profile_id: Optional[str] = None,
        recording: bool = False,
        stealth: bool = False,
        captcha: bool = False,
        web_bot_auth: bool = False,
        proxy: Optional[ProxySpec] = None,
    ) -> Session:
        """Create a browser session.

        captcha and proxy require stealth=True (enforced gateway-side).
        """
        body: Dict[str, Any] = {}
        if profile_id:
            body["profileId"] = profile_id
        if recording:
            body["recording"] = True
        if stealth:
            body["stealth"] = True
        if captcha:
            body["captcha"] = True
        if web_bot_auth:
            body["webBotAuth"] = True
        if proxy is not None:
            body["proxy"] = proxy.to_wire() if isinstance(proxy, ProxyRequest) else proxy

        # An empty body is sent as no body at all, matching the TS SDK.
        res = await self._client.request("POST", "/sessions", body if body else None)
        if res.status_code >= 400:
            text = res.text
            raise SolariError(
                f"Solari POST /sessions failed: {res.status_code} {text}",
                res.status_code,
                None,
                code_from_body(text),
            )

        data = _json(res, "/sessions")
        session_id = data.get("sessionId")
        ws_endpoint = data.get("wsEndpoint")
        if not session_id or not ws_endpoint:
            raise SolariError(f"Solari: unexpected session response: {json.dumps(data)}")

        cdp_endpoint = data.get("cdpEndpoint") or derive_cdp_from_ws(ws_endpoint)
        expires_at = data.get("expiresAt")
        if not expires_at:
            # Mirrors the TS fallback: assume the plan's 60m default rather than
            # leaving callers with an unusable empty deadline.
            import datetime as _dt

            expires_at = (
                _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=60)
            ).isoformat().replace("+00:00", "Z")

        session = Session(
            id=session_id,
            ws_endpoint=ws_endpoint,
            cdp_endpoint=cdp_endpoint,
            expires_at=expires_at,
        )

        # storage_state is only populated when a profile was requested. UNSET means
        # "no profile attached"; None means "profile exists but is empty".
        if profile_id is not None:
            url_obj = data.get("storageStateUrl")
            if url_obj is not None:
                session.storage_state = await _fetch_presigned_storage_state(url_obj.get("url"))
            else:
                session.storage_state = data.get("storageState")

        if data.get("proxy"):
            session.proxy = ResolvedProxyConfig.from_wire(data["proxy"])
        return session

    async def release(self, session_id: str) -> None:
        """Best-effort release. Never raises — use `release_and_wait` for confirmation."""
        try:
            res = await self._client.request(
                "DELETE", f"/sessions/{_q(session_id)}"
            )
            if res.status_code >= 400 and res.status_code != 404:
                log.warning(
                    "[Solari] DELETE /sessions/%s returned %s: %s",
                    session_id,
                    res.status_code,
                    res.text[:256],
                )
        except Exception as err:  # noqa: BLE001 - fire-and-forget by contract
            log.warning("[Solari] DELETE /sessions/%s failed (best-effort): %s", session_id, err)

    async def release_and_wait(self, session_id: str) -> None:
        """Release and confirm. A 404 is success — the session is already gone."""
        res = await self._client.request("DELETE", f"/sessions/{_q(session_id)}")
        if res.status_code >= 400 and res.status_code != 404:
            raise SolariError(
                f"Solari DELETE /sessions/{session_id} failed: {res.status_code} {res.text}",
                res.status_code,
            )

    async def get(self, session_id: str) -> Dict[str, Any]:
        """Raw session view from the gateway."""
        res = await self._client.request("GET", f"/sessions/{_q(session_id)}")
        if res.status_code >= 400:
            raise SolariError(
                f"Solari GET /sessions/{session_id} failed: {res.status_code} {res.text}",
                res.status_code,
            )
        return _json(res, f"/sessions/{session_id}")

    async def get_replay_url(self, session_id: str) -> ReplayUrl:
        """Presigned replay URL. Available ~1-3s after `release_and_wait`."""
        res = await self._client.request("GET", f"/sessions/{_q(session_id)}/replay-url")
        if res.status_code >= 400:
            raise SolariError(
                f"Solari GET /sessions/{session_id}/replay-url failed: "
                f"{res.status_code} {res.text}",
                res.status_code,
            )
        data = _json(res, "replay-url")
        if not data.get("url"):
            raise SolariError(f"Solari: unexpected replay-url response: {json.dumps(data)}")
        return ReplayUrl(
            url=data["url"],
            expires_in_seconds=data.get("expiresInSeconds") or 0,
            content_encoding=data.get("contentEncoding") or "gzip",
        )

    async def download_replay(self, session_id: str) -> bytes:
        """Download the replay as NDJSON bytes (gzip-encoded per `content_encoding`)."""
        replay = await self.get_replay_url(session_id)
        async with httpx.AsyncClient(timeout=self._client._timeout_s) as http:
            res = await http.get(replay.url)
        if res.status_code >= 400:
            raise SolariError(
                f"Solari: replay download failed: {res.status_code}", res.status_code
            )
        return res.content


class _Profiles:
    """`solari.profiles` — mirrors ProfilesResource."""

    def __init__(self, client: "Solari") -> None:
        self._client = client

    async def create(self, name: str) -> Profile:
        res = await self._client.request("POST", "/profiles", {"name": name})
        if res.status_code >= 400:
            text = res.text
            raise SolariError(
                f"Solari POST /profiles failed: {res.status_code} {text}",
                res.status_code,
                None,
                code_from_body(text),
            )
        return Profile.from_wire(_json(res, "/profiles"))

    async def list(self) -> List[Profile]:
        res = await self._client.request("GET", "/profiles")
        if res.status_code >= 400:
            raise SolariError(
                f"Solari GET /profiles failed: {res.status_code} {res.text}", res.status_code
            )
        data = _json(res, "/profiles")
        items = data if isinstance(data, list) else data.get("profiles", [])
        return [Profile.from_wire(p) for p in items]

    async def delete(self, profile_id: str) -> None:
        """Delete a profile. A 404 is success — it's already gone."""
        res = await self._client.request("DELETE", f"/profiles/{_q(profile_id)}")
        if res.status_code >= 400 and res.status_code != 404:
            raise SolariError(
                f"Solari DELETE /profiles/{profile_id} failed: {res.status_code} {res.text}",
                res.status_code,
            )

    async def save(self, profile_id: str, storage_state: StorageState) -> SaveResult:
        """Persist a Playwright storage state into the profile."""
        res = await self._client.request(
            "POST", f"/profiles/{_q(profile_id)}/save", {"storageState": storage_state}
        )
        if res.status_code >= 400:
            raise SolariError(
                f"Solari POST /profiles/{profile_id}/save failed: {res.status_code} {res.text}",
                res.status_code,
            )
        data = _json(res, "profiles/save")
        return SaveResult(
            version=data.get("version") or 0,
            size_bytes=data.get("sizeBytes") or 0,
        )


class Solari:
    """Solari Browser client.

        solari = Solari(api_key="slr_live_...")
        browser = await solari.launch(stealth=True)
        page = await browser.new_page()
        await page.goto("https://example.com")
        await browser.close()
        await solari.close()

    Or as an async context manager:

        async with Solari(api_key="slr_live_...") as solari:
            async with await solari.launch(stealth=True) as browser:
                ...
    """

    def __init__(
        self,
        api_key: str,
        *,
        region: str = DEFAULT_REGION,
        base_url: Optional[str] = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_ms: int = DEFAULT_BACKOFF_MS,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        if not api_key:
            raise SolariError("Solari: api_key is required")
        region_url = REGION_URLS.get(region)
        if not region_url:
            raise SolariError(
                f'Solari: unsupported region "{region}". '
                f"Supported: {', '.join(REGION_URLS)}."
            )
        self._api_key = api_key
        # base_url wins over region entirely — that's how you point at staging or a
        # self-hosted gateway.
        self._base_url = (base_url or region_url).rstrip("/")
        self._max_attempts = max_attempts
        self._backoff_ms = backoff_ms
        self._timeout_s = timeout_ms / 1000.0

        self.sessions = _Sessions(self)
        self.profiles = _Profiles(self)

        self._http: Optional[httpx.AsyncClient] = None
        self._playwright: Any = None  # lazily started; only for launch()

    @property
    def base_url(self) -> str:
        return self._base_url

    async def __aenter__(self) -> "Solari":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Release the HTTP client and (if launch() was used) the patchright driver."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def request(
        self, method: str, path: str, body: Optional[Any] = None
    ) -> httpx.Response:
        """Authenticated request with the SDK's retry policy.

        Retries ONLY 502/503/504 and transport errors, with a FIXED backoff. A
        non-retryable error status is RETURNED, not raised — callers parse `code`
        out of the body themselves (mirrors the TS SDK).
        """
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout_s)
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = None if body is None else json.dumps(body)

        last_err: Optional[BaseException] = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                res = await self._http.request(
                    method, url, headers=headers, content=payload
                )
                if res.status_code < 400 or res.status_code not in _RETRYABLE_STATUS:
                    return res
                last_err = SolariError(f"Solari {method} {path}: {res.status_code}", res.status_code)
            except Exception as err:  # noqa: BLE001 - all transport errors are retryable
                last_err = err

            if attempt < self._max_attempts:
                await asyncio.sleep(self._backoff_ms / 1000.0)

        raise SolariError(
            f"Solari {method} {path}: exhausted {self._max_attempts} attempts",
            None,
            last_err,
        )

    async def launch(
        self,
        *,
        profile_id: Optional[str] = None,
        recording: bool = False,
        stealth: bool = False,
        captcha: bool = False,
        web_bot_auth: bool = False,
        proxy: Optional[ProxySpec] = None,
        retries: int = 0,
        probe: Optional[bool] = None,
        probe_timeout_ms: int = 2_000,
    ) -> "BrowserSession":
        """Create a session and return a connected browser.

        This is the full-parity path: it connects over the Playwright wire protocol
        via patchright, exactly like the TS SDK. Go/Rust/C++ cannot do this — there
        is no Playwright client for them.
        """
        from .browser_session import BrowserSession  # local import: avoids cycle

        retries = max(0, retries)
        probe_enabled = (retries > 0) if probe is None else probe
        probe_timeout_ms = max(1, probe_timeout_ms)
        total_attempts = retries + 1

        chromium = await self._chromium()

        last_err: Optional[BaseException] = None
        for attempt in range(1, total_attempts + 1):
            session = await self.sessions.create(
                profile_id=profile_id,
                recording=recording,
                stealth=stealth,
                captcha=captcha,
                web_bot_auth=web_bot_auth,
                proxy=proxy,
            )
            browser = None
            try:
                browser = await chromium.connect(session.ws_endpoint)
                if probe_enabled:
                    await self._probe_browser_health(browser, probe_timeout_ms)
                return BrowserSession(self, session, browser)
            except Exception as err:  # noqa: BLE001
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:  # noqa: BLE001
                        pass
                await self.sessions.release(session.id)

                last_err = err
                is_health = isinstance(err, SolariError) and err.code == BROWSER_UNHEALTHY
                transient = (not is_health) and bool(_TRANSIENT_CONNECT.search(str(err)))
                if attempt < total_attempts and (is_health or transient):
                    await asyncio.sleep(0.1 * attempt)
                    continue
                raise

        raise last_err or SolariError("Solari.launch: exhausted attempts")

    async def _chromium(self) -> Any:
        """Start the patchright driver once and reuse it across launches."""
        if self._playwright is None:
            try:
                from patchright.async_api import async_playwright
            except ImportError as err:  # pragma: no cover - depends on install extras
                raise SolariError(
                    "Solari: launch() needs patchright. Install it with "
                    "`pip install 'solari-browser'` (it is a declared dependency) — "
                    "then `patchright install chromium` is NOT required, since the "
                    "browser runs remotely.",
                    None,
                    err,
                ) from err
            self._playwright = await async_playwright().start()
        return self._playwright.chromium

    async def _probe_browser_health(self, browser: Any, timeout_ms: int) -> None:
        """Prove the browser actually works before handing it back.

        A slot can accept the WS connect and still be wedged; without this, launch()
        returns a browser that fails on first use. Any failure is normalised to
        BROWSER_UNHEALTHY so launch()'s retry loop can act on it.
        """

        async def probe() -> None:
            ctx = await browser.new_context()
            try:
                page = await ctx.new_page()
                await page.evaluate("1")
            finally:
                try:
                    await ctx.close()
                except Exception:  # noqa: BLE001
                    pass

        try:
            await asyncio.wait_for(probe(), timeout=timeout_ms / 1000.0)
        except asyncio.TimeoutError as err:
            raise SolariError(
                f"Solari: browser health probe timed out after {timeout_ms}ms",
                None,
                err,
                BROWSER_UNHEALTHY,
            ) from err
        except Exception as err:  # noqa: BLE001
            if isinstance(err, SolariError) and err.code == BROWSER_UNHEALTHY:
                raise
            raise SolariError(
                f"Solari: browser health probe failed: {err}", None, err, BROWSER_UNHEALTHY
            ) from err


def _q(v: str) -> str:
    from urllib.parse import quote

    return quote(v, safe="")


def _json(res: httpx.Response, what: str) -> Any:
    try:
        return res.json()
    except Exception as err:  # noqa: BLE001
        raise SolariError(
            f"Solari: {what} response was not valid JSON: {res.text[:256]}", res.status_code, err
        ) from err


async def _fetch_presigned_storage_state(url: Optional[str]) -> Optional[StorageState]:
    """GET the presigned storage-state object. `None` url means an empty profile."""
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=_STORAGE_STATE_TIMEOUT_S) as http:
            res = await http.get(url)
    except Exception as err:  # noqa: BLE001
        raise SolariError(f"Solari: failed to fetch storageState: {err}", None, err) from err
    if res.status_code >= 400:
        raise SolariError(
            f"Solari: storageState fetch returned {res.status_code}: {res.text[:256]}",
            res.status_code,
        )
    try:
        return res.json()
    except Exception as err:  # noqa: BLE001
        raise SolariError(
            f"Solari: storageState response was not valid JSON: {err}", None, err
        ) from err
