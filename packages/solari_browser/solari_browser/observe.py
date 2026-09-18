"""``Observer`` — fast observe/act on a session's page. Added in the solari-python fork.

Reading a page through a Solari session meant ``page.content()``, a
screenshot, or custom ``page.evaluate`` scripts, and acting meant CSS selectors
or pixel coordinates. ``Observer`` gives the view the solari-reflex agent uses:

    observer = Observer(page)
    obs = await observer.observe()          # numbered controls + visible text
    print(format_observation(obs))          # e7 searchbox "Search Wikipedia" …
    obs = await observer.act("e7", "type", text="Gödel", submit=True)

- ``observe()`` is one ``page.evaluate`` of the solari-reflex observer
  (``observer.js``, installed as ``window.__reflex`` in patchright's isolated
  world, so page scripts cannot see or alter it). It returns the visible,
  enabled, uncovered controls, their values and state, the visible text, a
  page key and a guard per control.
- ``act()`` checks the target's guard and resolves its current position in the
  same evaluate, refuses a control that changed or is now covered (nothing is
  dispatched), then sends trusted input through ``page.mouse`` and
  ``page.keyboard`` (typing is one ``insert_text``, not a key per character).
  It then waits for what the action should produce (two animation frames, a
  suggestion list, or a new document) and returns a fresh observation.

Model output never becomes a selector, a coordinate or script: every action
names an observed control, and input goes to where that control is now.
Mirrors ``BrowserPage`` in solari-reflex ``src/browser.ts``.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Optional, Union

from .errors import SolariError, StaleObservationError
from .observation import ActKind, Observation, ObservedElement

_SOURCE = (Path(__file__).parent / "observer.js").read_text(encoding="utf-8")
_VERSION = re.search(r"OBSERVER_VERSION = (\d+)", _SOURCE)
if _VERSION is None:  # pragma: no cover - guarded by scripts/sync_reflex.py
    raise ImportError("solari_browser/observer.js has no OBSERVER_VERSION header")

#: Version of the vendored solari-reflex page observer.
OBSERVER_VERSION = int(_VERSION.group(1))
#: Expression that installs the observer in the page and returns it.
INSTALL_OBSERVER = "(" + _SOURCE[_SOURCE.index("function installObserver"):].strip() + f")({OBSERVER_VERSION})"

_MISSING = "__reflex_missing__"
_TARGETED = ("click", "type", "select")


class Observer:
    """Observe and act on one patchright ``Page`` of a Solari session.

    :param page: A page from ``BrowserSession.new_page()`` (or any patchright page).
    :param max_elements: Controls offered per observation.
    :param max_text_chars: Visible text sent with each observation.
    :param suggestion_wait_ms: Longest wait for a suggestion list after typing.
    :param navigation_wait_ms: Longest wait for a new document after an action.
    """

    def __init__(
        self,
        page: Any,
        *,
        max_elements: int = 120,
        max_text_chars: int = 4000,
        suggestion_wait_ms: int = 250,
        navigation_wait_ms: int = 10_000,
    ) -> None:
        self.page = page
        self.max_elements = max_elements
        self.max_text_chars = max_text_chars
        self.suggestion_wait_ms = suggestion_wait_ms
        self.navigation_wait_ms = navigation_wait_ms
        #: The last observation returned, which ``act`` resolves ids against.
        self.last: Optional[Observation] = None

    async def observe(self) -> Observation:
        """Read the page. Retries briefly while a document is being replaced."""
        options = json.dumps({"maxElements": self.max_elements, "maxTextChars": self.max_text_chars})
        last_err: Optional[BaseException] = None
        for _ in range(20):
            try:
                raw = await self._call(f"r.observe({options})")
            except Exception as err:  # noqa: BLE001 - "Execution context was destroyed" mid-navigation
                raw, last_err = None, err
            if raw:
                self.last = Observation.from_wire(raw)
                return self.last
            await asyncio.sleep(0.05)
        raise SolariError("Solari: the page kept changing and could not be observed", None, last_err)

    async def act(
        self,
        ref: Union[str, ObservedElement, None],
        action: ActKind = "click",
        *,
        text: Optional[str] = None,
        value: Optional[str] = None,
        key: Optional[str] = None,
        direction: str = "down",
        submit: bool = False,
        observation: Optional[Observation] = None,
    ) -> Observation:
        """Act on control ``ref`` (e.g. ``"e7"``) of the last observation and
        return a fresh observation.

        ``action`` is ``click``, ``type`` (with ``text``; ``submit=True`` presses
        Enter after), ``select`` (with ``value``, for a native ``<select>``),
        ``press`` (``key`` in Playwright's key syntax, default ``Enter``; ``ref``
        is ignored), ``scroll`` (``direction`` ``"up"``/``"down"``) or ``wait``.

        Raises :class:`~solari_browser.errors.StaleObservationError`, without
        dispatching anything, when the target changed, is covered or is gone.
        """
        el: Optional[ObservedElement] = None
        label = ref.id if isinstance(ref, ObservedElement) else ref
        if action in _TARGETED:
            obs = observation or self.last
            if obs is None:
                raise StaleObservationError(label, "unknown", "Nothing observed yet; call observe() first")
            el = obs.element(ref.id if isinstance(ref, ObservedElement) else str(ref))
            if el is None:
                raise StaleObservationError(label, "unknown")
            if action == "type" and text is None:
                raise ValueError('act(..., "type") needs text=')
            if action == "select" and value is None:
                raise ValueError('act(..., "select") needs value=')
            expected = json.dumps(obs.guard_of(el))
        elif action not in ("press", "scroll", "wait"):
            raise ValueError(f"unknown action {action!r}")

        watch = _NavigationWatch(self.page)
        try:
            if el is not None and action == "select":
                ok = await self._call(
                    f'r.guard({el.node}) !== {expected} ? "stale" : r.choose({el.node}, {json.dumps(value)})'
                )
                if ok == "stale":
                    raise StaleObservationError(label, "stale")
                if not ok:
                    raise StaleObservationError(label, "option")
            elif el is not None:
                # Guard check and current geometry in one evaluate.
                at = await self._call(
                    f'r.guard({el.node}) !== {expected} ? "stale" '
                    f": r.locate({el.node}, {'true' if el.editable else 'false'})"
                )
                if at == "stale":
                    raise StaleObservationError(label, "stale")
                if not at:
                    raise StaleObservationError(label, "covered")
                await self.page.mouse.click(at["x"], at["y"])
                if action == "type":
                    await self.page.keyboard.press("ControlOrMeta+a")
                    await self.page.keyboard.insert_text(text or "")
                    if submit:
                        await self.page.keyboard.press("Enter")
            elif action == "press":
                await self.page.keyboard.press(key or "Enter")
            elif action == "scroll":
                seen = observation or self.last
                viewport = seen.viewport if seen is not None else {}
                w, h = viewport.get("width", 1280), viewport.get("height", 800)
                await self.page.mouse.move(w / 2, h / 2)
                await self.page.mouse.wheel(0, (-1 if direction == "up" else 1) * round(h * 0.7))
            else:
                await asyncio.sleep(0.1)
            return await self._settle_and_observe(el.node if el is not None and action == "type" else None, watch)
        finally:
            watch.stop()

    async def _settle_and_observe(self, node: Optional[int], watch: "_NavigationWatch") -> Observation:
        """Wait for what the action should produce, then observe, in one evaluate:
        two animation frames, or a suggestion list for ``node``. If the main
        frame started loading a new document, that result is discarded and the
        new document is observed after its DOMContentLoaded."""
        options = json.dumps({"maxElements": self.max_elements, "maxTextChars": self.max_text_chars})
        target = "null" if node is None else str(node)
        try:
            raw = await self._call(f"r.settle({target}, {self.suggestion_wait_ms}).then(() => r.observe({options}))")
        except Exception:  # noqa: BLE001 - the document was replaced mid-wait; observe the new one
            raw = None
        if watch.started:
            try:
                await asyncio.wait_for(watch.loaded.wait(), timeout=self.navigation_wait_ms / 1000.0)
            except asyncio.TimeoutError:
                pass
            return await self.observe()
        if raw:
            self.last = Observation.from_wire(raw)
            return self.last
        return await self.observe()

    async def _call(self, body: str) -> Any:
        """Run ``body`` against the installed observer, bound as ``r``. Installs
        it in the same evaluate when this document does not have it yet."""
        expression = (
            f"window.__reflex?.version === {OBSERVER_VERSION} "
            f'? ((r) => {body})(window.__reflex) : "{_MISSING}"'
        )
        result = await self.page.evaluate(expression)
        if result == _MISSING:
            result = await self.page.evaluate(f"((r) => {body})({INSTALL_OBSERVER})")
        return result


class _NavigationWatch:
    """Watches the main frame for a new document from before an action is dispatched."""

    def __init__(self, page: Any) -> None:
        self._page = page
        self.started = False
        self.loaded = asyncio.Event()

        def _on_request(request: Any) -> None:
            try:
                if request.is_navigation_request() and request.frame == page.main_frame:
                    self.started = True
            except Exception:  # noqa: BLE001 - e.g. service-worker requests have no frame
                pass

        def _on_dcl(_page: Any) -> None:
            if self.started:
                self.loaded.set()

        self._handlers = (("request", _on_request), ("domcontentloaded", _on_dcl))
        for event, handler in self._handlers:
            page.on(event, handler)

    def stop(self) -> None:
        for event, handler in self._handlers:
            try:
                self._page.remove_listener(event, handler)
            except Exception:  # noqa: BLE001
                pass


__all__ = ["Observer", "OBSERVER_VERSION", "INSTALL_OBSERVER"]
