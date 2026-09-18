"""Offline tests for the fork's Observer (fast observe/act on a page).

No browser: a fake patchright page interprets the few observer calls Observer
makes (observe, guard + locate, guard + choose, settle), keeps a guard per
node, and records every mouse and keyboard call, so these assert that a stale
or covered target is refused before any input is sent.

``fixtures/format_expected.json`` is golden output of ``formatObservation``
from the solari-mcp fork; format_observation() must match it byte for byte.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from solari_browser import (
    OBSERVER_VERSION,
    STALE_OBSERVATION,
    Observation,
    Observer,
    SolariError,
    StaleObservationError,
    format_observation,
)
from solari_browser.observe import INSTALL_OBSERVER


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


FIXTURES = Path(__file__).parent / "fixtures"
WIRE = json.loads((FIXTURES / "observation.json").read_text(encoding="utf-8"))
EXPECTED = json.loads((FIXTURES / "format_expected.json").read_text(encoding="utf-8"))


class _Input:
    def __init__(self, log: List[tuple], page: "FakePage") -> None:
        self._log = log
        self._page = page

    async def click(self, x: float, y: float) -> None:
        self._log.append(("click", x, y))
        self._page.after_click()

    async def move(self, x: float, y: float) -> None:
        self._log.append(("move", x, y))

    async def wheel(self, dx: float, dy: float) -> None:
        self._log.append(("wheel", dx, dy))

    async def press(self, key: str) -> None:
        self._log.append(("press", key))

    async def insert_text(self, text: str) -> None:
        self._log.append(("insert_text", text))
        self._page.value = text


class FakePage:
    """Enough of a patchright Page for Observer: evaluate, mouse, keyboard, events."""

    def __init__(self) -> None:
        self.installed = False
        self.value = ""
        self.search_name = "Search Wikipedia"
        self.covered = False
        self.input: List[tuple] = []
        self.evaluates: List[str] = []
        self.listeners: Dict[str, List[Callable]] = {}
        self.main_frame = object()
        self.navigate_on_click = False
        self.fail_observes = 0
        self.settles: List[Optional[int]] = []
        self.mouse = _Input(self.input, self)
        self.keyboard = self.mouse

    # --- events
    def on(self, event: str, handler: Callable) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Callable) -> None:
        self.listeners[event].remove(handler)

    def emit(self, event: str, arg: Any) -> None:
        for h in list(self.listeners.get(event, [])):
            h(arg)

    def after_click(self) -> None:
        if self.navigate_on_click:
            page = self

            class _Req:
                frame = page.main_frame

                def is_navigation_request(self) -> bool:
                    return True

            self.emit("request", _Req())
            self.installed = False  # a new document has no observer
            asyncio.get_running_loop().call_later(0.01, self.emit, "domcontentloaded", self)

    # --- the observer, as seen through evaluate
    def guard(self, node: int) -> Optional[str]:
        if node == 9:
            return json.dumps([9, "searchbox", self.search_name, self.value])
        if node == 21:
            return json.dumps([21, "select", "Language", "English"])
        return None

    def observation(self) -> Dict[str, Any]:
        elements = [
            {"id": "e1", "node": 9, "role": "searchbox", "name": self.search_name, "editable": True},
            {"id": "e2", "node": 21, "role": "select", "name": "Language", "editable": False, "value": "English",
             "options": [{"value": "de", "label": "Deutsch"}]},
        ]
        if self.value:
            elements[0]["value"] = self.value
        return {
            "url": "https://en.wikipedia.org/wiki/Main_Page", "title": "Wikipedia",
            "viewport": {"width": 1280, "height": 800}, "scroll": {"y": 0, "height": 3000},
            "text": "Welcome to Wikipedia", "elements": elements, "omitted": 0,
            "pageKey": f"pk-{self.value}", "guards": {9: self.guard(9), 21: self.guard(21)},
        }

    async def evaluate(self, expression: str) -> Any:
        self.evaluates.append(expression)
        prefix = f"window.__reflex?.version === {OBSERVER_VERSION} ? ((r) => "
        if expression.startswith(prefix):
            if not self.installed:
                return "__reflex_missing__"
            body = expression[len(prefix):expression.rindex(")(window.__reflex)")]
        elif expression.startswith("((r) => ") and expression.endswith(f"}})({OBSERVER_VERSION}))"):
            assert INSTALL_OBSERVER in expression
            self.installed = True
            body = expression[len("((r) => "):expression.index(")((function installObserver")]
        else:
            raise AssertionError(f"unexpected evaluate: {expression[:80]}")
        return self.run(body)

    def run(self, body: str) -> Any:
        if body.startswith("r.observe("):
            if self.fail_observes > 0:
                self.fail_observes -= 1
                raise RuntimeError("Execution context was destroyed, most likely because of a navigation")
            opts = json.loads(body[len("r.observe("):-1])
            assert opts == {"maxElements": 120, "maxTextChars": 4000}
            return self.observation()
        m = re.fullmatch(r"r\.settle\((null|\d+), 250\)\.then\(\(\) => (r\.observe\(.*\))\)", body)
        if m:
            self.settles.append(None if m.group(1) == "null" else int(m.group(1)))
            return self.run(m.group(2))
        m = re.fullmatch(r'r\.guard\((\d+)\) !== (.*) \? "stale" : r\.(locate|choose)\((\d+), (.*)\)', body)
        assert m, body
        node, expected, op = int(m.group(1)), json.loads(m.group(2)), m.group(3)
        if self.guard(node) != expected:
            return "stale"
        if op == "locate":
            return None if self.covered else {"x": 640.5, "y": 42}
        return json.loads(m.group(5)) == "de"


# ---- format ----------------------------------------------------------------------


def test_format_matches_the_ts_helper() -> None:
    o = Observation.from_wire(WIRE)
    assert o.page_key == "k" and o.guards == {"4": "g4", "9": "g9"}
    assert format_observation(o) == EXPECTED["default"]
    assert format_observation(o, text_chars=10) == EXPECTED["short"]
    assert str(o) == EXPECTED["default"]


def test_observer_source_is_the_vendored_install_expression() -> None:
    assert INSTALL_OBSERVER.startswith("(function installObserver(version) {")
    assert INSTALL_OBSERVER.endswith(f"}})({OBSERVER_VERSION})")
    assert "window.__reflex = observer" in INSTALL_OBSERVER


# ---- observe ---------------------------------------------------------------------


@sync
async def test_observe_installs_the_observer_once_per_document() -> None:
    page = FakePage()
    o = Observer(page)
    first = await o.observe()
    assert first.elements[0].name == "Search Wikipedia" and o.last is first
    assert len(page.evaluates) == 2  # missing, then install + observe in one evaluate
    await o.observe()
    assert len(page.evaluates) == 3  # installed: one evaluate per observe


@sync
async def test_observe_retries_while_the_document_is_replaced() -> None:
    page = FakePage()
    page.installed = True
    page.fail_observes = 2
    obs = await Observer(page).observe()
    assert obs.url.startswith("https://en.wikipedia.org")


@sync
async def test_observe_gives_up_on_a_page_that_never_settles() -> None:
    page = FakePage()
    page.installed = True
    page.fail_observes = 100
    with pytest.raises(SolariError, match="could not be observed"):
        await Observer(page).observe()


# ---- act -------------------------------------------------------------------------


@sync
async def test_type_clicks_the_located_point_replaces_and_inserts_the_text() -> None:
    page = FakePage()
    o = Observer(page)
    await o.observe()
    after = await o.act("e1", "type", text="Gödel", submit=True)
    assert page.input == [
        ("click", 640.5, 42),
        ("press", "ControlOrMeta+a"),
        ("insert_text", "Gödel"),
        ("press", "Enter"),
    ]
    assert after.element("e1").value == "Gödel"
    assert o.last is after
    # Settling (waiting for the search box's suggestions) and the fresh
    # observation are one evaluate, after the guard/locate one.
    assert page.settles == [9]
    assert len(page.evaluates) == 2 + 2
    assert page.listeners == {"request": [], "domcontentloaded": []}


@sync
async def test_a_changed_target_is_refused_before_any_input() -> None:
    page = FakePage()
    o = Observer(page)
    await o.observe()
    page.search_name = "Search Wiktionary"
    with pytest.raises(StaleObservationError) as err:
        await o.act("e1", "type", text="x")
    assert err.value.reason == "stale" and err.value.ref == "e1"
    assert err.value.code == STALE_OBSERVATION
    assert page.input == []


@sync
async def test_a_covered_target_is_refused_before_any_input() -> None:
    page = FakePage()
    o = Observer(page)
    await o.observe()
    page.covered = True
    with pytest.raises(StaleObservationError) as err:
        await o.act("e1", "click")
    assert err.value.reason == "covered"
    assert page.input == []


@sync
async def test_acting_against_an_old_observation_is_refused() -> None:
    page = FakePage()
    o = Observer(page)
    old = await o.observe()
    await o.act("e1", "type", text="first")
    with pytest.raises(StaleObservationError):
        await o.act("e1", "type", text="second", observation=old)
    assert [i for i in page.input if i[0] == "insert_text"] == [("insert_text", "first")]


@sync
async def test_a_new_document_invalidates_every_target() -> None:
    page = FakePage()
    o = Observer(page)
    obs = await o.observe()
    page.installed = False  # navigated: new document, observer not installed
    page.value = "changed"
    with pytest.raises(StaleObservationError):
        await o.act("e1", "click", observation=obs)
    assert page.input == []


@sync
async def test_unknown_ref_and_acting_before_observing_are_refused() -> None:
    page = FakePage()
    o = Observer(page)
    with pytest.raises(StaleObservationError, match="observe\\(\\) first"):
        await o.act("e1", "click")
    await o.observe()
    with pytest.raises(StaleObservationError) as err:
        await o.act("e42", "click")
    assert err.value.reason == "unknown"
    assert page.input == []


@sync
async def test_select_chooses_in_page_and_refuses_unknown_options() -> None:
    page = FakePage()
    o = Observer(page)
    await o.observe()
    await o.act("e2", "select", value="de")
    with pytest.raises(StaleObservationError) as err:
        await o.act("e2", "select", value="xx")
    assert err.value.reason == "option"
    assert page.input == []


@sync
async def test_press_and_scroll_need_no_target() -> None:
    page = FakePage()
    o = Observer(page)
    await o.observe()
    await o.act(None, "press", key="Escape")
    await o.act(None, "scroll", direction="up")
    assert page.input == [("press", "Escape"), ("move", 640, 400), ("wheel", 0, -560)]


@sync
async def test_waits_for_a_navigation_the_action_started() -> None:
    page = FakePage()
    page.navigate_on_click = True
    o = Observer(page)
    await o.observe()
    after = await o.act("e1", "click")
    assert after.elements  # observed the new document (re-installed the observer)
    assert page.installed
    assert page.listeners == {"request": [], "domcontentloaded": []}


def test_bad_arguments_raise_before_anything_is_sent() -> None:
    page = FakePage()
    o = Observer(page)
    asyncio.run(o.observe())
    with pytest.raises(ValueError):
        asyncio.run(o.act("e1", "type"))
    with pytest.raises(ValueError):
        asyncio.run(o.act("e1", "hover"))  # type: ignore[arg-type]
    assert page.input == []
