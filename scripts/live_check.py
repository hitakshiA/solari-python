#!/usr/bin/env python3
"""One cheap live check of the fork's observe/act against real Solari machines.

    SOLARI_API_KEY=... python scripts/live_check.py [desktop] [browser]

Desktop: creates one desktop (16 GB disk), launches mousepad with
accessibility on, observes it, types into its text area, observes again, and
deletes the desktop in a ``finally``. It then lists sandboxes to confirm none
is left running.

Browser: creates a session, opens https://en.wikipedia.org, observes, types
into the search box, observes again, and releases the session.

Prints wall-clock timings for each step. The key is read from the environment
and never printed.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any, Awaitable, Dict, List, Tuple

BASE_URL = os.environ.get("SOLARI_BASE_URL", "https://api.getsolari.com")


async def timed(label: str, rows: List[Tuple[str, float]], aw: Awaitable[Any]) -> Any:
    t0 = time.perf_counter()
    try:
        return await aw
    finally:
        ms = (time.perf_counter() - t0) * 1000
        rows.append((label, ms))
        print(f"  {ms:8.0f} ms  {label}", flush=True)


async def desktop_check(key: str) -> Dict[str, Any]:
    from solari_sandbox import SandboxClient, format_observation

    rows: List[Tuple[str, float]] = []
    sbx = SandboxClient(api_key=key, base_url=BASE_URL)
    desktop = None
    out: Dict[str, Any] = {"rows": rows}
    try:
        print("desktop:")
        desktop = await timed(
            "create desktop (16 GB disk)", rows,
            sbx.create_desktop(disk_gb=16, resolution="1280x800", timeout_ms=900_000),
        )
        await timed("connect control channel", rows, desktop.connect())
        await timed("install reflexd (first use)", rows, desktop.observer.start())
        out["transport"] = desktop.observer.transport
        print(f"  transport: {out['transport']}")
        obs = await timed("launch mousepad and wait for its window", rows, desktop.launch("mousepad"))
        obs = await timed("observe", rows, desktop.observe())
        print(format_observation(obs, text_chars=200))
        area = next((e for e in obs.elements if e.editable), None)
        if area is None:
            raise RuntimeError("no editable control in mousepad")
        after = await timed(
            f"act {area.id} type (returns fresh observation)", rows,
            desktop.act(area.id, "type", text="Typed through solari-python observe/act."),
        )
        again = after.element(area.id)
        print(f"  value after act: {again.value if again else None!r}")
        out["typed"] = bool(again and again.value and "solari-python" in again.value)
        await timed("observe again", rows, desktop.observe())
        return out
    finally:
        if desktop is not None:
            try:
                await desktop.close()
            except Exception:  # noqa: BLE001
                pass
            await sbx.kill(desktop.id)
            print("  deleted desktop")
        running = [s async for s in sbx.list_all(state="running")]
        starting = [s async for s in sbx.list_all(state="starting")]
        out["left_running"] = len(running) + len(starting)
        print(f"  machines still running or starting: {out['left_running']}")
        await sbx.aclose()


async def browser_check(key: str) -> Dict[str, Any]:
    from solari_browser import Observer, Solari, format_observation

    rows: List[Tuple[str, float]] = []
    out: Dict[str, Any] = {"rows": rows}
    async with Solari(api_key=key, base_url=BASE_URL) as solari:
        print("browser:")
        browser = await timed("create session and connect", rows, solari.launch())
        try:
            context = await timed(
                "new context (1280x800)", rows, browser.new_context(viewport={"width": 1280, "height": 800})
            )
            page = await timed("new page", rows, context.new_page())
            await timed(
                "goto en.wikipedia.org", rows, page.goto("https://en.wikipedia.org", wait_until="domcontentloaded")
            )
            observer = Observer(page)
            obs = await timed("observe (installs the observer)", rows, observer.observe())
            obs = await timed("observe", rows, observer.observe())
            print(format_observation(obs, text_chars=200))
            search = next(
                e for e in obs.elements if e.editable and e.role in ("searchbox", "combobox", "textbox")
            )
            after = await timed(
                f"act {search.id} type (returns fresh observation)", rows,
                observer.act(search.id, "type", text="Gödel's incompleteness theorems"),
            )
            now = after.element(search.id)
            print(f"  value after act: {now.value if now else None!r}")
            out["typed"] = bool(now and now.value == "Gödel's incompleteness theorems")
            await timed("observe again", rows, observer.observe())
        finally:
            await timed("release session", rows, browser.close())
    return out


async def main(which: List[str]) -> int:
    key = os.environ.get("SOLARI_API_KEY")
    if not key:
        print("set SOLARI_API_KEY", file=sys.stderr)
        return 2
    ok = True
    if "desktop" in which:
        d = await desktop_check(key)
        ok = ok and d.get("typed", False) and d.get("left_running") == 0
    if "browser" in which:
        b = await browser_check(key)
        ok = ok and b.get("typed", False)
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:] or ["desktop", "browser"])))
