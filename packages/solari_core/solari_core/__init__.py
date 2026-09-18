"""solari_core — shared runtime for the Solari VM SDKs.

The transport, session handles (:class:`Desktop`, :class:`Sandbox`), image
builder, and typed errors shared by ``solari-desktop`` and ``solari-sandbox``.
Mirrors the TypeScript ``@solarisdk/core`` package. You normally install one of
the leaf packages (``solari-desktop`` / ``solari-sandbox``), which re-export
this surface; import from here directly only for shared types.
"""
from __future__ import annotations

from ._http import HttpTransport, new_idempotency_key
from .desktop import Desktop, DesktopConfig, ExecStreamHandler
from .handle import SessionConfig, SessionHandle, SessionHooks
from .image import CompiledImage, Image, LocalCopy
from .observation import (
    ActKey, ActKind, Observation, ObservedElement, SelectOption,
    format_observation, parse_observation,
)
from .sandbox import Sandbox
from .template_client import SyncTemplateClient, TemplateClient
from .volume_client import SyncVolumeClient, VolumeClient
from .errors import (
    ActionError, AuthError, ConcurrencyLimitError, ConnectionError,
    GatewayError, NoCapacityError, SolariError, PlanError, TimeoutError,
)
from .types import (
    CodeLanguage, CodeResultItem, CommandResult, CreateDesktopResponse,
    CreateSandboxResponse, DeleteDesktopResponse, DesktopLifecycleResponse,
    DesktopStatus, ExecResult, ExecStreamChunk, FsEntry, FsSearchMatch, FsStat,
    FsWatchEvent, GatewayErrorBody, GitBranch, GitCommit, GitStatus,
    GetDesktopResponse, HealthResult, KeyAction, MetricsResult, MouseAction,
    MouseButton, PackageManager, PkgInstallResult, PortInfo, ProcessInfo,
    RpcErrorBody, RpcResponse, RunCodeResult, SandboxKind, SandboxState,
    SandboxView, ScreenshotFormat, SnapshotView,
)

__version__ = "0.2.0"

__all__ = [
    "HttpTransport", "new_idempotency_key",
    "Image", "CompiledImage", "LocalCopy",
    "TemplateClient", "SyncTemplateClient", "VolumeClient", "SyncVolumeClient",
    "Desktop", "DesktopConfig", "ExecStreamHandler", "Sandbox",
    "SessionHandle", "SessionConfig", "SessionHooks",
    "ActionError", "AuthError", "ConcurrencyLimitError", "ConnectionError",
    "GatewayError", "NoCapacityError", "SolariError", "PlanError", "TimeoutError",
    "CreateDesktopResponse", "CreateSandboxResponse", "DeleteDesktopResponse",
    "DesktopLifecycleResponse", "DesktopStatus", "ExecResult", "ExecStreamChunk",
    "FsEntry", "FsStat", "GatewayErrorBody", "GetDesktopResponse", "HealthResult",
    "KeyAction", "MouseAction", "MouseButton", "PackageManager", "PkgInstallResult",
    "PortInfo", "ProcessInfo", "RpcErrorBody", "RpcResponse", "ScreenshotFormat",
    "CodeLanguage", "CodeResultItem", "CommandResult", "FsSearchMatch",
    "FsWatchEvent", "GitBranch", "GitCommit", "GitStatus", "MetricsResult",
    "RunCodeResult", "SandboxKind", "SandboxState", "SandboxView", "SnapshotView",
    # solari-python fork: fast observe/act
    "ActKey", "ActKind", "Observation", "ObservedElement", "SelectOption",
    "format_observation", "parse_observation",
]
