"""Typed wrapper over the coffee-roaster-mcp stdio server (component plan §2, §4).

E5-S1: Pydantic mirrors of every tool result shape, derived from the
actual coffee-roaster-mcp source (`mcp_server.py` dataclasses, v0.1.3
surface verified in plan §2) and validated against the 7 Jun 2026
live-roast exports. The mirrors use ``extra="ignore"`` so new optional
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

from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict

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
        return RoastSessionState.model_validate(
            await self._call("get_roast_state", {"session_id": session_id})
        )

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
        return ExportRoastLogResult.model_validate(
            await self._call("export_roast_log", {"session_id": session_id})
        )

    async def emergency_stop(self, reason: str = "manual emergency stop") -> EventCommandResult:
        return EventCommandResult.model_validate(
            await self._call("emergency_stop", {"reason": reason})
        )
