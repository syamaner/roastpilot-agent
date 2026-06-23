"""Typed wrapper over the coffee-roaster-mcp stdio server (component plan §2, §4).

E5-S1: Pydantic mirrors of every tool result shape, derived from the
actual coffee-roaster-mcp source (`mcp_server.py` dataclasses, v0.1.3
surface verified in plan §2 — unchanged through v0.1.5, the pinned
version: 0.1.4 added the `mic-check` CLI and 0.1.5 made a transient mic
overflow recoverable, neither touching the 13-tool surface;
mcp-contract-checker confirmed zero drift) and validated against the
7 Jun 2026 live-roast exports. The mirrors use ``extra="ignore"`` so new optional
upstream fields never break the agent — drift is detected by the
mcp-contract-checker sub-agent and the contract fixtures (E5-S3), not by
runtime crashes.

E5-S2 adds the transport: the client owns the MCP child process (D6 —
spawn `coffee-roaster-mcp serve`, health, per-call timeouts,
restart → recovery). Until then the client takes an injectable async
tool-caller and owns shape validation only.

Neither the advisor nor the SPA ever sees this client: every write
command arrives via explicit controller methods carrying a
SafetyEvaluation. All temperatures are Celsius; derived metrics are
passed through from MCP, never recomputed.
"""

import asyncio
import json
import math
import os
import shutil
import sys
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, ConfigDict

from roastpilot_agent.config import DEFAULT_MCP_COMMAND, MCPConfig
from roastpilot_agent.models import MicStatus, RoastTelemetry

# --- vocabulary mirrored from coffee_roaster_mcp (session.py / config.py) ---

#: MCP's own phase machine (session.py) — inputs to the agent's phase
#: mapping (plan §3); distinct from the agent's models.RoastPhase.
MCPPhase = Literal[
    "pre_roast",
    "roasting",
    "development",
    "dropped",
    "cooling",
    "complete",
    "fault",
]

FirstCrackMode = Literal["disabled", "audio", "manual"]
FirstCrackRuntimeStatus = Literal[
    "disabled", "manual", "pending", "detected", "faulted", "unavailable"
]
T0RuntimeStatus = Literal["disabled", "pending", "detected", "unavailable"]
EventPayloadValue = str | int | float | bool | None


