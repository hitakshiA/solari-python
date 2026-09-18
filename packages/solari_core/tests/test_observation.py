"""Offline tests for the fork's Observation types and format_observation().

``fixtures/format_expected.json`` was produced by running ``formatObservation``
from the solari-mcp fork (``dist/fast.js``) on ``fixtures/observation.json``,
so these assert the Python helper writes byte-identical text.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from solari_core import (
    Observation,
    ObservedElement,
    SelectOption,
    format_observation,
    parse_observation,
)

FIXTURES = Path(__file__).parent / "fixtures"
WIRE = json.loads((FIXTURES / "observation.json").read_text(encoding="utf-8"))
EXPECTED = json.loads((FIXTURES / "format_expected.json").read_text(encoding="utf-8"))


def obs() -> Observation:
    return parse_observation(WIRE)


def test_parse_keeps_every_field() -> None:
    o = obs()
    assert o.url == WIRE["url"]
    assert o.pageKey == "k"
    assert o.omitted == 3
    assert o.focused == "e2"
    assert [e.id for e in o.elements] == [f"e{i}" for i in range(1, 8)]
    e6 = o.element("e6")
    assert e6 is not None and e6.options == [
        SelectOption(value="de", label="Deutsch"),
        SelectOption(value="fr", label="Français"),
    ]
    assert o.element("e3").checked == "false"
    assert o.element("e2").editable is True
    assert o.element("e99") is None


def test_guards_are_keyed_by_node_as_string() -> None:
    o = parse_observation({**WIRE, "guards": {4: "g4"}})
    assert o.guards == {"4": "g4"}
    assert o.guard_of(o.element("e1")) == "g4"
    assert o.guard_of(o.element("e2")) is None


def test_format_matches_the_ts_helper() -> None:
    assert format_observation(obs()) == EXPECTED["default"]
    assert str(obs()) == EXPECTED["default"]


def test_format_caps_visible_text() -> None:
    assert format_observation(obs(), text_chars=10) == EXPECTED["short"]


def test_format_lists_at_most_twenty_options() -> None:
    base = obs()
    many = replace(
        base,
        elements=[replace(base.element("e6"), options=[SelectOption(str(i), f"L{i}") for i in range(25)])],
        omitted=0,
        focused=None,
    )
    assert format_observation(many) == EXPECTED["many"]
    assert '"L19"]' in format_observation(many)


def test_format_empty_screen() -> None:
    assert format_observation(replace(obs(), elements=[], omitted=0, text="")) == EXPECTED["empty"]


def test_format_line_shape() -> None:
    o = Observation(
        url="app://mousepad/Untitled 1",
        title="Untitled 1",
        viewport={"width": 1280, "height": 800},
        scroll={"y": 0, "height": 800},
        text="",
        elements=[ObservedElement(id="e1", node=7, role="textbox", name="", editable=True, value="hi")],
        omitted=0,
        pageKey="x",
        focused="e1",
    )
    assert format_observation(o).splitlines()[3] == 'e1 textbox "" value="hi" [focused]'
