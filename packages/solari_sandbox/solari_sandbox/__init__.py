"""solari_sandbox — fast, ephemeral code sandboxes (micro-VMs) with a code
interpreter, filesystem, and git. Mirrors the TypeScript ``@solarisdk/sandbox``
package.

Import :class:`SandboxClient` to create and drive sandboxes; the shared handles
and types are re-exported from :mod:`solari_core`.
"""
from __future__ import annotations

from solari_core import *  # noqa: F401,F403  (shared surface)
from solari_core import __all__ as _core_all
from .sandbox_client import SandboxClient, SyncSandboxClient

__version__ = "0.2.0"
__all__ = list(_core_all) + ["SandboxClient", "SyncSandboxClient"]