class MCPMirror(BaseModel):
    """Base for all mirrors: immutable, tolerant of new upstream fields."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class EventSnapshot(MCPMirror):
    """Mirror of mcp_server.EventSnapshot.

    ``payload`` is deliberately permissive: a manual ``beans_added`` event
    carries an empty payload while auto-T0 carries source/charge/drop
    metadata (verified against both 7 Jun live-roast sessions).

    With coffee-roaster-mcp v0.1.7 (#169/#170) the event ``monotonic_seconds``
    is **backdated** to the turning-point (``beans_added``) / crack-onset
    (``first_crack_detected``) instant, and the ``payload`` carries the raw
    confirmation moment so a consumer can recover the lag the server corrected:

    - ``beans_added``: ``turning_point_monotonic_seconds`` (== the backdated
      ``monotonic_seconds``) + ``confirmed_at_monotonic_seconds`` +
      ``confirmed_at_utc``.
    - ``first_crack_detected``: ``detected_at_monotonic_seconds`` (== the
      backdated ``monotonic_seconds``) + ``confirmed_at_monotonic_seconds``.

    All three monotonic values live in the **MCP** ``time.monotonic`` domain,
    which is a *different* domain to the agent's clock (the MCP is a separate
    child process, D6). Only the in-domain *difference*
    (``confirmed_at − onset``) is meaningful agent-side — see
    :func:`event_backdate_seconds`.
    """

    kind: str
    recorded_at_utc: str
    monotonic_seconds: float
    payload: dict[str, EventPayloadValue]


class RoasterDeviceState(MCPMirror):
    """Mirror of mcp_server.RoasterDeviceState."""

    driver: str
    connected: bool
    bean_temp_c: float | None
    env_temp_c: float | None
    heat_level_percent: int
    fan_level_percent: int
    cooling_on: bool
    raw_vendor_data: dict[str, EventPayloadValue]


class FirstCrackStatus(MCPMirror):
    """Mirror of mcp_server.FirstCrackStatus (audio pipeline counters feed
    the dashboard diagnostics drawer, plan §7)."""

    mode: FirstCrackMode
    status: FirstCrackRuntimeStatus
    detected_at_utc: str | None
    detected_monotonic_seconds: float | None
    allow_manual_override: bool
    reason: str | None = None
    audio_running: bool = False
    queued_window_count: int = 0
    emitted_window_count: int = 0
    dropped_window_count: int = 0
    processed_window_count: int = 0


class T0Status(MCPMirror):
    """Mirror of mcp_server.T0Status."""

    auto_detection_enabled: bool
    status: T0RuntimeStatus
    charge_temperature_c: float | None
    current_drop_c: float | None
    drop_threshold_c: float
    detected_bean_temperature_c: float | None
    reason: str | None = None


class RoastSessionState(MCPMirror):
    """Mirror of mcp_server.RoastSessionState — the get_roast_state result
    the controller's tick consumes (via RoastTelemetry projection)."""

    session_id: str
    active: bool
    phase: MCPPhase
    created_at_utc: str
    stopped_at_utc: str | None
    elapsed_monotonic_seconds: float
    heat_level_percent: int
    fan_level_percent: int
    cooling_on: bool
    beans_added_at_utc: str | None
    first_crack_at_utc: str | None
    beans_dropped_at_utc: str | None
    cooling_started_at_utc: str | None
    cooling_stopped_at_utc: str | None
    faulted_at_utc: str | None
    beans_added_monotonic_seconds: float | None
    first_crack_monotonic_seconds: float | None
    beans_dropped_monotonic_seconds: float | None
    cooling_started_monotonic_seconds: float | None
    cooling_stopped_monotonic_seconds: float | None
    faulted_monotonic_seconds: float | None
    roast_elapsed_seconds: float | None
    development_time_seconds: float | None
    development_percent: float | None
    bean_temp_delta_60s_c: float | None
    env_temp_delta_60s_c: float | None
    bean_ror_c_per_min: float | None
    env_ror_c_per_min: float | None
    device_state: RoasterDeviceState | None
    t0_status: T0Status
    first_crack_status: FirstCrackStatus
    events: tuple[EventSnapshot, ...]
    log_dir: str | None


class ServerInfo(MCPMirror):
    """Mirror of mcp_server.ServerInfo."""

    product_name: str
    package_name: str
    version: str
    transport: str
    # Deliberately str, not MCPPhase: this is the project/runtime phase
    # label (e.g. "bootstrap" in the captured 0.1.3 fixture), not the
    # roast phase.
    current_phase: str
    roaster_driver: str
    first_crack_mode: str
    bootstrap_safe: bool
    available_bootstrap_tools: tuple[str, ...]
    started_at_utc: str


class RuntimeConfigSnapshot(MCPMirror):
    """Mirror of mcp_server.RuntimeConfigSnapshot."""

    config_source: str | None
    roaster_driver: str
    roaster_port: str | None
    roaster_baudrate: int
    temperature_unit: str
    command_interval_seconds: float
    first_crack_mode: str
    model_repo_id: str
    model_precision: str
    allow_manual_override: bool
    log_dir: str
    sample_interval_seconds: float
    auto_t0_detection_enabled: bool
    auto_t0_drop_threshold_c: float


class StartRoastSessionResult(MCPMirror):
    """Mirror of mcp_server.StartRoastSessionResult."""

    session: RoastSessionState


class ControlCommandResult(MCPMirror):
    """Mirror of mcp_server.ControlCommandResult (set_heat / set_fan)."""

    session_id: str
    phase: MCPPhase
    heat_level_percent: int
    fan_level_percent: int
    cooling_on: bool


class EventCommandResult(MCPMirror):
    """Mirror of mcp_server.EventCommandResult (mark_*, drop, cooling,
    emergency_stop)."""

    session_id: str
    phase: MCPPhase
    event: EventSnapshot
    event_count: int


class ExportRoastLogResult(MCPMirror):
    """Mirror of mcp_server.ExportRoastLogResult."""

    session_id: str
    log_dir: str
    jsonl_path: str
    csv_path: str
    summary_path: str
    ready: bool
    note: str


