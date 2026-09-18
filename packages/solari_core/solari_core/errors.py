"""Typed errors.

Gateway HTTP status codes (CONTRACTS §1) and control-channel RPC failures
(§4) are mapped onto these so callers can branch on ``isinstance``.

Mirrors ``sdk/src/errors.ts`` name-for-name.
"""

from __future__ import annotations

from typing import Optional

from .types import GatewayErrorBody


class SolariError(Exception):
    """Base class for every error thrown by the SDK."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
        #: Mirrors the TS ``error.name`` (the concrete subclass name).
        self.name = type(self).__name__


class GatewayError(SolariError):
    """Any non-2xx response from the gateway that isn't otherwise specialized."""

    def __init__(
        self,
        status: int,
        message: str,
        body: Optional[GatewayErrorBody] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code: Optional[str] = body.code if body else None
        self.body = body


class AuthError(GatewayError):
    """``401`` — the API key was missing, malformed, or rejected."""

    def __init__(
        self,
        message: str = "Invalid or missing API key",
        body: Optional[GatewayErrorBody] = None,
    ) -> None:
        super().__init__(401, message, body)


class PlanError(GatewayError):
    """``402 {code:"FeatureRequiresPlan"}`` — the plan doesn't allow this."""

    def __init__(
        self,
        message: str = "This feature requires a different plan",
        body: Optional[GatewayErrorBody] = None,
    ) -> None:
        super().__init__(402, message, body)


class ConcurrencyLimitError(GatewayError):
    """``429 {code:"ConcurrencyLimitExceeded"}`` — too many live desktops."""

    def __init__(
        self,
        message: str = "Concurrency limit exceeded",
        body: Optional[GatewayErrorBody] = None,
    ) -> None:
        super().__init__(429, message, body)


class NoCapacityError(GatewayError):
    """``503`` — no desktop host currently available to serve the request."""

    def __init__(
        self,
        message: str = "No desktop host available",
        body: Optional[GatewayErrorBody] = None,
    ) -> None:
        super().__init__(503, message, body)


class ActionError(SolariError):
    """A computer-use RPC returned ``{ok:false}``."""

    def __init__(self, method: str, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.method = method
        self.code = code


class TimeoutError(SolariError):  # noqa: A001 - intentionally shadows builtin, mirrors TS
    """An RPC did not receive a reply within the per-call timeout."""

    def __init__(self, method: str, timeout_ms: int) -> None:
        super().__init__(f'Action "{method}" timed out after {timeout_ms}ms')
        self.method = method
        self.timeoutMs = timeout_ms


class ConnectionError(SolariError):  # noqa: A001 - intentionally shadows builtin, mirrors TS
    """The control WebSocket is not open (never connected, or closed)."""

    def __init__(self, message: str = "Control channel is not connected") -> None:
        super().__init__(message)


def map_gateway_error(
    status: int,
    body: Optional[GatewayErrorBody],
) -> GatewayError:
    """Map a gateway HTTP response onto the appropriate typed error.

    Falls back to a generic :class:`GatewayError` for unrecognized statuses.
    """
    msg: Optional[str] = None
    if body is not None:
        msg = body.message or body.error or body.code

    if status == 401:
        return AuthError(msg or "Invalid or missing API key", body)
    if status == 402:
        return PlanError(msg or "This feature requires a different plan", body)
    if status == 429:
        return ConcurrencyLimitError(msg or "Concurrency limit exceeded", body)
    if status == 503:
        return NoCapacityError(msg or "No desktop host available", body)
    return GatewayError(
        status,
        msg or f"Gateway request failed with status {status}",
        body,
    )


__all__ = [
    "SolariError",
    "GatewayError",
    "AuthError",
    "PlanError",
    "ConcurrencyLimitError",
    "NoCapacityError",
    "ActionError",
    "TimeoutError",
    "ConnectionError",
    "map_gateway_error",
]
