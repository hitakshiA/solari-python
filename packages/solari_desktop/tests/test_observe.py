"""Offline tests for the fork's desktop observe/act/launch.

No network and no desktop: a fake reflexd holds a tiny screen, and is reached
either through an :class:`httpx.MockTransport` (the preview-URL path) or
through a fake ``commands.run`` that answers ``curl`` (the exec path). The
install script and the launch command are asserted as sent.

Coroutines run through ``asyncio.run`` rather than pytest-asyncio, as in
solari-browser's tests.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import gzip
import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import httpx
import pytest

import solari_core.reflex as reflex_mod
from solari_core.types import CommandResult, CreateDesktopResponse
from solari_desktop import (
    Desktop,
    DesktopConfig,
    DesktopObserver,
    ObserverError,
    SessionHooks,
    StaleObservationError,
    format_observation,
)
from solari_core.reflex import app_matches, reflexd_source, with_path


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _instant(_s: float) -> None:
        return None
    monkeypatch.setattr(reflex_mod.asyncio, "sleep", _instant)


# The shape Solari returns: no path, the access token in the query.
PREVIEW = "https://sbx-7788.preview.example.com?pt_token=preview-secret"


class FakeReflexd:
    """reflexd's HTTP contract over a two-control screen (a text area and a Save button)."""

    def __init__(self, app: str = "Mousepad") -> None:
        self.app = app
        self.windows = True
        self.value = ""
        self.save_name = "Save"
        self.token: Optional[str] = None
        self.dispatched: List[Dict[str, Any]] = []
        self.requests: List[Dict[str, Any]] = []
        self.null_observes = 0

    def guard(self, node: int) -> Optional[str]:
        if node == 1:
            return json.dumps(["text", "", self.value, "1100110"])
        if node == 2 and self.save_name:
            return json.dumps(["push button", self.save_name, None, "0000110"])
        return None

    def observe(self) -> Optional[Dict[str, Any]]:
        if not self.windows or self.null_observes > 0:
            self.null_observes -= 1
            return None
        elements = [{"id": "e1", "node": 1, "role": "textbox", "name": "textbox", "editable": True}]
        if self.value:
            elements[0]["value"] = self.value
        if self.save_name:
            elements.append({"id": "e2", "node": 2, "role": "button", "name": self.save_name, "editable": False})
        return {
            "url": f"app://{self.app}/Untitled 1",
            "title": "Untitled 1",
            "viewport": {"width": 1280, "height": 800},
            "scroll": {"y": 0, "height": 800},
            "text": "File Edit Search",
            "elements": elements,
            "omitted": 0,
            "pageKey": f"key-{self.value}",
            "guards": {str(e["node"]): self.guard(e["node"]) for e in elements},
            "focused": "e1",
        }

    def act(self, action: Dict[str, Any], expected: Optional[str]) -> Dict[str, Any]:
        if "node" in action:
            current = self.guard(int(action["node"]))
            if current is None:
                return {"error": "gone"}
            if expected is not None and current != expected:
                return {"error": "stale"}
        self.dispatched.append(action)
        if action["kind"] == "type":
            self.value = action["text"]
        return {"navigated": False}

    def handle(self, method: str, path: str, auth: Optional[str], body: Any) -> Dict[str, Any]:
        self.requests.append({"method": method, "path": path, "auth": auth, "body": body})
        if method == "GET" and path == "/health":
            return {"ok": True, "version": 1}
        if auth != f"Bearer {self.token}":
            return {"error": "unauthorised"}
        if path == "/observe":
            return {"result": self.observe(), "ms": 3}
        if path == "/act":
            return {"result": self.act(body["action"], body.get("guard")), "ms": 5}
        return {"error": "not found"}

    def transport(self) -> httpx.MockTransport:
        def _h(req: httpx.Request) -> httpx.Response:
            assert req.url.params.get("pt_token") == "preview-secret"
            body = json.loads(req.content) if req.content else None
            return httpx.Response(200, json=self.handle(req.method, req.url.path, req.headers.get("authorization"), body))
        return httpx.MockTransport(_h)


