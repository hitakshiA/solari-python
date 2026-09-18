"""``DesktopObserver`` — fast observe/act on a desktop through ``reflexd``.

Added in the solari-python fork. The stock desktop image ships with the
accessibility bus off and no bindings installed, so the only way to look at it
through this SDK was a screenshot, and the only way to act was pixel
coordinates. On first use this module:

1. turns accessibility on and installs ``reflexd`` (vendored from solari-reflex
   as ``solari_core/reflexd/reflexd.py``) through the handle's existing
   ``commands.run``. The daemon travels gzipped: Solari's HTTP exec rejects
   request bodies over 16 KB, and the same script works over either path;
2. starts it as the desktop user on port 7788, with a random bearer token;
3. reaches it through the machine's preview URL (``preview_url(7788)``): one
   keep-alive HTTP connection, no exec per call. When the preview URL is not
   available or does not answer, it falls back to ``commands.run("curl", …)``
   against localhost, one control-channel round trip per call.

Every observe is then one request that returns an
:class:`~solari_core.observation.Observation`; every act is one request that
re-checks the target's guard inside the desktop, next to the input, and is
refused (nothing dispatched) when the target changed.

Mirrors ``DesktopSurface`` in solari-reflex ``src/desktop.ts``.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import re
import shlex
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Union
from urllib.parse import urlsplit, urlunsplit

import httpx

from .errors import ObserverError, StaleObservationError
from .observation import ActKind, Observation, ObservedElement, parse_observation

if TYPE_CHECKING:  # pragma: no cover
    from .desktop import Desktop

#: Port reflexd listens on inside the desktop.
REFLEXD_PORT = 7788
#: Keys reflexd can press. It maps anything else to Return, so the SDK refuses
#: other keys rather than send them.
ACT_KEYS = ("Enter", "Escape", "Tab")

_REFLEXD_PATH = Path(__file__).parent / "reflexd" / "reflexd.py"
_TARGETED = ("click", "type", "select")

#: How the client reaches reflexd.
ObserverTransport = Literal["preview", "exec"]


def reflexd_source() -> bytes:
    """The vendored reflexd daemon, as installed into desktops."""
    return _REFLEXD_PATH.read_bytes()


def reflexd_gz_b64() -> str:
    """reflexd, gzipped (level 9, fixed mtime) and base64-encoded for the install script."""
    return base64.b64encode(gzip.compress(reflexd_source(), 9, mtime=0)).decode("ascii")


def install_script(token: str, port: int = REFLEXD_PORT) -> str:
    """Root script, run once through ``commands.run``: accessibility packages,
    reflexd, the bus, and a launcher that starts apps with the bridge on.
    Idempotent. Same script as solari-reflex ``desktop.ts``."""
    return f"""set -e
export DEBIAN_FRONTEND=noninteractive
if ! python3 -c 'import gi; gi.require_version("Atspi","2.0")' 2>/dev/null; then
  apt-get update -qq >/dev/null 2>&1
  apt-get install -y -qq --no-install-recommends at-spi2-core python3-gi gir1.2-atspi-2.0 libatk-adaptor imagemagick >/dev/null 2>&1
