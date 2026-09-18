# solari-desktop (Python)

Python SDK for **Solari Desktop** — managed, hardware-isolated Linux desktops
with a computer-use action API (mouse, keyboard, screenshot), driven over a
WebSocket control channel. Mirrors the TypeScript `@solarisdk/desktop` package.

## Install
```sh
pip install solari-desktop
```

## Quickstart
```python
import asyncio, os
from solari_desktop import DesktopClient

async def main():
    client = DesktopClient(api_key=os.environ["SOLARI_API_KEY"])
    desktop = await client.create()          # managed Linux desktop
    await desktop.connect()
    await desktop.screenshot()
    await desktop.kill()

asyncio.run(main())
```

`DesktopClient(...)` accepts `api_key`, `base_url` (default
`https://api.getsolari.com`), and `call_timeout_ms`. The same `slr_live_…` key
authenticates against both the browser and desktop/sandbox APIs.

Docs: <https://getsolari.com>
