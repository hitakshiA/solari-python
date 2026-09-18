"""solari_desktop — managed, hardware-isolated Linux desktops with a
computer-use action API. Mirrors the TypeScript ``@solarisdk/desktop`` package.

Import :class:`DesktopClient` to create and drive desktops; the shared handles
and types are re-exported from :mod:`solari_core`.
"""
from __future__ import annotations

from solari_core import *  # noqa: F401,F403  (shared surface)
from solari_core import __all__ as _core_all
from .client import DesktopClient, SyncDesktopClient

__version__ = "0.2.0"
__all__ = list(_core_all) + ["DesktopClient", "SyncDesktopClient"]
