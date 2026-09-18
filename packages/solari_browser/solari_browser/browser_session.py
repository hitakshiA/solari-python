"""BrowserSession — a connected browser + its Solari session. Mirrors sdk/src/browser-session.ts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional

from .types import ResolvedProxyConfig, Session

if TYPE_CHECKING:  # pragma: no cover
    from .client import Solari


class BrowserSession:
    """Wraps a patchright `Browser` and the Solari session backing it.

    `close()` closes the browser AND releases the session — a browser close alone
    would leave the slot held until its plan deadline.
    """

    def __init__(self, client: "Solari", session: Session, browser: Any) -> None:
        self._client = client
        self._session = session
        self._browser = browser
        self._closed = False

    @property
    def id(self) -> str:
        return self._session.id

    @property
    def expires_at(self) -> str:
        """ISO 8601 UTC deadline; the session auto-releases at this point."""
        return self._session.expires_at

    @property
    def proxy(self) -> Optional[ResolvedProxyConfig]:
        return self._session.proxy

    @property
    def ws_endpoint(self) -> str:
        """Upstream Playwright wire-protocol endpoint."""
        return self._session.ws_endpoint

    @property
    def cdp_endpoint(self) -> str:
        """Upstream raw-CDP endpoint.

        NOTE: driving the browser over raw CDP bypasses the pool's Playwright-path
        input humanization (see CLAUDE.md) — relevant if you care about stealth.
        """
        return self._session.cdp_endpoint

    @property
    def session(self) -> Session:
        return self._session

    @property
    def raw(self) -> Any:
        """The underlying patchright `Browser`."""
        return self._browser

    def is_connected(self) -> bool:
        return bool(self._browser.is_connected())

    @property
    def version(self) -> str:
        return str(self._browser.version)

    def contexts(self) -> List[Any]:
        """Existing browser contexts. The default context is `contexts()[0]`."""
        return list(self._browser.contexts)

    async def new_context(self, **kwargs: Any) -> Any:
        return await self._browser.new_context(**kwargs)

    async def new_page(self) -> Any:
        return await self._browser.new_page()

    async def close(self) -> None:
        """Close the browser and release the session. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._browser.close()
        except Exception:  # noqa: BLE001 - releasing the slot matters more
            pass
        await self._client.sessions.release_and_wait(self._session.id)

    async def __aenter__(self) -> "BrowserSession":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()
