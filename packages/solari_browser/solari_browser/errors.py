"""Errors for the Solari Browser SDK. Mirrors `SolariError` in sdk/src/index.ts."""

from __future__ import annotations

from typing import Any, Optional

# Codes the gateway returns in the `code` field of an error body. Mirrors
# `SolariErrorCode` (sdk/src/index.ts). Kept as plain strings, not an Enum: the
# gateway may add codes we don't know yet, and an Enum would raise on those
# rather than pass them through to the caller.
FEATURE_REQUIRES_PLAN = "FeatureRequiresPlan"
CONCURRENCY_LIMIT_EXCEEDED = "ConcurrencyLimitExceeded"
PLAN_LIMIT_EXCEEDED = "PlanLimitExceeded"
BROWSER_UNHEALTHY = "BrowserUnhealthy"
# Raised client-side by Observer.act (solari-python fork); never sent by the gateway.
STALE_OBSERVATION = "StaleObservation"


class SolariError(Exception):
    """Raised for every Solari failure.

    Attributes:
        status: HTTP status, when the failure came from an HTTP response.
        cause:  The underlying exception, when there was one.
        code:   Gateway error code (see the constants above), when the response
                body carried one. `code == BROWSER_UNHEALTHY` is what makes
                `launch(retries=...)` retry.
    """

    def __init__(
        self,
        message: str,
        status: Optional[int] = None,
        cause: Optional[BaseException] = None,
        code: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.cause = cause
        self.code = code

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = super().__str__()
        bits = []
        if self.status is not None:
            bits.append(f"status={self.status}")
        if self.code:
            bits.append(f"code={self.code}")
        return f"{base} ({', '.join(bits)})" if bits else base


class StaleObservationError(SolariError):
    """An ``Observer.act`` target no longer matches the observation it was chosen from.

    Raised before any input is dispatched, so the page is untouched: observe
    again and decide again. ``code`` is ``STALE_OBSERVATION``; ``reason`` is:

    - ``"stale"``: the control's role, name, value, state or row changed;
    - ``"covered"``: it is covered, disabled or gone;
    - ``"option"``: the requested ``<select>`` option cannot be chosen;
    - ``"unknown"``: ``ref`` is not an id in the observation.

    Added in the solari-python fork.
    """

    def __init__(self, ref: Optional[str], reason: str, message: Optional[str] = None) -> None:
        target = ref or "the target"
        super().__init__(
            message or f"{target} {_STALE_REASONS.get(reason, reason)}; observe again",
            None,
            None,
            STALE_OBSERVATION,
        )
        self.ref = ref
        self.reason = reason


_STALE_REASONS = {
    "stale": "changed since it was observed",
    "covered": "is covered, disabled or gone",
    "option": "has no such option",
    "unknown": "is not a control in the last observation",
}


def code_from_body(text: str) -> Optional[str]:
    """Best-effort extract of `{"code": "..."}` from an error body.

    The gateway returns a JSON body with a `code` on plan/concurrency errors, but
    error bodies are not guaranteed to be JSON at all (proxies, 502 HTML pages),
    so every failure here is swallowed — a missing code must never mask the real
    HTTP error.
    """
    import json

    try:
        parsed: Any = json.loads(text)
    except Exception:
        return None
    if isinstance(parsed, dict):
        code = parsed.get("code")
        if isinstance(code, str):
            return code
    return None
