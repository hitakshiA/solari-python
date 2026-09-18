# solari-browser (Python)

Python SDK for **Solari Browser** — managed, stealth-hardened remote Chromium as
an API, driven over the Playwright wire protocol (or raw CDP). A faithful port of
the TypeScript `@solarisdk/browser` package; class, method, and field names match
it one-for-one (snake_case where TS uses camelCase).

The browser runs remotely on Solari's pool — `pip install` pulls the driver only;
you do **not** run `patchright install chromium` locally.

## Install

```sh
pip install solari-browser
```

## Quickstart

```python
import asyncio, os
from solari_browser import Solari

async def main():
    solari = Solari(api_key=os.environ["SOLARI_API_KEY"])
    browser = await solari.launch(stealth=True)   # managed, stealth-hardened Chromium
    page = await browser.new_page()
    await page.goto("https://example.com")
    print(await page.title())
    await browser.close()

asyncio.run(main())
```

## Sessions

Drive a session yourself instead of `launch()`:

```python
solari = Solari(api_key=os.environ["SOLARI_API_KEY"])
session = await solari.sessions.create(stealth=True, proxy="smart")
# ... connect over the session's CDP/WS endpoint ...
await solari.close()
```

`Solari(...)` accepts `api_key`, `region` (default `us-west`), and `base_url`
(default `https://api.getsolari.com`).

## Docs

Full reference and language guides: <https://getsolari.com>
