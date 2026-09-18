# solari-python (solari-fast fork)

> **This is a source fork of Solari's Python SDK**, which is published only on PyPI: [`solari-core`](https://pypi.org/project/solari-core/) 0.2.1, [`solari-desktop`](https://pypi.org/project/solari-desktop/) 0.2.0, [`solari-sandbox`](https://pypi.org/project/solari-sandbox/) 0.2.1 and [`solari-browser`](https://pypi.org/project/solari-browser/) 0.1.3. The first commit is those four sdists exactly as published. Every commit after it is one of our changes, with its reason. Solari's own READMEs are unchanged, in each package folder.

## What this fork adds

Through this SDK, an agent could look at a Solari desktop only as a screenshot, and act on it only by pixel coordinates. A browser session gave it page HTML, screenshots or its own `page.evaluate` scripts, and CSS selectors to act with. Every step needed a vision model, or a model writing JavaScript.

This fork adds the view the [solari-reflex](https://github.com/hitakshiA/solari-reflex) agent and the [solari-mcp fork](https://github.com/hitakshiA/solari-mcp) use. The screen is numbered controls plus its visible text, and every action names one of those controls.

| API | What it does |
|---|---|
| `desktop.observe()` | Returns the active window as an `Observation`: numbered controls (`e7 button "Save"`), their values and state, and the visible text. It's read from the Linux accessibility tree by `reflexd`, a small daemon installed into the desktop on first use. |
| `desktop.act(ref, action, text=, value=, key=, submit=)` | Clicks, types, selects, presses a key or scrolls, then **returns the fresh observation in the same call**. |
| `desktop.launch(command, args)` | Starts an app as the desktop user with accessibility on, then waits for its window. Apps started with `open()` expose no controls. |
| `Observer(page).observe()` / `.act(...)` | The same for a browser session's page, read in-page by the solari-reflex observer. Input goes through `page.mouse` and `page.keyboard`. |
| `format_observation(obs)` | The compact text an LLM reads. It's byte-identical to the MCP fork's `formatObservation`. |

Two rules hold for every action:
- **Stale targets are refused.** Each act is checked against the observation it came from, next to the input: inside the desktop, or in the page's own evaluate. If the control changed, is covered or disappeared, nothing is dispatched and `StaleObservationError` says why (`reason` is `"stale"`, `"gone"`, `"covered"`, `"option"` or `"unknown"`).
- **No coordinates from the model.** The model names an observed control. It never supplies coordinates, selectors or script.

### Measured

One live run of [`scripts/live_check.py`](scripts/live_check.py) against api.getsolari.com (us-west), from a client about 250 ms per round trip away. Single runs, wall-clock times from the client.

**Desktop:** created with `SandboxClient.create_desktop(disk_gb=16)`. `reflexd` was reached over the preview URL.

| Step | Time |
|---|---|
| Create the desktop | 6.2 s |
| First use: accessibility on, install and start `reflexd` (once per desktop) | 9.5 s |
| `launch("mousepad")`, until its window is observed | 0.59 s |
| `observe()` (10 controls) | 0.28 s |
| `act(e10, "type", text=…)`, including the fresh observation | 0.68 s |
| `observe()` again | 0.28 s |

The desktop was deleted in a `finally`. The list API then showed no machines running.

**Browser:** a fast-pool session on en.wikipedia.org at 1280×800.

| Step | Time |
|---|---|
| `observe()`, first call on the page (installs the observer in the same evaluate) | 0.59 s |
| `observe()` (54 controls) | 0.30 s |
| `act(e2, "type", text="Gödel's incompleteness theorems")`, including the fresh observation and its suggestion list | 1.73 s |
| `observe()` again | 0.28 s |

A browser observe is one round trip. A type act is five: guard and locate, click, select-all, insert the text, then settle and observe.

### Changes, commit by commit

1. **Import the four sdists unmodified.** solari-core, solari-desktop and solari-sandbox are MIT; solari-browser is Apache-2.0.
2. **Ignore the local venv, caches and `.env`.**
3. **`solari_desktop`: depend on `solari-core==0.2.1`.** Upstream pins `==0.2.0`, while solari-sandbox 0.2.1 pins `==0.2.1`. From PyPI, `pip install solari-desktop solari-sandbox` quietly falls back to sandbox 0.2.0 and core 0.2.0. Installing from source fails outright.
4. **Vendor the solari-reflex observer and `reflexd`,** with `scripts/sync_reflex.py` to regenerate them from a solari-reflex checkout (both come from commit `d50025f`).
   - `solari_browser/observer.js` is `installObserver`, taken from the built `dist/page/observer.js`.
   - `solari_core/reflexd/reflexd.py` is copied byte for byte and ships as package data.
5. **`solari_core`: `Observation`, `ObservedElement` and `format_observation()`.** The tests hold the formatter to golden output from the TS `formatObservation`.
6. **`solari_core`: `desktop.observe()`, `act()` and `launch()`,** plus the typed errors `StaleObservationError` and `ObserverError`. The `Desktop` handle lives in solari-core and is returned by both `DesktopClient.create()` and `SandboxClient.create_desktop()`, so the methods live there too; solari-desktop re-exports them.
   - **Install.** `reflexd` is sent gzipped through the existing `commands.run`, since Solari's HTTP exec rejects bodies over 16 KB.
   - **Transport.** It's reached through `preview_url(7788)`, falling back to `commands.run("curl", …)`.
   - **Safer than the TS reference in three places:**
     - A target with no recorded guard is refused, because `reflexd` skips its check when the guard is null.
     - `press` accepts only `Enter`, `Escape` and `Tab`, because `reflexd` presses Return for any other key.
     - An act whose preview request failed is not retried through exec, because the input may already have landed.
   - **Preview URLs.** Solari's preview URLs carry the token as `?pt_token=` with no path, so request paths are set with `urlsplit`. Appending `"/observe"`, as the TS reference does, puts it inside the token.
7. **`solari_browser`: `Observer(page)`.**
   - **Isolated world.** The observer is installed as `window.__reflex` in patchright's isolated world, so page scripts can't see or change it.
   - **Guard check.** It shares one evaluate with locating the target.
   - **Input.** A trusted click at the located point, then select-all and a single `keyboard.insert_text` rather than one key per character.
   - **Settle.** Settling and the fresh observation are one evaluate. If the action started a navigation, the result is discarded and the new document is observed after its `DOMContentLoaded`.
8. **Unique test module names,** so `pytest packages/*/tests` runs every suite in one go.
9. **Mark the two solari-browser files the fork changed,** as Apache-2.0 section 4(b) asks.
10. **This README, `NOTICE` and `scripts/live_check.py`.**

No existing API was removed or changed; the only edit to existing behaviour is the dependency pin. The upstream test suite (28 tests in solari-browser) still passes, alongside 44 new tests. Run everything with `pytest packages/*/tests`.

### Install

```bash
pip install \
  "solari-core @ git+https://github.com/hitakshiA/solari-python#subdirectory=packages/solari_core" \
  "solari-sandbox @ git+https://github.com/hitakshiA/solari-python#subdirectory=packages/solari_sandbox" \
  "solari-desktop @ git+https://github.com/hitakshiA/solari-python#subdirectory=packages/solari_desktop" \
  "solari-browser @ git+https://github.com/hitakshiA/solari-python#subdirectory=packages/solari_browser"
```

Install `solari-core` from this fork alongside the others. The PyPI release has the same version number but none of the observe/act code.

### Use it: desktop

```python
import asyncio, os
from solari_sandbox import SandboxClient, StaleObservationError, format_observation

async def main():
    client = SandboxClient(api_key=os.environ["SOLARI_API_KEY"], base_url="https://api.getsolari.com")
    # The unified /sandboxes route honours disk_gb; DesktopClient.create() has no disk option.
    desktop = await client.create_desktop(disk_gb=16, resolution="1280x800")
    try:
        obs = await desktop.launch("mousepad")       # accessibility on, waits for the window
        print(format_observation(obs))
        # url: app://mousepad/Untitled 1 - Mousepad
        # controls (10):
        # e1 button "Minimize"
        # ...
        # e10 textbox "textbox" [focused]
        area = next(e for e in obs.elements if e.editable)
        obs = await desktop.act(area.id, "type", text="Hello from Python")
        try:
            obs = await desktop.act("e4", "click")    # "File"
        except StaleObservationError as err:
            print(err.reason)                          # nothing was clicked; observe and decide again
            obs = await desktop.observe()
    finally:
        await desktop.kill()
        await client.aclose()

asyncio.run(main())
```

`desktop.observer` is the underlying `DesktopObserver`. `desktop.observer.transport` tells you whether calls go over the preview URL or exec. Construct one yourself to force `transport="exec"` or change the element and text caps.

### Use it: browser

```python
import asyncio, os
from solari_browser import Observer, Solari, format_observation

async def main():
    async with Solari(api_key=os.environ["SOLARI_API_KEY"]) as solari:
        async with await solari.launch() as browser:
            context = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await context.new_page()
            await page.goto("https://en.wikipedia.org")
            observer = Observer(page)
            obs = await observer.observe()
            search = next(e for e in obs.elements if e.role == "searchbox")
            obs = await observer.act(search.id, "type", text="Gödel's incompleteness theorems")
            print(format_observation(obs))             # the suggestion list is now in the controls
            obs = await observer.act(None, "press", key="Enter")

asyncio.run(main())
```

### Upstream issues found along the way

These are in Solari's published packages. This fork fixes only the dependency pin.

- **The desktop and sandbox packages can't be installed together at their latest versions** (fixed here, commit 3).
- **The READMEs say `base_url` defaults to `https://api.getsolari.com`,** but `DesktopClient` and `SandboxClient` require it. The quickstarts, `DesktopClient(api_key=...)`, raise `TypeError`.
- **`__version__` disagrees with the package metadata:**
  - solari-core says `0.2.0` but is 0.2.1;
  - solari-sandbox says `0.2.0` but is 0.2.1;
  - solari-browser says `0.1.2` but is 0.1.3.
- **`commands.run(timeout_ms=...)` is accepted and ignored;** it's never sent to the guest.
- **`solari_core/ws.py` imports the deprecated `websockets.legacy` client,** while the dependency is `websockets>=11` with no upper bound.
- **`DesktopClient.create()` has no disk-size option.** Only `SandboxClient.create_desktop(disk_gb=...)` does.

## Solari's original READMEs

Each package's README is Solari's, unchanged:

- [`packages/solari_core/README.md`](packages/solari_core/README.md)
- [`packages/solari_desktop/README.md`](packages/solari_desktop/README.md)
- [`packages/solari_sandbox/README.md`](packages/solari_sandbox/README.md)
- [`packages/solari_browser/README.md`](packages/solari_browser/README.md)

See [NOTICE](NOTICE) for licences and credits.
