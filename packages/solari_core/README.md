# solari-core

Shared runtime for the Solari VM SDKs — the HTTP/WebSocket transport, the
`Desktop` and `Sandbox` session handles, the image builder, and the typed error
hierarchy. Mirrors the TypeScript `@solarisdk/core` package.

You normally install a leaf package instead:

- [`solari-desktop`](https://pypi.org/project/solari-desktop/) — computer-use desktops
- [`solari-sandbox`](https://pypi.org/project/solari-sandbox/) — code sandboxes

Both depend on and re-export `solari-core`, so `SandboxClient` / `DesktopClient`
plus the shared types are importable straight from the leaf package.

Docs: <https://getsolari.com>
