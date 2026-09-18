"""Solari Browser — Python SDK.

Managed, stealthy remote Chromium over the Playwright wire protocol / raw CDP.

    import asyncio
    from solari_browser import Solari

    async def main():
        async with Solari(api_key="slr_live_...") as solari:
            async with await solari.launch(stealth=True) as browser:
                page = await browser.new_page()
                await page.goto("https://example.com")
                print(await page.title())

    asyncio.run(main())
"""

from .browser_session import BrowserSession
from .client import Solari, derive_cdp_from_ws
from .errors import (
    BROWSER_UNHEALTHY,
    CONCURRENCY_LIMIT_EXCEEDED,
    FEATURE_REQUIRES_PLAN,
    PLAN_LIMIT_EXCEEDED,
    STALE_OBSERVATION,
    SolariError,
    StaleObservationError,
)
from .observation import ActKind, Observation, ObservedElement, SelectOption, format_observation
from .observe import OBSERVER_VERSION, Observer
from .types import (
    DEFAULT_REGION,
    REGION_URLS,
    UNSET,
    Profile,
    ProxyRequest,
    ReplayUrl,
    ResolvedProxyConfig,
    SaveResult,
    Session,
)

__version__ = "0.1.2"

__all__ = [
    "Solari",
    "BrowserSession",
    "SolariError",
    "Session",
    "Profile",
    "ProxyRequest",
    "ResolvedProxyConfig",
    "ReplayUrl",
    "SaveResult",
    "UNSET",
    "REGION_URLS",
    "DEFAULT_REGION",
    "derive_cdp_from_ws",
    "FEATURE_REQUIRES_PLAN",
    "CONCURRENCY_LIMIT_EXCEEDED",
    "PLAN_LIMIT_EXCEEDED",
    "BROWSER_UNHEALTHY",
    # solari-python fork: fast observe/act
    "Observer",
    "Observation",
    "ObservedElement",
    "SelectOption",
    "ActKind",
    "format_observation",
    "OBSERVER_VERSION",
    "StaleObservationError",
    "STALE_OBSERVATION",
    "__version__",
]