#: Injectable transport: (tool_name, arguments) -> raw result payload.
#: E5-S2's stdio child-process session implements this with per-call
#: timeouts; tests inject fakes.
ToolCaller = Callable[[str, dict[str, object]], Awaitable[object]]


class RoasterMCPClient:
    """Typed client for exactly the verified 13-tool MCP surface.

    Every method validates the raw result into its mirror — there is no
    arbitrary tool-execution surface. Child-process lifecycle (spawn,
    health, restart → recovery, per-call timeouts) lands in E5-S2.
    """

    def __init__(self, call_tool: ToolCaller) -> None:
        self._call = call_tool

    async def get_server_info(self) -> ServerInfo:
        return ServerInfo.model_validate(await self._call("get_server_info", {}))

    async def get_runtime_config(self) -> RuntimeConfigSnapshot:
        return RuntimeConfigSnapshot.model_validate(await self._call("get_runtime_config", {}))

    async def start_roast_session(self) -> StartRoastSessionResult:
        return StartRoastSessionResult.model_validate(await self._call("start_roast_session", {}))

    async def get_roast_state(self, session_id: str | None = None) -> RoastSessionState:
        # Omit the key entirely when defaulted: JSON-RPC servers commonly
        # treat an explicit null differently from an absent optional arg.
        args: dict[str, object] = {} if session_id is None else {"session_id": session_id}
        return RoastSessionState.model_validate(await self._call("get_roast_state", args))

    async def set_heat(self, heat_level_percent: int) -> ControlCommandResult:
        return ControlCommandResult.model_validate(
            await self._call("set_heat", {"heat_level_percent": heat_level_percent})
        )

    async def set_fan(self, fan_level_percent: int) -> ControlCommandResult:
        return ControlCommandResult.model_validate(
            await self._call("set_fan", {"fan_level_percent": fan_level_percent})
        )

    async def mark_beans_added(self) -> EventCommandResult:
        return EventCommandResult.model_validate(await self._call("mark_beans_added", {}))

    async def mark_first_crack(self) -> EventCommandResult:
        return EventCommandResult.model_validate(await self._call("mark_first_crack", {}))

    async def drop_beans(self) -> EventCommandResult:
        return EventCommandResult.model_validate(await self._call("drop_beans", {}))

    async def start_cooling(self) -> EventCommandResult:
        return EventCommandResult.model_validate(await self._call("start_cooling", {}))

    async def stop_cooling(self) -> EventCommandResult:
        return EventCommandResult.model_validate(await self._call("stop_cooling", {}))

    async def export_roast_log(self, session_id: str | None = None) -> ExportRoastLogResult:
        args: dict[str, object] = {} if session_id is None else {"session_id": session_id}
        return ExportRoastLogResult.model_validate(await self._call("export_roast_log", args))

    async def emergency_stop(self, reason: str = "manual emergency stop") -> EventCommandResult:
        return EventCommandResult.model_validate(
            await self._call("emergency_stop", {"reason": reason})
        )


# --- E9: controller-protocol adapter over the typed client ---


def project_mic_status(status: FirstCrackStatus) -> MicStatus:
    """Project an MCP ``FirstCrackStatus`` into the SPA-facing ``MicStatus`` (#197).

    A pure, read-only observability projection — no safety logic, no MCP write.
    It forwards only the capture-alive fields the MCP already computes (the Pi
    performance constraint: no per-window level work, #33) and lets
    :meth:`MicStatus.from_first_crack_status` derive the health the icon maps to.

    Args:
        status: The MCP first-crack status from ``RoastSessionState``.

    Returns:
        The capture-alive mic status with its derived :class:`MicHealth`.
    """
    return MicStatus.from_first_crack_status(
        status=status.status,
        audio_running=status.audio_running,
        queued_window_count=status.queued_window_count,
        emitted_window_count=status.emitted_window_count,
        dropped_window_count=status.dropped_window_count,
        processed_window_count=status.processed_window_count,
        reason=status.reason,
    )


def _payload_float(payload: dict[str, EventPayloadValue], key: str) -> float | None:
    """Read a finite numeric payload field as a float, or ``None``.

    Booleans are rejected (``bool`` is an ``int`` subclass but never a valid
    timestamp), as are non-finite values — the result feeds a clock delta, so a
    garbage value must collapse to "absent" rather than poison the origin.
    """
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        return None
    return numeric


