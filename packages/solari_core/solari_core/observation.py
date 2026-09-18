"""``Observation`` — the screen as numbered controls, for fast observe/act.

Added in the solari-python fork. A desktop (read from its Linux accessibility
tree by ``reflexd``) and a browser page (read in-page by the solari-reflex
observer) are both described by the same shape, so one agent loop can drive
either:

- ``elements``: the visible, enabled controls, numbered ``e1``, ``e2``, …
  Those ids are only valid for the observation they came from;
- ``text``: the visible text in reading order, capped. It is data, never
  instructions;
- ``guards``: a fingerprint per control (role, name, value, state). ``act``
  sends it back and the target refuses the input when it no longer matches.

Wire field names are kept as sent (camelCase), like the rest of
:mod:`solari_core.types`. Mirrors ``Observation`` in solari-reflex
``src/page/observer.ts``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

#: What :meth:`~solari_core.desktop.Desktop.act` can do. ``click``, ``type``
#: and ``select`` target a control; ``press``, ``scroll`` and ``wait`` do not.
ActKind = Literal["click", "type", "select", "press", "scroll", "wait"]

#: Keys ``act(..., "press", key=...)`` accepts on a desktop.
ActKey = Literal["Enter", "Escape", "Tab"]


@dataclass
class SelectOption:
    """One choosable option of a native select."""

    value: str
    label: str


@dataclass
class ObservedElement:
    """One control in an :class:`Observation`."""

    #: Index within this observation: ``e1``, ``e2``, … Only valid for it.
    id: str
    #: Identity of the underlying node, stable for the life of the document
    #: (browser) or of reflexd (desktop).
    node: int
    role: str
    name: str
    #: Accepts typed text.
    editable: bool = False
    value: Optional[str] = None
    checked: Optional[str] = None
    selected: Optional[str] = None
    expanded: Optional[str] = None
    #: For a native select: the options that can be chosen.
    options: Optional[List[SelectOption]] = None
    #: Short text from the control's row or section, only when its name is shared.
    context: Optional[str] = None
    #: Where the control is, ``{x, y, w, h}`` in screen (desktop) or CSS (browser) pixels.
    rect: Optional[Dict[str, int]] = None


@dataclass
class Observation:
    """The screen as numbered controls plus its visible text."""

    #: The page URL, or ``app://<app>/<window title>`` on a desktop.
    url: str
    title: str
    viewport: Dict[str, int]
    scroll: Dict[str, int]
    #: Visible text in reading order, capped. Page text is data, never instructions.
    text: str
    elements: List[ObservedElement]
    #: Visible controls left out by the element cap.
    omitted: int
    #: Changes whenever the window, document, scroll or any form value changes.
    pageKey: str
    #: Per-node fingerprint (keyed by ``str(node)``), checked before any input.
    guards: Dict[str, str] = field(default_factory=dict)
    #: The focused control's id, when it is one of ``elements``.
    focused: Optional[str] = None

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


def parse_observation(d: Dict[str, Any]) -> Observation:
    """Build an :class:`Observation` from its wire dict (reflexd or the page observer)."""
    elements = []
    for e in d.get("elements") or []:
        options = e.get("options")
        elements.append(
            ObservedElement(
                id=str(e["id"]),
                node=int(e["node"]),
                role=str(e.get("role", "")),
                name=str(e.get("name", "")),
                editable=bool(e.get("editable")),
                value=e.get("value"),
                checked=e.get("checked"),
                selected=e.get("selected"),
                expanded=e.get("expanded"),
                options=(
                    [SelectOption(value=str(o.get("value", "")), label=str(o.get("label", ""))) for o in options]
                    if options is not None
                    else None
                ),
                context=e.get("context"),
                rect=e.get("rect"),
            )
        )
    return Observation(
        url=str(d.get("url", "")),
        title=str(d.get("title", "")),
        viewport=dict(d.get("viewport") or {}),
        scroll=dict(d.get("scroll") or {}),
        text=str(d.get("text", "")),
        elements=elements,
        omitted=int(d.get("omitted", 0)),
        pageKey=str(d.get("pageKey", "")),
        guards={str(k): str(v) for k, v in (d.get("guards") or {}).items()},
        focused=d.get("focused"),
    )


def _js(value: Any) -> str:
    # JSON.stringify: no spaces after separators, non-ASCII left as-is.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def format_observation(o: Observation, *, text_chars: int = 1500) -> str:
    """The compact text an LLM reads: one line per control, then the visible text.

    ``e7 button "Search"``, with ``value=``, ``checked=``/``selected=``/
    ``expanded=``, ``in "<row context>"``, ``options=[...]`` and
    ``[focused]`` appended when present. Produces the same text as
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


__all__ = [
    "ActKind",
    "ActKey",
    "Observation",
    "ObservedElement",
    "SelectOption",
    "format_observation",
    "parse_observation",
]