class FakeCommands:
    """``desktop.commands``: runs the install script, launch.sh and curl against the fake reflexd."""

    def __init__(self, daemon: FakeReflexd, *, install_ok: bool = True) -> None:
        self.daemon = daemon
        self.install_ok = install_ok
        self.calls: List[Dict[str, Any]] = []

    async def run(self, cmd: str, *, args: Optional[List[str]] = None, **kw: Any) -> CommandResult:
        args = args or []
        self.calls.append({"cmd": cmd, "args": args})
        if cmd == "bash" and "reflexd.py" in args[-1] and "start.sh" in args[-1]:
            if not self.install_ok:
                return CommandResult(exitCode=1, stdout="reflexd not ready", stderr="gi missing")
            self.daemon.token = re.search(r"start\.sh '([0-9a-f]+)'", args[-1]).group(1)
            return CommandResult(exitCode=0, stdout="reflexd ready\n", stderr="")
        if cmd == "bash" and args[-1].startswith("runuser") and "launch.sh" in args[-1]:
            self.daemon.windows = True
            return CommandResult(exitCode=0, stdout="", stderr="")
        if cmd == "curl":
            url = args[args.index("-X") + 2]
            auth = args[args.index("-H") + 1].split(": ", 1)[1]
            body = json.loads(args[args.index("--data-binary") + 1])
            reply = self.daemon.handle("POST", urlsplit(url).path, auth, body)
            return CommandResult(exitCode=0, stdout=json.dumps(reply), stderr="")
        raise AssertionError(f"unexpected command {cmd} {args}")


class FakeDesktop:
    """The slice of :class:`Desktop` the observer uses."""

    def __init__(self, daemon: FakeReflexd, *, preview: Any = PREVIEW, install_ok: bool = True) -> None:
        self.connected = False
        self.commands = FakeCommands(daemon, install_ok=install_ok)
        self._preview = preview

    async def connect(self) -> None:
        self.connected = True

    async def preview_url(self, port: int) -> Dict[str, Any]:
        assert port == 7788
        if isinstance(self._preview, Exception):
            raise self._preview
        return {"url": self._preview}


def observer(daemon: FakeReflexd, **kw: Any) -> DesktopObserver:
    desktop = FakeDesktop(daemon, **{k: kw.pop(k) for k in ("preview", "install_ok") if k in kw})
    return DesktopObserver(desktop, http=httpx.AsyncClient(transport=daemon.transport()), **kw)


# ---- install and transport ----------------------------------------------------


@sync
async def test_start_installs_the_vendored_reflexd_gzipped_and_uses_the_preview_url() -> None:
    d = FakeReflexd()
    o = observer(d)
    assert o.transport is None
    await o.start()
    await o.start()  # idempotent
    installs = [c for c in o._d.commands.calls if c["cmd"] == "bash"]
    assert len(installs) == 1
    script = installs[0]["args"][1]
    payload = re.search(r"echo '([A-Za-z0-9+/=]+)' \| base64 -d \| gunzip", script).group(1)
    assert gzip.decompress(base64.b64decode(payload)) == reflexd_source()
    # Solari's HTTP exec rejects bodies over 16 KB; the script stays under it.
    assert len(script.encode()) < 16_000
    assert o._d.connected is True
    assert o.transport == "preview"
    assert d.requests[0]["path"] == "/health"


@sync
async def test_calls_keep_the_preview_token_query_and_send_the_bearer_token() -> None:
    d = FakeReflexd()
    o = observer(d)
    obs = await o.observe()
    assert obs.url == "app://Mousepad/Untitled 1"
    assert d.requests[-1] == {
        "method": "POST",
        "path": "/observe",
        "auth": f"Bearer {d.token}",
        "body": {"max_elements": 120, "max_text_chars": 3000},
    }
    assert not any(c["cmd"] == "curl" for c in o._d.commands.calls)


def test_with_path_keeps_the_query() -> None:
    assert with_path(PREVIEW, "/observe") == "https://sbx-7788.preview.example.com/observe?pt_token=preview-secret"
    assert with_path("https://h.example.com/", "/act") == "https://h.example.com/act"
    # What plain concatenation would have sent: the path lands inside the token.
    assert urlsplit(PREVIEW + "/observe").query == "pt_token=preview-secret/observe"


@sync
async def test_falls_back_to_exec_when_there_is_no_preview_url() -> None:
    d = FakeReflexd()
    o = observer(d, preview=RuntimeError("preview_url requires a client-created handle"))
    obs = await o.observe()
    assert o.transport == "exec"
    assert obs.elements[0].role == "textbox"
    curl = [c for c in o._d.commands.calls if c["cmd"] == "curl"]
    assert len(curl) == 1 and "http://127.0.0.1:7788/observe" in curl[0]["args"]


