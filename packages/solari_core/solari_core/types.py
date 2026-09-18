"""Wire types for ``solari_desktop``.

Per the repo rule, types are DUPLICATED on each side of a network boundary and
never imported across it. These shapes mirror ``docs/CONTRACTS.md`` §1 (the
SDK <-> Gateway public API) and §4 (the host-agent <-> guest-agent computer-use
JSON-RPC API, which the gateway tunnels over the control WebSocket).

The TypeScript SDK expresses these as ``interface``/``type`` declarations. In
Python we mirror them with :class:`~dataclasses.dataclass` for the structured
response/result records and ``Literal`` aliases for the string-enum types, plus
``TypedDict`` for the loose option bags that callers pass in as keyword
arguments (so the methods accept idiomatic kwargs rather than dict literals).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Union

# ---------------------------------------------------------------------------
# §1 — SDK <-> Gateway (HTTPS)
# ---------------------------------------------------------------------------

#: Session lifecycle status reported by ``GET /desktops/:sessionId``.
DesktopStatus = Literal["starting", "ready", "paused", "releasing", "gone"]


@dataclass
class CreateDesktopResponse:
    """``201`` response from ``POST /desktops``."""

    #: Signed, opaque session capability: ``<poolId>:<vmId>:<orgId>.<sig>``.
    sessionId: str
    #: ``wss://`` URL serving plain RFB (VNC) bytes for embedding the live view.
    streamUrl: str
    #: ``wss://`` URL for the newline-delimited JSON-RPC control channel.
    controlUrl: str
    #: ISO-8601 expiry timestamp.
    expiresAt: str
    #: Presigned playback URL, present when ``record=True``.
    recordingUrl: Optional[str] = None


@dataclass
class GetDesktopResponse:
    """``GET /desktops/:sessionId`` response."""

    sessionId: str
    status: DesktopStatus
    expiresAt: str
    #: Presigned playback URL, present when the session was recorded.
    recordingUrl: Optional[str] = None


@dataclass
class DesktopLifecycleResponse:
    """Response from ``POST /desktops/:id/pause`` and ``.../resume``."""

    sessionId: str
    status: DesktopStatus


@dataclass
class DeleteDesktopResponse:
    """``DELETE /desktops/:sessionId`` response (idempotent)."""

    ok: bool = True


@dataclass
class GatewayErrorBody:
    """Shape gateway error bodies may take."""

    code: Optional[str] = None
    error: Optional[str] = None
    message: Optional[str] = None
    # Gateway hint that the failure is transient (e.g. 503 "No desktop host
    # available"). The HTTP transport retries idempotent requests when set.
    retryable: Optional[bool] = None


# ---------------------------------------------------------------------------
# CONTRACTS-V2 §1/§2 — Sandboxes + Snapshots (SDK <-> Gateway)
# ---------------------------------------------------------------------------

#: ``kind`` of a session: headless ``sandbox`` or GUI ``desktop``.
SandboxKind = Literal["sandbox", "desktop"]

#: Lifecycle state reported by ``GET /sandboxes/:id``.
SandboxState = Literal[
    "starting", "running", "paused", "archived", "releasing", "gone"
]


@dataclass
class CreateSandboxResponse:
    """``201`` response from ``POST /sandboxes``."""

    sandboxId: str
    kind: SandboxKind
    controlUrl: str
    expiresAt: str
    #: Present when ``kind == "desktop"``.
    streamUrl: Optional[str] = None
    #: Presigned playback URL, present when ``record=True`` on a desktop-kind
    #: create. Resolves once the guest uploads the mp4 on ``record.stop()``.
    recordingUrl: Optional[str] = None


@dataclass
class SandboxView:
    """``GET /sandboxes/:id`` response."""

    sandboxId: str
    kind: SandboxKind
    state: SandboxState
    metadata: dict
    expiresAt: str
    cpu: int
    memMb: int
    #: Effective disk size (GiB), host-reported at create.
    diskGb: int = 0
    #: Presigned playback URL, present for a recorded desktop-kind session.
    recordingUrl: Optional[str] = None


@dataclass
class MetricsResult:
    """``GET /sandboxes/:id/metrics`` (also ``metrics()`` on a handle)."""

    cpuPct: float
    memBytes: int
    memTotalBytes: int
    diskBytes: int


@dataclass
class SnapshotView:
    """Snapshot record (``GET /snapshots``, ``GET /snapshots/:id``)."""

    id: str
    parent: Optional[str]
    name: Optional[str]
    sizeBytes: int
    createdAt: str
    kind: SandboxKind
    template: str


@dataclass
class CommandResult:
    """Terminal result of ``commands.run``."""

    exitCode: int
    stdout: str
    stderr: str


@dataclass
class FsSearchMatch:
    """A match from ``files.search``."""

    path: str
    line: int
    text: str


@dataclass
class FsWatchEvent:
    """A filesystem watch event (``fs.event`` async frame)."""

    type: str
    path: str


ChartType = Literal[
    "line", "scatter", "bar", "pie", "box_and_whisker", "composite", "unknown"
]


@dataclass
class ChartAxis:
    """One axis of a 2D chart."""

    label: Optional[str] = None
    ticks: Optional["list[Any]"] = None
    scale: Optional[str] = None


@dataclass
class Chart:
    """Structured matplotlib figure (mirrors TS ``Chart``). ``elements`` holds
    per-type data (points/bars/slices); kept loose for forward-compat."""

    type: ChartType = "unknown"
    title: Optional[str] = None
    xLabel: Optional[str] = None
    yLabel: Optional[str] = None
    x: Optional[ChartAxis] = None
    y: Optional[ChartAxis] = None
    elements: Optional["list[Any]"] = None


@dataclass
class CodeResultItem:
    """One rich result object from ``code.run``."""

    type: Literal["stdout", "stderr", "result"]
    text: Optional[str] = None
    png: Optional[str] = None
    jpeg: Optional[str] = None
    svg: Optional[str] = None
    html: Optional[str] = None
    latex: Optional[str] = None
    json: Any = None
    markdown: Optional[str] = None
    chart: Optional[Chart] = None


@dataclass
class RunCodeResult:
    """Result of ``run_code``."""

    results: "list[CodeResultItem]"
    error: Any = None
    #: All structured charts across ``results`` (flattened convenience view).
    charts: "list[Chart]" = field(default_factory=list)


#: Language accepted by ``run_code``.
CodeLanguage = Literal["python", "javascript", "typescript", "bash", "r"]


# ---------------------------------------------------------------------------
# git — first-class version-control namespace (mirrors TS). Implemented in the
# SDK as safe, non-shell ``git`` invocations over the command RPC.
# ---------------------------------------------------------------------------


@dataclass
class GitStatus:
    """Working-tree status (``git.status``)."""

    branch: str = ""
    detached: bool = False
    ahead: int = 0
    behind: int = 0
    staged: "list[str]" = field(default_factory=list)
    modified: "list[str]" = field(default_factory=list)
    untracked: "list[str]" = field(default_factory=list)
    clean: bool = True


@dataclass
class GitBranch:
    """One branch entry (``git.branches``)."""

    name: str
    commit: str
    current: bool


@dataclass
class GitCommit:
    """One commit record (``git.log``)."""

    hash: str
    author: str
    email: str
    date: str
    message: str


# ---------------------------------------------------------------------------
# §4 — computer-use JSON-RPC (tunneled over the control WebSocket)
# ---------------------------------------------------------------------------

# --- exec -------------------------------------------------------------------


@dataclass
class ExecResult:
    exitCode: int
    stdout: str
    stderr: str


# --- fs ---------------------------------------------------------------------


@dataclass
class FsEntry:
    name: str
    dir: bool
    size: int


@dataclass
class FsStat:
    name: str
    dir: bool
    size: int
    #: Unix permission bits (e.g. 0o644).
    mode: int
    #: Modification time in unix-millis.
    modTimeMs: int


@dataclass
class ExecStreamChunk:
    """A streamed exec output chunk delivered to a stream callback."""

    #: Which stream the bytes came from.
    stream: Literal["stdout", "stderr"]
    #: Decoded UTF-8 text of the chunk.
    text: str
    #: Raw bytes of the chunk.
    bytes: bytes


# --- input.mouse ------------------------------------------------------------

MouseButton = Literal["left", "right", "middle"]
MouseAction = Literal["move", "click", "down", "up", "scroll"]

# --- input.key --------------------------------------------------------------

KeyAction = Literal["press", "down", "up"]

# --- screenshot -------------------------------------------------------------

ScreenshotFormat = Literal["png", "jpeg"]

# --- process ----------------------------------------------------------------


@dataclass
class ProcessInfo:
    pid: int
    name: str
    cmd: Optional[str] = None


# --- ports ------------------------------------------------------------------


@dataclass
class PortInfo:
    """One listening TCP socket reported by ``ports.list``."""

    port: int
    #: Local bind address (e.g. ``0.0.0.0``, ``::``).
    addr: str
    #: Owning process pid when resolvable.
    pid: Optional[int] = None


# --- pkg --------------------------------------------------------------------

PackageManager = Literal["apt", "pip", "npm"]


@dataclass
class PkgInstallResult:
    exitCode: int
    stdout: str
    stderr: str


# --- health -----------------------------------------------------------------


@dataclass
class HealthResult:
    ready: bool
    display: bool
    vnc: bool


# --- RPC frames -------------------------------------------------------------

#: Loose JSON value type used across the control channel.
JsonValue = Union[None, bool, int, float, str, list, dict]


@dataclass
class RpcResponse:
    """A reply frame read from the control channel."""

    id: str
    ok: bool
    result: JsonValue = None
    error: Union[str, "RpcErrorBody", None] = None


@dataclass
class RpcErrorBody:
    code: Optional[str] = None
    message: Optional[str] = None


__all__ = [
    "DesktopStatus",
    "CreateDesktopResponse",
    "GetDesktopResponse",
    "DesktopLifecycleResponse",
    "DeleteDesktopResponse",
    "GatewayErrorBody",
    "ExecResult",
    "ExecStreamChunk",
    "FsEntry",
    "FsStat",
    "MouseButton",
    "MouseAction",
    "KeyAction",
    "ScreenshotFormat",
    "ProcessInfo",
    "PortInfo",
    "PackageManager",
    "PkgInstallResult",
    "HealthResult",
    "JsonValue",
    "RpcResponse",
    "RpcErrorBody",
    # v2 — sandboxes/snapshots
    "SandboxKind",
    "SandboxState",
    "CreateSandboxResponse",
    "SandboxView",
    "MetricsResult",
    "SnapshotView",
    "CommandResult",
    "FsSearchMatch",
    "FsWatchEvent",
    "CodeResultItem",
    "RunCodeResult",
    "CodeLanguage",
]