fi
mkdir -p /opt/reflex
echo '{reflexd_gz_b64()}' | base64 -d | gunzip > /opt/reflex/reflexd.py
P=$(pgrep -x xfce4-session | head -1)
ps -o user= -p "$P" | tr -d ' ' > /opt/reflex/user
tr '\\0' '\\n' < /proc/$P/environ | grep -E '^(DBUS_SESSION_BUS_ADDRESS|DISPLAY|XDG_RUNTIME_DIR|HOME|XAUTHORITY)=' > /opt/reflex/session.env
cat > /opt/reflex/start.sh <<'SH'
set -a; . /opt/reflex/session.env; set +a
gsettings set org.gnome.desktop.interface toolkit-accessibility true 2>/dev/null || true
pgrep -u "$(id -un)" -f at-spi-bus-launcher >/dev/null || setsid -f /usr/libexec/at-spi-bus-launcher --launch-immediately >/dev/null 2>&1 </dev/null
sleep 0.5
pkill -u "$(id -un)" -f "reflex/reflexd.py" || true
REFLEXD_TOKEN="$1" REFLEXD_PORT="$2" setsid -f python3 /opt/reflex/reflexd.py >/opt/reflex/reflexd.log 2>&1 </dev/null
SH
cat > /opt/reflex/launch.sh <<'SH'
set -a; . /opt/reflex/session.env; set +a
export NO_AT_BRIDGE=0 GTK_MODULES=gail:atk-bridge SAL_USE_VCLPLUGIN=gtk3 GNOME_ACCESSIBILITY=1
setsid -f "$@" >/dev/null 2>&1 </dev/null
SH
chown -R "$(cat /opt/reflex/user)" /opt/reflex
runuser -u "$(cat /opt/reflex/user)" -- bash /opt/reflex/start.sh '{token}' '{port}'
for i in $(seq 1 50); do curl -sf http://127.0.0.1:{port}/health >/dev/null && {{ echo "reflexd ready"; exit 0; }}; sleep 0.2; done
echo "reflexd not ready"; tail -5 /opt/reflex/reflexd.log; exit 1"""


def with_path(url: str, path: str) -> str:
    """``url`` with its path replaced by ``path``, keeping the query.

    Solari preview URLs carry their access token as ``?pt_token=…``, so
    ``url + "/observe"`` would put the path inside the query string.
    """
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


class DesktopObserver:
    """Observe and act on one :class:`~solari_core.desktop.Desktop` through reflexd.

    Normally used through ``desktop.observe()``, ``desktop.act()`` and
    ``desktop.launch()``; construct one directly to choose the transport or
    pass an :class:`httpx.AsyncClient`.

    :param transport: ``"preview"`` (default) reaches reflexd over the preview
        URL and falls back to exec when that fails; ``"exec"`` always uses
        ``commands.run`` + curl.
    :param max_elements: Controls offered per observation.
    :param max_text_chars: Visible text sent with each observation.
    """

    def __init__(
        self,
        desktop: "Desktop",
        *,
        transport: ObserverTransport = "preview",
        http: Optional[httpx.AsyncClient] = None,
        max_elements: int = 120,
        max_text_chars: int = 3000,
        request_timeout_ms: int = 60_000,
    ) -> None:
        self._d = desktop
        self._want = transport
        self._http = http
        self._owns_http = http is None
        self._timeout = request_timeout_ms / 1000.0
        self.max_elements = max_elements
        self.max_text_chars = max_text_chars
        self._token = uuid.uuid4().hex
        self._base_url: Optional[str] = None
        self._started = False
        # Created on first use: on Python 3.9 a Lock binds to the loop current
        # at construction, and a handle may be built outside one (attach()).
        self._lock: Optional[asyncio.Lock] = None
        #: The last observation returned, which ``act`` resolves ids against.
        self.last: Optional[Observation] = None

    @property
    def transport(self) -> Optional[ObserverTransport]:
        """``"preview"`` or ``"exec"`` once started, ``None`` before."""
        if not self._started:
            return None
        return "preview" if self._base_url else "exec"

    # --- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Turn accessibility on, install and start reflexd, and connect to it.
        Idempotent; ``observe``/``act``/``launch`` call it on first use."""
        if self._started:
            return
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._started:
                return
            if not self._d.connected:
                await self._d.connect()
            r = await self._d.commands.run("bash", args=["-c", install_script(self._token)])
            if r.exitCode != 0 or "reflexd ready" not in r.stdout:
                detail = (r.stderr or r.stdout or "").strip()[-300:]
                raise ObserverError(f"reflexd did not start (exit {r.exitCode}): {detail}")
            if self._want == "preview":
                self._base_url = await self._reach_preview()
            self._started = True

    async def _reach_preview(self) -> Optional[str]:
        """The preview URL for reflexd if it answers ``/health``, else ``None``."""
        try:
            info = await self._d.preview_url(REFLEXD_PORT)
        except Exception:  # noqa: BLE001 - no hook, or the gateway refused: use exec
            return None
        url = info.get("url") if isinstance(info, dict) else info
        if not url:
            return None
        try:
            res = await self._client().get(with_path(str(url), "/health"), timeout=10.0)
            if res.status_code == 200 and res.json().get("ok"):
                return str(url)
        except Exception:  # noqa: BLE001 - unreachable or not JSON: use exec
            pass
        return None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient()
        return self._http

    async def aclose(self) -> None:
        """Close the HTTP client (only if this observer owns it)."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    # --- observe / act / launch -----------------------------------------------

    async def observe(self) -> Observation:
        """Read the active window (and any open menus or popups) as numbered controls."""
        await self.start()
        body = {"max_elements": self.max_elements, "max_text_chars": self.max_text_chars}
        for _ in range(10):
            raw = await self._call("/observe", body)
            if raw:
                self.last = parse_observation(raw)
                return self.last
            await asyncio.sleep(0.2)
        raise ObserverError("No window is active on the desktop")

    async def act(
        self,
        ref: Union[str, ObservedElement, None],
        action: ActKind = "click",
        *,
        text: Optional[str] = None,
        value: Optional[str] = None,
        key: Optional[str] = None,
        direction: Literal["up", "down"] = "down",
        submit: bool = False,
        observation: Optional[Observation] = None,
    ) -> Observation:
        """Act on control ``ref`` (e.g. ``"e7"``) of the last observation and
        return a fresh observation.

        The target's guard is checked inside the desktop, next to the input: if
        the control changed, disappeared or is covered, nothing is dispatched
        and :class:`~solari_core.errors.StaleObservationError` is raised.
        """
        payload: Dict[str, Any] = {"kind": action}
        guard: Optional[str] = None
        label: Optional[str] = ref.id if isinstance(ref, ObservedElement) else ref
        if action in _TARGETED:
            obs = observation or self.last
            if obs is None:
                raise StaleObservationError(label, "unknown", "Nothing observed yet; call observe() first")
            el = obs.element(ref.id if isinstance(ref, ObservedElement) else str(ref))
            if el is None:
                raise StaleObservationError(label, "unknown")
            guard = obs.guard_of(el)
            if guard is None:
                # reflexd skips the check when no guard is sent; never let that happen.
                raise StaleObservationError(label, "stale")
            payload["node"] = el.node
            if action == "type":
                if text is None:
                    raise ValueError('act(..., "type") needs text=')
                payload["text"] = text
            elif action == "select":
                if value is None:
                    raise ValueError('act(..., "select") needs value=')
                payload["value"] = value
        elif action == "press":
            key = key or "Enter"
            if key not in ACT_KEYS:
                raise ValueError(f"key must be one of {', '.join(ACT_KEYS)}; got {key!r}")
            payload["key"] = key
        elif action == "scroll":
            payload["direction"] = direction
        elif action != "wait":
            raise ValueError(f"unknown action {action!r}")

        await self.start()
        r = await self._call("/act", {"action": payload, "guard": guard}, retry=False)
        if isinstance(r, dict) and r.get("error"):
            raise StaleObservationError(label or action, str(r["error"]))
        if action == "type" and submit:
            await self._call("/act", {"action": {"kind": "press", "key": "Enter"}, "guard": None}, retry=False)
        return await self.observe()

    async def launch(
        self,
        command: str,
        args: Optional[List[str]] = None,
        *,
        wait_ms: int = 45_000,
    ) -> Observation:
        """Start a GUI app as the desktop user with the accessibility bridge on,
        and wait until its window is the one observed."""
        await self.start()
        quoted = " ".join(shlex.quote(a) for a in [command, *(args or [])])
        await self._d.commands.run(
            "bash",
            args=["-c", f'runuser -u "$(cat /opt/reflex/user)" -- bash /opt/reflex/launch.sh {quoted}'],
        )
        name = command.rsplit("/", 1)[-1]
        deadline = time.monotonic() + wait_ms / 1000.0
        last: Optional[Observation] = None
        while time.monotonic() < deadline:
            try:
                last = await self.observe()
            except ObserverError:
                last = None
            if last is not None and last.elements and app_matches(last, name):
                return last
            await asyncio.sleep(0.5)
        if last is not None:
            return last
        raise ObserverError(f"{command} did not open a window within {wait_ms}ms")

    # --- transport ------------------------------------------------------------

    async def _call(self, path: str, body: Any, *, retry: bool = True) -> Any:
        if self._base_url:
            try:
                reply = await self._via_preview(path, body)
            except (httpx.HTTPError, ValueError) as exc:
                if not retry:
                    raise ObserverError(f"reflexd {path} over the preview URL failed: {exc}") from exc
                # Safe to repeat (observe/screenshot): drop to exec for good.
                self._base_url = None
                reply = await self._via_exec(path, body)
        else:
            reply = await self._via_exec(path, body)
        if not isinstance(reply, dict):
            raise ObserverError(f"reflexd {path}: unexpected reply")
        if reply.get("error"):
            raise ObserverError(f"reflexd {path}: {reply['error']}")
        return reply.get("result")

    async def _via_preview(self, path: str, body: Any) -> Any:
        assert self._base_url is not None
        res = await self._client().post(
            with_path(self._base_url, path),
            headers={"Authorization": f"Bearer {self._token}"},
            json=body,
            timeout=self._timeout,
        )
        return res.json()

    async def _via_exec(self, path: str, body: Any) -> Any:
        r = await self._d.commands.run(
            "curl",
            args=[
                "-s", "-X", "POST", f"http://127.0.0.1:{REFLEXD_PORT}{path}",
                "-H", f"Authorization: Bearer {self._token}",
                "-H", "Content-Type: application/json",
                "--data-binary", json.dumps(body),
            ],
        )
        try:
            return json.loads(r.stdout)
        except ValueError:
            raise ObserverError(f"reflexd {path}: unreadable reply: {(r.stdout or r.stderr)[:200]}") from None


def app_matches(o: Observation, command: str) -> bool:
    """Whether observation ``o`` is a window of the app started by ``command``."""
    app = o.url.replace("app://", "", 1).split("/")[0].lower()
    c = command.lower()
    if not app:
        return False
    if app in c or c in app:
        return True
    return c == "soffice" and re.search(r"office|calc|writer", f"{app} {o.title}".lower()) is not None


__all__ = [
    "ACT_KEYS",
    "REFLEXD_PORT",
    "DesktopObserver",
    "ObserverTransport",
    "app_matches",
    "install_script",
    "reflexd_gz_b64",
    "reflexd_source",
    "with_path",
]
