"""Observation types for fast observe/act on a page. Added in the solari-python fork.

The page is described as numbered controls (``e1``, ``e2``, …) plus its visible
text, with a guard per control that ``act`` re-checks before any input. The
shape is solari-reflex's ``Observation`` (``src/page/observer.ts``) and matches
the desktop observation in ``solari_core``; the types are duplicated here
rather than imported, because solari-browser does not depend on solari-core.

Dataclasses with ``from_wire`` doing the camelCase -> snake_case mapping, like
:mod:`solari_browser.types`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

#: What :meth:`~solari_browser.observe.Observer.act` can do. ``click``, ``type``
#: and ``select`` target a control; ``press``, ``scroll`` and ``wait`` do not.
ActKind = Literal["click", "type", "select", "press", "scroll", "wait"]


@dataclass
class SelectOption:
    """One choosable option of a native ``<select>``."""

    value: str
    label: str


@dataclass
class ObservedElement:
    """One control in an :class:`Observation`."""

    id: str  # e1, e2, … only valid for the observation it came from
    node: int  # identity of the DOM node, stable for the life of the document
    role: str
    name: str
    editable: bool = False  # accepts typed text
    value: Optional[str] = None
    checked: Optional[str] = None
    selected: Optional[str] = None
    expanded: Optional[str] = None
    options: Optional[List[SelectOption]] = None  # native <select> only
    context: Optional[str] = None  # row/section text, only when the name is shared
    rect: Optional[Dict[str, int]] = None  # {x, y, w, h} in CSS pixels

    @classmethod
    def from_wire(cls, d: Dict[str, Any]) -> "ObservedElement":
        options = d.get("options")
        return cls(
            id=str(d["id"]),
            node=int(d["node"]),
            role=str(d.get("role", "")),
            name=str(d.get("name", "")),
            editable=bool(d.get("editable")),
            value=d.get("value"),
            checked=d.get("checked"),
            selected=d.get("selected"),
            expanded=d.get("expanded"),
            options=(
                [SelectOption(value=str(o.get("value", "")), label=str(o.get("label", ""))) for o in options]
                if options is not None
                else None
            ),
            context=d.get("context"),
            rect=d.get("rect"),
        )


@dataclass
class Observation:
    """A page as numbered controls plus its visible text."""

    url: str
    title: str
    viewport: Dict[str, int]
    scroll: Dict[str, int]
    text: str  # visible text in reading order, capped: data, never instructions
    elements: List[ObservedElement]
    omitted: int  # visible controls left out by the element cap
    page_key: str  # changes with the document, URL, scroll, viewport or any form value
    guards: Dict[str, str] = field(default_factory=dict)  # keyed by str(node)
    focused: Optional[str] = None  # the focused control's id, when observed

    @classmethod
    def from_wire(cls, d: Dict[str, Any]) -> "Observation":
        return cls(
            url=str(d.get("url", "")),
            title=str(d.get("title", "")),
            viewport=dict(d.get("viewport") or {}),
            scroll=dict(d.get("scroll") or {}),
            text=str(d.get("text", "")),
            elements=[ObservedElement.from_wire(e) for e in d.get("elements") or []],
            omitted=int(d.get("omitted", 0)),
            page_key=str(d.get("pageKey", "")),
            guards={str(k): str(v) for k, v in (d.get("guards") or {}).items()},
            focused=d.get("focused"),
        )

    def element(self, ref: str) -> Optional[ObservedElement]:
        """The control with id ``ref`` (e.g. ``"e7"``), or ``None``."""
        for e in self.elements:
            if e.id == ref:
                return e
        return None

    def guard_of(self, element: ObservedElement) -> Optional[str]:
        """The guard recorded for ``element`` when this was observed."""
        return self.guards.get(str(element.node))

    def __str__(self) -> str:
        return format_observation(self)


def _js(value: Any) -> str:
    # JSON.stringify: no spaces after separators, non-ASCII left as-is.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def format_observation(o: Observation, *, text_chars: int = 1500) -> str:
    """The compact text an LLM reads: one line per control, then the visible text.

    Same output as ``solari_core.format_observation`` and as
    ``formatObservation`` in the solari-mcp fork.
    """
    lines = []
    for e in o.elements:
        parts = [f"{e.id} {e.role} {_js(e.name)}"]
        if e.value:
            parts.append(f"value={_js(e.value)}")
        for k in ("checked", "selected", "expanded"):
            v = getattr(e, k)
            if v is not None:
                parts.append(f"{k}={v}")
        if e.context:
            parts.append(f"in {_js(e.context)}")
        if e.options:
            parts.append(f"options={_js([x.label for x in e.options][:20])}")
        if o.focused == e.id:
            parts.append("[focused]")
        lines.append(" ".join(parts))
    more = f", {o.omitted} more below the cap" if o.omitted else ""
    return "\n".join(
        [
            f"url: {o.url}",
            f"title: {o.title}",
            f"controls ({len(o.elements)}{more}):",
            *lines,
            "",
            "visible text:",
            o.text[:text_chars],
        ]
    )