def event_backdate_seconds(event: EventSnapshot, *, onset_key: str) -> float | None:
    """Return the MCP-domain backdating delta for a backdated milestone event.

    coffee-roaster-mcp v0.1.7 (#169/#170) backdates the ``beans_added`` /
    ``first_crack_detected`` event ``monotonic_seconds`` to the turning-point /
    crack-onset instant and preserves the confirmation tick in the payload. This
    returns ``confirmed_at_monotonic_seconds − onset`` — a duration computed
    **entirely within the MCP monotonic domain**, so it is safe to subtract from
    the agent's own ``time.monotonic`` receive-tick (the agent receives the event
    at ≈ the confirmation moment). It is *never* an absolute MCP timestamp:
    cross-process monotonic clocks are not comparable, so this only ever returns a
    domain-free delta.

    The onset is read from ``payload[onset_key]`` when present (the in-domain pair
    keeps the subtraction self-consistent) and falls back to the event's own
    backdated ``monotonic_seconds`` (which the server sets equal to that field).

    Args:
        event: The backdated milestone event snapshot.
        onset_key: The payload key holding the backdated onset in the MCP domain
            (``"turning_point_monotonic_seconds"`` for T0,
            ``"detected_at_monotonic_seconds"`` for FC).

    Returns:
        The non-negative backdate delta in seconds, or ``None`` when the v0.1.7
        ``confirmed_at_monotonic_seconds`` field is absent (a manual mark or a
        pre-0.1.7 payload), the onset is unreadable, or the delta is negative /
        non-finite (a malformed payload). ``None`` means "stamp at receive-tick",
        the conservative pre-backdating behaviour.
    """
    confirmed = _payload_float(event.payload, "confirmed_at_monotonic_seconds")
    if confirmed is None:
        return None
    onset = _payload_float(event.payload, onset_key)
    if onset is None:
        onset = event.monotonic_seconds
    delta = confirmed - onset
    if not math.isfinite(delta) or delta < 0.0:
        return None
    return delta


def _latest_backdate_seconds(
    events: Sequence[EventSnapshot], *, kind: str, onset_key: str
) -> float | None:
    """Return the backdate delta of the most recent event of ``kind``.

    Both milestones are singletons in a session, but iterating in reverse keeps
    the helper correct (and cheap) regardless of how many events precede them.
    """
    for event in reversed(events):
        if event.kind == kind:
            return event_backdate_seconds(event, onset_key=onset_key)
    return None


def project_session_state(state: RoastSessionState, *, age_seconds: float) -> RoastTelemetry | None:
    """Project an MCP ``RoastSessionState`` into the controller's ``RoastTelemetry``.

    Returns ``None`` when no usable reading exists — no device state, or bean/
    environment temperature absent (the mock driver reports ``connected`` with
    null temperatures during pre-roast). ``RoastTelemetry`` requires both
    temperatures, so a partial reading is "no telemetry", not a zero reading.

    Detection booleans come from the contract-checked Literal status fields
    (``t0_status.status`` / ``first_crack_status.status``), not the latched
    ``*_at_utc`` timestamps. The backdating deltas (#337) come from the matching
    ``beans_added`` / ``first_crack_detected`` event payloads (v0.1.7), surfaced
    as in-domain durations the controller subtracts from its receive-tick clock.
    Derived metrics (RoR) are passed through from MCP, never recomputed (plan §2).
    ``age_seconds`` is supplied by the caller — the session state carries no
    per-reading wall-clock age."""
    device = state.device_state
    if device is None or device.bean_temp_c is None or device.env_temp_c is None:
        return None
    return RoastTelemetry(
        bean_temp_c=device.bean_temp_c,
        env_temp_c=device.env_temp_c,
        age_seconds=age_seconds,
        bean_ror_c_per_min=state.bean_ror_c_per_min,
        env_ror_c_per_min=state.env_ror_c_per_min,
        t0_detected=state.t0_status.status == "detected",
        first_crack_detected=state.first_crack_status.status == "detected",
        cooling_on=state.cooling_on,
        mic_status=project_mic_status(state.first_crack_status),
        t0_backdate_seconds=_latest_backdate_seconds(
            state.events,
            kind="beans_added",
            onset_key="turning_point_monotonic_seconds",
        ),
        first_crack_backdate_seconds=_latest_backdate_seconds(
            state.events,
            kind="first_crack_detected",
            onset_key="detected_at_monotonic_seconds",
        ),
    )