@sync
async def test_falls_back_to_exec_when_the_preview_url_does_not_answer() -> None:
    d = FakeReflexd()

    def _down(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    o = DesktopObserver(FakeDesktop(d), http=httpx.AsyncClient(transport=httpx.MockTransport(_down)))
    await o.observe()
    assert o.transport == "exec"


@sync
async def test_exec_transport_can_be_forced() -> None:
    d = FakeReflexd()
    o = observer(d, transport="exec")
    await o.observe()
    assert o.transport == "exec"
    assert not any(r["path"] == "/health" for r in d.requests)


@sync
async def test_install_failure_is_an_observer_error() -> None:
    o = observer(FakeReflexd(), install_ok=False)
    with pytest.raises(ObserverError, match="reflexd did not start"):
        await o.observe()


# ---- observe -----------------------------------------------------------------


@sync
async def test_observe_retries_while_no_window_is_active() -> None:
    d = FakeReflexd()
    d.null_observes = 3
    o = observer(d)
    obs = await o.observe()
    assert obs.elements and o.last is obs
    assert sum(r["path"] == "/observe" for r in d.requests) == 4


@sync
async def test_observe_gives_up_with_no_window() -> None:
    d = FakeReflexd()
    d.windows = False
    with pytest.raises(ObserverError, match="No window is active"):
        await observer(d).observe()


# ---- act and guards ------------------------------------------------------------


@sync
async def test_act_sends_the_node_and_its_guard_and_returns_a_fresh_observation() -> None:
    d = FakeReflexd()
    o = observer(d)
    first = await o.observe()
    after = await o.act("e1", "type", text="hello from python")
    assert d.dispatched == [{"kind": "type", "node": 1, "text": "hello from python"}]
    act_req = [r for r in d.requests if r["path"] == "/act"][0]
    assert act_req["body"]["guard"] == first.guards["1"]
    assert after.element("e1").value == "hello from python"
    assert after.pageKey != first.pageKey
    assert o.last is after


@sync
async def test_type_with_submit_presses_enter_after() -> None:
    d = FakeReflexd()
    o = observer(d)
    await o.observe()
    await o.act("e1", "type", text="x", submit=True)
    assert d.dispatched == [{"kind": "type", "node": 1, "text": "x"}, {"kind": "press", "key": "Enter"}]


@sync
async def test_a_changed_target_is_refused_and_nothing_is_dispatched() -> None:
    d = FakeReflexd()
    o = observer(d)
    await o.observe()
    d.save_name = "Save As…"  # the button changed under the agent
    with pytest.raises(StaleObservationError) as err:
        await o.act("e2", "click")
    assert err.value.reason == "stale" and err.value.ref == "e2"
    assert "observe again" in str(err.value)
    assert d.dispatched == []


@sync
async def test_a_vanished_target_is_refused_as_gone() -> None:
    d = FakeReflexd()
    o = observer(d)
    await o.observe()
    d.save_name = ""
    with pytest.raises(StaleObservationError) as err:
        await o.act("e2", "click")
    assert err.value.reason == "gone"
    assert d.dispatched == []


@sync
async def test_an_old_observation_is_checked_against_the_screen_now() -> None:
    d = FakeReflexd()
    o = observer(d)
    old = await o.observe()
    await o.act("e1", "type", text="first")
    with pytest.raises(StaleObservationError):
        await o.act("e1", "type", text="second", observation=old)
    assert d.dispatched == [{"kind": "type", "node": 1, "text": "first"}]


@sync
async def test_unknown_ref_and_acting_before_observing_send_nothing() -> None:
    d = FakeReflexd()
    o = observer(d)
    with pytest.raises(StaleObservationError, match="observe\\(\\) first"):
        await o.act("e1", "click")
    await o.observe()
    with pytest.raises(StaleObservationError) as err:
        await o.act("e9", "click")
    assert err.value.reason == "unknown"
    assert not any(r["path"] == "/act" for r in d.requests)


@sync
async def test_a_missing_guard_is_never_sent_as_none() -> None:
    # reflexd skips its check when guard is null; the SDK must refuse instead.
    d = FakeReflexd()
    o = observer(d)
    obs = await o.observe()
    obs.guards.pop("2")
    with pytest.raises(StaleObservationError):
        await o.act("e2", "click")
    assert d.dispatched == []


@sync
async def test_press_accepts_only_keys_reflexd_knows() -> None:
    d = FakeReflexd()
    o = observer(d)
    await o.observe()
    with pytest.raises(ValueError, match="Enter, Escape, Tab"):
        await o.act(None, "press", key="Backspace")  # reflexd would press Return
    await o.act(None, "press", key="Escape")
    await o.act(None, "scroll", direction="up")
    assert d.dispatched == [{"kind": "press", "key": "Escape"}, {"kind": "scroll", "direction": "up"}]


@sync
async def test_act_over_a_failing_preview_is_not_repeated_through_exec() -> None:
    d = FakeReflexd()
    calls = {"n": 0}
    inner = d.transport()

    def _flaky(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/act":
            calls["n"] += 1
            raise httpx.ReadTimeout("timed out", request=req)
        return inner.handle_request(req)

    o = DesktopObserver(FakeDesktop(d), http=httpx.AsyncClient(transport=httpx.MockTransport(_flaky)))
    await o.observe()
    with pytest.raises(ObserverError, match="preview URL failed"):
        await o.act("e2", "click")
    # The click may have reached the desktop; re-sending it could click twice.
    assert calls["n"] == 1
    assert not any(c["cmd"] == "curl" for c in o._d.commands.calls)


# ---- launch --------------------------------------------------------------------


@sync
async def test_launch_starts_the_app_through_the_accessible_launcher_and_waits_for_it() -> None:
    d = FakeReflexd(app="Mousepad")
    d.windows = False
    o = observer(d)
    obs = await o.launch("mousepad", ["/tmp/it's here.txt"])
    launch = [c for c in o._d.commands.calls if c["cmd"] == "bash" and c["args"][1].startswith("runuser")]
    assert launch[0]["args"][1] == (
        'runuser -u "$(cat /opt/reflex/user)" -- bash /opt/reflex/launch.sh '
        "mousepad '/tmp/it'\"'\"'s here.txt'"
    )
    assert obs.url.startswith("app://Mousepad/")


@sync
async def test_launch_gives_up_when_no_window_appears() -> None:
    d = FakeReflexd()
    d.windows = False
    o = observer(d)
    d_launch = o._d.commands.run

    async def _no_window(cmd: str, **kw: Any) -> CommandResult:
        r = await d_launch(cmd, **kw)
        d.windows = False
        return r

    o._d.commands.run = _no_window  # type: ignore[method-assign]
    with pytest.raises(ObserverError, match="did not open a window"):
        await o.launch("mousepad", wait_ms=1)


def test_app_matches() -> None:
    from solari_core import parse_observation

    def o(url: str, title: str = "") -> Any:
        return parse_observation({"url": url, "title": title})

    assert app_matches(o("app://Mousepad/Untitled 1"), "mousepad")
    assert app_matches(o("app://soffice/Untitled 1 - LibreOffice Calc", "Untitled 1 - LibreOffice Calc"), "soffice")
    assert not app_matches(o("app://Thunar/Home"), "mousepad")
    assert not app_matches(o("app:///"), "mousepad")


# ---- through the Desktop handle ------------------------------------------------


@sync
async def test_desktop_handle_observe_act_and_format(monkeypatch) -> None:
    d = FakeReflexd()
    seen_ports: List[int] = []

    async def _preview(session_id: str, port: int) -> Dict[str, Any]:
        seen_ports.append(port)
        return {"url": PREVIEW}

    desktop = Desktop(
        CreateDesktopResponse(sessionId="sbx-1", streamUrl="", controlUrl="wss://x/control/sbx-1", expiresAt=""),
        DesktopConfig(hooks=SessionHooks(preview_url=_preview)),
    )
    monkeypatch.setattr(Desktop, "connected", property(lambda self: True))
    desktop.commands = FakeCommands(d)  # type: ignore[assignment]
    desktop.observer._http = httpx.AsyncClient(transport=d.transport())

    obs = await desktop.observe()
    assert seen_ports == [7788]
    assert format_observation(obs).splitlines()[3] == 'e1 textbox "textbox" [focused]'
    after = await desktop.act("e1", "type", text="hi")
    assert after.element("e1").value == "hi"
    with pytest.raises(StaleObservationError):
        await desktop.act("e1", "type", text="again", observation=obs)
    await desktop.close()
