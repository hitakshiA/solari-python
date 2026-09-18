"""Offline tests for the Solari Browser Python SDK.

No network, no browser: a real loopback HTTP server stands in for the gateway so the
request shapes are asserted on the wire (mirrors the mock-listener approach the Go and
Rust desktop SDKs use). `launch()` is deliberately NOT covered here — it needs a live
pool and a real patchright driver.
"""

from __future__ import annotations

import asyncio
import functools
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Tuple

import pytest

# The SDK is async, but the suite runs each coroutine through asyncio.run() rather
# than pytest-asyncio. Two reasons: it drops a dev dependency, and pytest-asyncio's
# instrumented Task breaks anyio's task introspection on older anyio (httpx runs on
# anyio) with:
#     TypeError: descriptor 'get_coro' for '_asyncio.Task' objects doesn't apply to
#     a 'Task' object
# A plain asyncio.run() loop has no such problem, so the tests stay portable across
# whatever anyio/pytest-asyncio the host happens to ship.
def sync(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper

from solari_browser import (
    UNSET,
    ProxyRequest,
    Solari,
    SolariError,
    derive_cdp_from_ws,
)

# ---- a tiny scriptable gateway ------------------------------------------------

Recorded = Dict[str, Any]


class _Gateway:
    """Loopback HTTP server whose responses are scripted per (method, path)."""

    def __init__(self) -> None:
        self.routes: Dict[Tuple[str, str], List[Tuple[int, Any]]] = {}
        self.seen: List[Recorded] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:  # silence stderr spam
                pass

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode() if length else None
                outer.seen.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "auth": self.headers.get("Authorization"),
                        "content_type": self.headers.get("Content-Type"),
                        "body": json.loads(raw) if raw else None,
                        "raw_body": raw,
                    }
                )
                queue = outer.routes.get((self.command, self.path))
                if not queue:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'{"error":"no route"}')
                    return
                status, payload = queue.pop(0) if len(queue) > 1 else queue[0]
                body = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_DELETE = _handle

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def route(self, method: str, path: str, status: int, payload: Any) -> None:
        self.routes.setdefault((method, path), []).append((status, payload))

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def gw():
    g = _Gateway()
    yield g
    g.stop()


def client(gw: _Gateway, **kw: Any) -> Solari:
    return Solari(api_key="slr_live_test_secret", base_url=gw.url, **kw)


SESSION_OK = {
    "sessionId": "sess-1",
    "wsEndpoint": "wss://api.example.com/ws/sess-1",
    "cdpEndpoint": "wss://api.example.com/cdp/sess-1",
    "expiresAt": "2026-07-16T12:00:00Z",
}


# ---- construction -------------------------------------------------------------


def test_api_key_required():
    with pytest.raises(SolariError):
        Solari(api_key="")


def test_unsupported_region_rejected():
    with pytest.raises(SolariError):
        Solari(api_key="k", region="eu-north")


def test_base_url_overrides_region_and_strips_slash(gw):
    c = Solari(api_key="k", base_url=gw.url + "/")
    assert c.base_url == gw.url


def test_default_region_is_prod():
    assert Solari(api_key="k").base_url == "https://api.getsolari.com"


# ---- sessions.create ----------------------------------------------------------


@sync
async def test_create_sends_no_body_when_no_options(gw):
    gw.route("POST", "/sessions", 201, SESSION_OK)
    c = client(gw)
    s = await c.sessions.create()
    await c.close()
    assert s.id == "sess-1"
    # An empty options set must send NO body at all, not "{}".
    assert gw.seen[0]["raw_body"] is None
    assert gw.seen[0]["auth"] == "Bearer slr_live_test_secret"
    assert gw.seen[0]["content_type"] == "application/json"


@sync
async def test_create_omits_falsy_keys(gw):
    gw.route("POST", "/sessions", 201, SESSION_OK)
    c = client(gw)
    await c.sessions.create(stealth=True, recording=False, captcha=False)
    await c.close()
    body = gw.seen[0]["body"]
    assert body == {"stealth": True}  # recording/captcha False => absent entirely


@sync
async def test_create_serialises_proxy_request(gw):
    gw.route("POST", "/sessions", 201, SESSION_OK)
    c = client(gw)
    await c.sessions.create(
        stealth=True,
        proxy=ProxyRequest(country="gb", tier="mobile", session="warm-1", session_duration=15),
    )
    await c.close()
    assert gw.seen[0]["body"]["proxy"] == {
        "country": "gb",
        "tier": "mobile",
        "session": "warm-1",
        "sessionDuration": 15,  # camelCase on the wire
    }


@sync
async def test_create_accepts_proxy_string_sentinels(gw):
    gw.route("POST", "/sessions", 201, SESSION_OK)
    c = client(gw)
    await c.sessions.create(stealth=True, proxy="smart")
    await c.close()
    assert gw.seen[0]["body"]["proxy"] == "smart"


@sync
async def test_create_derives_cdp_when_absent(gw):
    gw.route(
        "POST",
        "/sessions",
        201,
        {**SESSION_OK, "cdpEndpoint": None},
    )
    c = client(gw)
    s = await c.sessions.create()
    await c.close()
    assert s.cdp_endpoint == "wss://api.example.com/cdp/sess-1"


@sync
async def test_create_returns_upstream_endpoints_not_loopback(gw):
    # Unlike the TS SDK (which rewrites to a LocalProxy), we hand back upstream.
    gw.route("POST", "/sessions", 201, SESSION_OK)
    c = client(gw)
    s = await c.sessions.create()
    await c.close()
    assert s.ws_endpoint.startswith("wss://api.example.com/ws/")
    assert "127.0.0.1" not in s.ws_endpoint


