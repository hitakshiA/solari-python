"""REGRESSION GUARD for the shipped fix: the resolved-proxy type carries NO credentials.

ResolvedProxyConfig used to carry server/username/password. The gateway stopped
sending them (they disclose the proxy vendor's account), so the bindings parsed
empty strings forever — a contract that was not merely dead but misleading: a
caller could read `session.proxy.server`, get "", and conclude no proxy applied.

test_client.py asserts the fields that ARE present, so re-adding a credential
field breaks nothing there. These assert the ABSENCE, which is the fix.
"""

from __future__ import annotations

import dataclasses

from solari_browser.types import ResolvedProxyConfig

# Exact set, not a denylist: a field named `creds` or `upstream` would be the same
# disclosure under a name no denylist predicts.
EXPECTED_FIELDS = ["timezone_id", "country", "tier"]


def test_resolved_proxy_config_has_exactly_the_confirmation_fields() -> None:
    got = [f.name for f in dataclasses.fields(ResolvedProxyConfig)]
    assert got == EXPECTED_FIELDS, (
        f"ResolvedProxyConfig fields = {got}, want exactly {EXPECTED_FIELDS}. "
        "This type is a COARSE CONFIRMATION: egress is applied server-side, so a "
        "caller never dials the proxy and must never receive its address or account."
    )


def test_from_wire_drops_legacy_credential_keys() -> None:
    rp = ResolvedProxyConfig.from_wire(
        {
            "timezoneId": "America/Los_Angeles",
            "country": "us",
            "tier": "mobile",
            # What an older gateway used to send.
            "server": "http://resi.vendor.example:8000",
            "username": "acct-12345",
            "password": "hunter2",
        }
    )

    assert rp.timezone_id == "America/Los_Angeles"
    assert rp.country == "us"
    assert rp.tier == "mobile"

    for leaked in ("server", "username", "password"):
        assert not hasattr(rp, leaked), f"ResolvedProxyConfig re-grew a {leaked!r} attribute"

    # Nothing a caller can serialise or log carries the vendor account.
    rendered = repr(dataclasses.asdict(rp)) + repr(rp)
    for secret in ("resi.vendor.example", "acct-12345", "hunter2"):
        assert secret not in rendered, f"resolved proxy leaks {secret!r}: {rendered}"
