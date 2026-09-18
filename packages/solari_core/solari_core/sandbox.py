"""``Sandbox`` — a handle to one live headless session.

All of its surface lives on the shared
:class:`~solari_desktop.handle.SessionHandle` base (commands, pty, run_code,
files, volumes, metrics, snapshot, revert, pause/resume, set_timeout,
env, kill). Construct via :class:`~solari_desktop.sandbox_client.SandboxClient`.
Mirrors ``sdk/src/sandbox.ts``.
"""

from __future__ import annotations

from typing import Optional

from .handle import SessionConfig, SessionHandle
from .types import CreateSandboxResponse


class Sandbox(SessionHandle):
    """A live headless sandbox session."""

    def __init__(
        self,
        session: CreateSandboxResponse,
        config: Optional[SessionConfig] = None,
    ) -> None:
        super().__init__(session.sandboxId, session.controlUrl, session.expiresAt, config)

    @property
    def sandboxId(self) -> str:
        """Alias of :attr:`SessionHandle.id`."""
        return self.id


__all__ = ["Sandbox"]
