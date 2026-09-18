"""Wire types for the Solari Browser SDK. Mirrors the interfaces in sdk/src/index.ts.

Dataclasses (not TypedDicts) so callers get attribute access + IDE completion, with
`from_wire` classmethods doing the camelCase→snake_case mapping in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Union

# Supported regions. More coming soon. Mirrors `SolariRegion`.
Region = Literal["us-west"]
REGION_URLS: Dict[str, str] = {
    "us-west": "https://api.getsolari.com",
}
DEFAULT_REGION: str = "us-west"

ProxyTier = Literal["residential", "static", "mobile"]

# A Playwright storage state (cookies + origins/localStorage). Left as a plain dict:
# it is passed straight through to patchright's `storage_state`, so imposing our own
# schema would only risk drifting from Playwright's.
StorageState = Dict[str, Any]


@dataclass
class ProxyRequest:
    """Managed proxy egress request. Mirrors `ProxyRequest`."""

    country: Optional[str] = None  # ISO-3166-1 alpha-2, lowercase. Defaults to "us".
    tier: Optional[ProxyTier] = None  # residential (default, rotating) | static | mobile
    asn: Optional[str] = None  # pin egress ASN, e.g. "20057" (AT&T Mobility)
    session: Optional[str] = None  # sticky id, alnum + dash, <=32 chars
    session_duration: Optional[int] = None  # sticky lifetime, minutes (1-30, default 10)
    state: Optional[str] = None  # US-only, e.g. "california"
    city: Optional[str] = None  # US-only, e.g. "los_angeles"

    def to_wire(self) -> Dict[str, Any]:
        """Only non-None keys are sent — the gateway distinguishes absent from null."""
        wire: Dict[str, Any] = {}
        if self.country is not None:
            wire["country"] = self.country
        if self.tier is not None:
            wire["tier"] = self.tier
        if self.asn is not None:
            wire["asn"] = self.asn
        if self.session is not None:
            wire["session"] = self.session
        if self.session_duration is not None:
            wire["sessionDuration"] = self.session_duration
        if self.state is not None:
            wire["state"] = self.state
        if self.city is not None:
            wire["city"] = self.city
        return wire


# `proxy=` accepts the same shapes as the TS SDK: a country string ("us"), a
# ProxyRequest, or the "off"/"smart" sentinels.
ProxySpec = Union[str, ProxyRequest]


@dataclass
class ResolvedProxyConfig:
    """Coarse confirmation of the proxy the gateway resolved for a session.

    Carries NO credentials by design: egress is applied server-side (the pool
    attaches the upstream to your session), so a caller never dials the proxy
    and never needs its address or account.
    """

    timezone_id: str
    country: str
    tier: Optional[ProxyTier] = None

    @classmethod
    def from_wire(cls, d: Dict[str, Any]) -> "ResolvedProxyConfig":
        return cls(
            timezone_id=d.get("timezoneId", ""),
            country=d.get("country", ""),
            tier=d.get("tier"),
        )


# Sentinel distinguishing "no profile attached" from "profile exists but empty".
# The TS SDK encodes this as undefined vs null on `Session.storageState`; Python has
# only None, so absence is this sentinel and empty-profile is None.
class _Unset:
    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET = _Unset()


@dataclass
class Session:
    """A live browser session.

    NOTE: unlike the TS SDK, `ws_endpoint`/`cdp_endpoint` are the UPSTREAM gateway
    URLs. The TS SDK rewrites them to a loopback LocalProxy for connect-retry
    purposes; that is a Node-specific device and is deliberately not reproduced here.
    """

    id: str
    ws_endpoint: str  # Playwright wire protocol — for chromium.connect()
    cdp_endpoint: str  # raw CDP — for connect_over_cdp() / other CDP clients
    expires_at: str  # ISO 8601 UTC; session auto-releases at this point
    storage_state: Union[StorageState, None, _Unset] = UNSET
    proxy: Optional[ResolvedProxyConfig] = None


@dataclass
class Profile:
    """A stored browser profile (cookies + localStorage)."""

    id: str
    name: str
    raw: Dict[str, Any] = field(default_factory=dict)  # full wire object, forward-compatible

    @classmethod
    def from_wire(cls, d: Dict[str, Any]) -> "Profile":
        return cls(id=d.get("id", ""), name=d.get("name", ""), raw=d)


@dataclass
class ReplayUrl:
    url: str
    expires_in_seconds: int = 0
    content_encoding: str = "gzip"


@dataclass
class SaveResult:
    version: int = 0
    size_bytes: int = 0