@sync
async def test_storage_state_unset_without_profile(gw):
    gw.route("POST", "/sessions", 201, SESSION_OK)
    c = client(gw)
    s = await c.sessions.create()
    await c.close()
    assert s.storage_state is UNSET  # "no profile attached"


@sync
async def test_storage_state_none_for_empty_profile(gw):
    gw.route(
        "POST", "/sessions", 201, {**SESSION_OK, "storageStateUrl": {"url": None}}
    )
    c = client(gw)
    s = await c.sessions.create(profile_id="p1")
    await c.close()
    assert s.storage_state is None  # "profile exists but empty"


@sync
async def test_create_error_parses_code(gw):
    gw.route("POST", "/sessions", 402, {"error": "nope", "code": "FeatureRequiresPlan"})
    c = client(gw)
    with pytest.raises(SolariError) as ei:
        await c.sessions.create(stealth=True)
    await c.close()
    assert ei.value.status == 402
    assert ei.value.code == "FeatureRequiresPlan"


@sync
async def test_missing_session_id_is_an_error(gw):
    gw.route("POST", "/sessions", 201, {"wsEndpoint": "wss://x/ws/1"})
    c = client(gw)
    with pytest.raises(SolariError):
        await c.sessions.create()
    await c.close()


# ---- retry policy -------------------------------------------------------------


@sync
async def test_retries_503_then_succeeds(gw):
    gw.route("POST", "/sessions", 503, {"error": "no capacity"})
    gw.route("POST", "/sessions", 201, SESSION_OK)
    c = client(gw, backoff_ms=1)
    s = await c.sessions.create()
    await c.close()
    assert s.id == "sess-1"
    assert len(gw.seen) == 2  # retried exactly once


@sync
async def test_does_not_retry_400(gw):
    gw.route("POST", "/sessions", 400, {"error": "bad"})
    gw.route("POST", "/sessions", 201, SESSION_OK)
    c = client(gw, backoff_ms=1)
    with pytest.raises(SolariError):
        await c.sessions.create()
    await c.close()
    assert len(gw.seen) == 1  # 400 is terminal — no second attempt


@sync
async def test_exhausting_attempts_raises(gw):
    gw.route("POST", "/sessions", 503, {"error": "no capacity"})
    c = client(gw, backoff_ms=1, max_attempts=2)
    with pytest.raises(SolariError) as ei:
        await c.sessions.create()
    await c.close()
    assert "exhausted 2 attempts" in str(ei.value)
    assert len(gw.seen) == 2


# ---- release / replay ---------------------------------------------------------


@sync
async def test_release_and_wait_tolerates_404(gw):
    gw.route("DELETE", "/sessions/sess-1", 404, {"error": "gone"})
    c = client(gw)
    await c.sessions.release_and_wait("sess-1")  # must NOT raise
    await c.close()


@sync
async def test_release_never_raises(gw):
    gw.route("DELETE", "/sessions/sess-1", 500, {"error": "boom"})
    c = client(gw)
    await c.sessions.release("sess-1")  # best-effort by contract
    await c.close()


@sync
async def test_replay_url_defaults(gw):
    gw.route("GET", "/sessions/sess-1/replay-url", 200, {"url": "https://s3/replay"})
    c = client(gw)
    r = await c.sessions.get_replay_url("sess-1")
    await c.close()
    assert r.url == "https://s3/replay"
    assert r.expires_in_seconds == 0
    assert r.content_encoding == "gzip"


@sync
async def test_session_id_is_url_encoded(gw):
    # Session ids are HMAC-signed composites and contain non-path-safe chars.
    weird = "abc/def+ghi=="
    gw.route("DELETE", "/sessions/abc%2Fdef%2Bghi%3D%3D", 200, {"ok": True})
    c = client(gw)
    await c.sessions.release_and_wait(weird)
    await c.close()
    assert gw.seen[0]["path"] == "/sessions/abc%2Fdef%2Bghi%3D%3D"


# ---- profiles -----------------------------------------------------------------


@sync
async def test_profiles_create_and_list(gw):
    gw.route("POST", "/profiles", 200, {"id": "p1", "name": "shopper"})
    gw.route("GET", "/profiles", 200, [{"id": "p1", "name": "shopper"}])
    c = client(gw)
    p = await c.profiles.create("shopper")
    all_ = await c.profiles.list()
    await c.close()
    assert (p.id, p.name) == ("p1", "shopper")
    assert [x.id for x in all_] == ["p1"]
    assert gw.seen[0]["body"] == {"name": "shopper"}


@sync
async def test_profiles_delete_tolerates_404(gw):
    gw.route("DELETE", "/profiles/p1", 404, {"error": "gone"})
    c = client(gw)
    await c.profiles.delete("p1")  # must NOT raise
    await c.close()


@sync
async def test_profiles_save_defaults(gw):
    gw.route("POST", "/profiles/p1/save", 200, {})
    c = client(gw)
    r = await c.profiles.save("p1", {"cookies": []})
    await c.close()
    assert (r.version, r.size_bytes) == (0, 0)
    assert gw.seen[0]["body"] == {"storageState": {"cookies": []}}


# ---- pure helpers -------------------------------------------------------------


def test_derive_cdp_rewrites_ws_path():
    assert (
        derive_cdp_from_ws("wss://api.example.com/ws/abc?k=1")
        == "wss://api.example.com/cdp/abc?k=1"
    )


def test_derive_cdp_passthrough_when_not_ws_path():
    assert derive_cdp_from_ws("wss://api.example.com/other/abc") == (
        "wss://api.example.com/other/abc"
    )
    assert derive_cdp_from_ws("not-a-url") == "not-a-url"