class RoasterControlAdapter:
    """Adapts :class:`RoasterMCPClient` to the controller's ``StateReader`` and
    ``CommandExecutor`` protocols (E9 wiring seam).

    Three concerns the raw client does not cover for the tick loop:

    - ``set_targets`` fans a single heat/fan target out to ``set_heat`` then
      ``set_fan``. Heat is the safety-critical lever, applied first: a fail-safe
      heat-off lands even if the paired fan write then fails (the failure raises
      and the controller records ``COMMAND_FAILED``). The real-Hottop ordering
      is confirmed under E12 hardware validation.
    - ``read_telemetry`` projects ``get_roast_state`` and derives the reading's
      **age** from a stalled session clock: ``age_seconds`` grows only while
      ``elapsed_monotonic_seconds`` stops advancing across reads, so the
      stale-telemetry safety fault (``evaluate_telemetry_validity``) can actually
      fire against a wedged-but-alive server — a hardcoded ``0.0`` would make it
      structurally unreachable.
    - It retains the last raw ``RoastSessionState`` (``last_state``) so the runner
      can persist ``raw_state_json`` / ``development_percent`` / ``mcp_phase``,
      which live on the session state, not on the projected ``RoastTelemetry``.

    Transport failures (``MCPConnectionError`` / ``MCPToolTimeoutError``) are
    **not** caught here — they propagate so the controller's consecutive-failure
    rules see them as read/command faults (never a silent reconnect-and-continue)."""

    def __init__(
        self,
        client: RoasterMCPClient,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._clock = clock
        self._last_state: RoastSessionState | None = None
        self._last_elapsed: float | None = None
        self._last_change_monotonic: float | None = None

    @property
    def last_state(self) -> RoastSessionState | None:
        """The most recent raw ``RoastSessionState`` read (``None`` before the
        first read). Source of the persisted ``raw_state_json`` / dev% / MCP
        phase fields the projected telemetry drops."""
        return self._last_state

    async def read_telemetry(self) -> RoastTelemetry | None:
        state = await self._client.get_roast_state()
        now = self._clock()
        if self._last_elapsed is None or state.elapsed_monotonic_seconds != self._last_elapsed:
            self._last_elapsed = state.elapsed_monotonic_seconds
            self._last_change_monotonic = now
        age = 0.0 if self._last_change_monotonic is None else now - self._last_change_monotonic
        self._last_state = state
        return project_session_state(state, age_seconds=age)

    async def start_session(self) -> None:
        # A new session must not inherit the previous roast's cached read. Until
        # the first read_telemetry tick lands, last_state (and the age tracking)
        # would otherwise expose the prior session's state — e.g. a stale mic
        # icon on a back-to-back roast (#200/Codex), or a stale raw_state_json /
        # dev% / mcp_phase persisted for the new run's first tick. Reset on start.
        self._last_state = None
        self._last_elapsed = None
        self._last_change_monotonic = None
        await self._client.start_roast_session()

    async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
        await self._client.set_heat(heat_percent)
        await self._client.set_fan(fan_percent)

    async def mark_beans_added(self) -> None:
        await self._client.mark_beans_added()

    async def mark_first_crack(self) -> None:
        await self._client.mark_first_crack()

    async def drop_beans(self) -> None:
        await self._client.drop_beans()

    async def start_cooling(self) -> None:
        await self._client.start_cooling()

    async def stop_cooling(self) -> None:
        await self._client.stop_cooling()

    async def emergency_stop(self, *, reason: str) -> None:
        await self._client.emergency_stop(reason)

    async def export_roast_log(self) -> ExportRoastLogResult:
        """Export the roast log (the runner's completion step — not a control
        write, so it is outside the ``CommandExecutor`` protocol)."""
        return await self._client.export_roast_log()


# --- E5-S2: stdio child-process transport (D6) ---


class MCPConnectionError(Exception):
    """Typed failure for any MCP transport problem (dead child, broken
    pipe, protocol error). The controller's consecutive-failure rules map
    it to fail-closed — never silent reconnect-and-continue."""


class MCPToolTimeoutError(MCPConnectionError):
    """An MCP call exceeded ``call_timeout_seconds`` — the child is wedged.

    Raising (instead of stalling the tick) is the E4-S2 safety-reviewer
    carry-forward: a hung ``get_roast_state`` — or worse, a hung
    ``emergency_stop`` — must surface as a failure the safety rules see.
    """


class MCPToolError(MCPConnectionError):
    """The server answered with ``isError`` — the call failed server-side."""


class ToolSession(Protocol):
    """The slice of mcp.ClientSession the transport needs (test seam)."""

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> object:
        """Execute one tool call and return the raw result object."""
        ...


class InitializableSession(ToolSession, Protocol):
    """A ToolSession that also supports the MCP initialize handshake."""

    async def initialize(self) -> object:
        """Run the MCP initialization handshake."""
        ...


SessionFactory = Callable[
    [StdioServerParameters], AbstractAsyncContextManager[InitializableSession]
]


def resolve_mcp_command(command: str) -> str:
    """Resolve the MCP child command, pinning the default to the agent's own env.

    The advisor never controls hardware, but the *binary the controller spawns*
    is itself a safety surface: it carries the pinned ``coffee-roaster-mcp``
    (and its pinned pydantic / pydantic_core / mcp stack) that the safety and
    telemetry paths are validated against. In production a bare-PATH spawn of
    ``coffee-roaster-mcp`` (the agent was started outside its activated venv)
    resolved to ``/opt/homebrew/bin/coffee-roaster-mcp`` — a *foreign* homebrew
    Python carrying a STALE, mismatched dependency set (pydantic 2.12.3 /
    pydantic_core 2.41.4, mcp 1.19.0 instead of the agent's 2.13.4 / 2.46.4),
    and that stale native stack is exactly where the end-of-roast audio-teardown
    segfault hit. So when the command is left at the default, we never trust a
    bare-PATH lookup to find the right one: we resolve it to the console script
    installed alongside the running interpreter (``sys.executable``), which is
    guaranteed to be the in-venv, pinned install.

    Resolution order for the default command:

    1. The console script next to ``sys.executable`` (``<py-dir>/<name>`` plus
       the platform's executable suffix on Windows), if it exists — the pinned
       in-venv install.
    2. A ``PATH`` lookup via :func:`shutil.which` — a best-effort fallback when
       the script is not co-located with the interpreter (e.g. a pipx/uv shim
       layout) but is still reachable.
    3. The bare name unchanged — let the transport's own spawn report failure.

    An explicit, non-default ``command`` (an operator/config override) is
    ALWAYS returned verbatim: the operator has deliberately chosen a binary and
    that choice must win, including an intentional homebrew or system path.

    Args:
        command: The configured ``MCPConfig.command``.

    Returns:
        The command to spawn — an absolute in-venv path when the default is
        resolvable, otherwise an unchanged string.
    """
    if command != DEFAULT_MCP_COMMAND:
        # Explicit operator/config override — never second-guess it.
        return command
    interpreter_dir = Path(sys.executable).parent
    suffix = Path(sys.executable).suffix  # ".exe" on Windows, "" elsewhere
    candidate = interpreter_dir / f"{DEFAULT_MCP_COMMAND}{suffix}"
    if candidate.exists():
        return str(candidate)
    on_path = shutil.which(DEFAULT_MCP_COMMAND)
    if on_path is not None:
        return on_path
    return DEFAULT_MCP_COMMAND


@asynccontextmanager
async def _spawn_stdio_session(
    params: StdioServerParameters,
) -> AsyncGenerator[ClientSession]:
    """Default factory: spawn the child and open a ClientSession over it.

    Excluded from unit coverage: this is the thin real-IO shim that only
    runs with the actual coffee-roaster-mcp binary — exercised by
    test_real_child_process_round_trip, which auto-activates at E9.
    """
    async with (  # pragma: no cover
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        yield session


def parse_tool_result(result: object) -> object:
    """Extract the payload from a CallToolResult-shaped object.

    FastMCP serializes dataclass results into ``structuredContent``
    directly; scalar results arrive wrapped as ``{"result": value}``.
    Falls back to the first text content block parsed as JSON.
    """
    if getattr(result, "isError", False):
        raise MCPToolError(f"tool call failed server-side: {_result_text(result)}")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        structured_typed = cast("dict[str, object]", structured)
        if set(structured_typed.keys()) == {"result"}:
            # FastMCP wraps non-dict (scalar) tool results as {"result": x}.
            # Invariant this relies on: every real result dataclass has more
            # than one field, so a single-key "result" dict can only be the
            # scalar wrapper. The mcp-contract-checker guards the invariant
            # (a future one-field dataclass named `result` would break it).
            return structured_typed["result"]
        return structured_typed
    text = _result_text(result)
    if text is None:
        raise MCPConnectionError("tool result carried no structured or text content")
    return json.loads(text)


def _result_text(result: object) -> str | None:
    content = getattr(result, "content", None)
    if isinstance(content, Sequence):
        blocks = cast("Sequence[object]", content)
        for block in blocks:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                return text
    return None


class MCPServerProcess:
    """Owns the coffee-roaster-mcp stdio child process (D6, E5-S2).

    One systemd unit: the agent spawns ``coffee-roaster-mcp serve``,
    health-checks it (initialize + get_server_info), bounds every call
    with ``call_timeout_seconds``, and shuts it down cleanly. An agent
    restart therefore means a clean MCP restart into the recovery flow.
    Implements the :data:`ToolCaller` contract for RoasterMCPClient.
    """

    def __init__(
        self,
        config: MCPConfig | None = None,
        *,
        session: ToolSession | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._config = config or MCPConfig()
        self._session: ToolSession | None = session  # injectable test seam
        self._session_factory: SessionFactory = session_factory or _spawn_stdio_session
        self._stack: AsyncExitStack | None = None

    def build_server_parameters(self) -> StdioServerParameters:
        """The spawn argv: ``<command> serve`` (server.json packageArguments).

        The default command is resolved to the in-venv console script before
        spawning (see :func:`resolve_mcp_command`) so a bare-PATH lookup can
        never silently shadow the pinned install with a foreign one — the
        homebrew-stale-deps segfault trap. An explicit operator override is
        passed through verbatim.

        Config ``env`` overrides are merged over the agent's own environment so
        the child keeps ``PATH``/``HOME`` while gaining the requested selectors
        (E9-S2 sets the mock-driver vars). With no overrides, ``env`` stays
        ``None`` and the transport supplies its default safe environment."""
        env = {**os.environ, **self._config.env} if self._config.env else None
        command = resolve_mcp_command(self._config.command)
        return StdioServerParameters(command=command, args=["serve"], env=env)

    @property
    def running(self) -> bool:
        """Whether a session is attached (spawned or injected)."""
        return self._session is not None

    async def start(self) -> None:
        """Spawn the child, initialize the MCP session, health-check it."""
        if self._session is not None:
            return
        stack = AsyncExitStack()
        try:
            session = await stack.enter_async_context(
                self._session_factory(self.build_server_parameters())
            )
            await asyncio.wait_for(
                session.initialize(), timeout=self._config.startup_timeout_seconds
            )
            self._stack = stack
            self._session = session
            # Health check through the public surface.
            await self.call_tool("get_server_info", {})
        except MCPConnectionError:
            await stack.aclose()
            self._stack = None
            self._session = None
            raise
        except Exception as exc:
            await stack.aclose()
            self._stack = None
            self._session = None
            raise MCPConnectionError(f"failed to start coffee-roaster-mcp: {exc}") from exc

    async def stop(self) -> None:
        """Shut the child down cleanly."""
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        """ToolCaller implementation: timeout-bounded, typed failures only."""
        if self._session is None:
            raise MCPConnectionError("MCP server process is not running")
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(name, dict(arguments)),
                timeout=self._config.call_timeout_seconds,
            )
            # Parsed inside the try: a malformed text block (JSONDecodeError)
            # must surface as a typed failure too (review finding, E5-S2 PR).
            return parse_tool_result(result)
        except TimeoutError as exc:
            raise MCPToolTimeoutError(
                f"MCP call '{name}' exceeded {self._config.call_timeout_seconds:.1f}s "
                f"— the child is wedged"
            ) from exc
        except MCPConnectionError:
            raise
        except Exception as exc:
            raise MCPConnectionError(f"MCP call '{name}' failed: {exc}") from exc
