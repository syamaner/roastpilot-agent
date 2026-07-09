"""Typed wrapper over the coffee-roaster-mcp stdio server (component plan §2, §4).

E5-S1: Pydantic mirrors of every tool result shape, derived from the
actual coffee-roaster-mcp source (`mcp_server.py` dataclasses, v0.1.3
surface verified in plan §2 — the 13-tool surface held unchanged through
v0.1.5 (0.1.4 added the `mic-check` CLI; 0.1.5 made a transient mic
overflow recoverable, neither touching the tool surface), and v0.1.9 adds
the 14th tool `set_recording_metadata` (#176 — sets the export filename's
origin slug + roast number; the 13 pre-existing tools have zero drift,
mcp-contract-checker confirmed) and validated against the
7 Jun 2026 live-roast exports. The mirrors use ``extra="ignore"`` so new optional
upstream fields never break the agent — drift is detected by the
mcp-contract-checker sub-agent and the contract fixtures (E5-S3), not by
runtime crashes. v0.1.12 adds ``ambient_status`` to ``get_roast_state``'s
session state (#342, D85 — read-only corpus metadata: temperature/humidity/
pressure from an optional Yoctopuce probe; no tool-surface change, hardware-
validated on a live read). #464 (D86, revises D85) additionally projects the
LATEST ambient triad onto ``RoastTelemetry`` every tick (mirroring
``mic_status``) so the live SSE dashboard can show it updating — still no
control/safety coupling, and the one-time charge-instant capture (#342) is
untouched.

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
import logging
import math
import os
import shutil
import signal
import sys
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, ConfigDict

from roastpilot_agent.config import DEFAULT_MCP_COMMAND, MCPConfig, MCPDeviceConfig
from roastpilot_agent.models import MicStatus, RoastTelemetry

_log = logging.getLogger(__name__)

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

#: #342 (D85): ambient is MCP-owned corpus metadata — the agent only mirrors and
#: stores the reading, never gates on it.
AmbientMode = Literal["disabled", "yoctopuce"]
AmbientRuntimeStatus = Literal["disabled", "unavailable", "ok"]


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


class AmbientStatus(MCPMirror):
    """Mirror of mcp_server.AmbientStatus (#342, D85 — 0.1.12).

    Ambient (temperature/humidity/pressure) is MCP-owned corpus metadata: a
    read-only projection the agent mirrors byte-for-byte and, once per run at
    charge, persists onto ``roast_runs`` (:meth:`RoastStore.set_ambient`). It
    carries no safety or control significance — no safety gate, transition, or
    advisor context reads it — so a probe fault degrades to ``"unavailable"``
    and the agent fails soft (nulls persisted, roast unaffected), never a fault
    or a recovery.

    Hardware-validated on a live Yoctopuce Yocto-Meteo-V2-C read (28.49 °C /
    38.6 % / 1008.56 hPa)."""

    mode: AmbientMode
    status: AmbientRuntimeStatus
    reason: str | None = None
    ambient_running: bool = False
    temperature_c: float | None = None
    humidity_percent: float | None = None
    pressure_hpa: float | None = None
    last_reading_monotonic_seconds: float | None = None


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
    ambient_status: AmbientStatus
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


class SetRecordingMetadataResult(MCPMirror):
    """Mirror of mcp_server.SetRecordingMetadataResult (#176)."""

    origin: str
    roast_num: int


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
    """Typed client for exactly the verified 14-tool MCP surface.

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

    async def set_recording_metadata(
        self, origin: str, roast_num: int
    ) -> SetRecordingMetadataResult:
        """Set the export filename's origin slug + roast number (v0.1.9, #176).

        Must be called BEFORE ``start_roast_session``: the MCP applies the
        metadata to the recording filename when the session opens, so calling it
        after the session has started silently falls back to session-id / roast 0
        naming (verified on hardware). The server re-slugifies ``origin``.

        Args:
            origin: A bean/origin slug (e.g. ``"colombia-huila"``); re-slugified
                by the server.
            roast_num: The per-origin roast counter used in the filename.

        Returns:
            The origin + roast number the server recorded.
        """
        return SetRecordingMetadataResult.model_validate(
            await self._call("set_recording_metadata", {"origin": origin, "roast_num": roast_num})
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


def project_live_ambient(status: AmbientStatus) -> tuple[float | None, float | None, float | None]:
    """Project an MCP ``AmbientStatus`` into the live ambient triad (#464, D86).

    A pure, read-only observability projection — no safety logic, no MCP
    write — mirroring :func:`project_mic_status`'s precedent exactly: it
    forwards the MCP's own already-computed ``status`` gate rather than
    re-deriving anything. This is the LATEST/live reading, refreshed every
    tick — distinct from the one-time charge-instant capture
    :meth:`RoastStore.set_ambient` persists (#342, D85), which this function
    does not touch.

    Args:
        status: The MCP ambient status from ``RoastSessionState``.

    Returns:
        The ``(temperature_c, humidity_percent, pressure_hpa)`` triad when
        ``status.status == "ok"``, else ``(None, None, None)`` — the MCP's own
        fail-soft contract for a disabled/unavailable probe.
    """
    if status.status != "ok":
        return None, None, None
    return status.temperature_c, status.humidity_percent, status.pressure_hpa


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
    ambient_temp_c, ambient_humidity_pct, ambient_pressure_hpa = project_live_ambient(
        state.ambient_status
    )
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
        ambient_temp_c=ambient_temp_c,
        ambient_humidity_pct=ambient_humidity_pct,
        ambient_pressure_hpa=ambient_pressure_hpa,
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

    async def start_session(
        self, *, recording_origin: str | None = None, recording_roast_num: int | None = None
    ) -> None:
        """Start a new MCP roast session, optionally naming the recording.

        When ``recording_origin`` and ``recording_roast_num`` are both supplied,
        ``set_recording_metadata`` is called FIRST (v0.1.9, #176): the MCP only
        applies the origin slug + roast number to the export filename if the
        metadata is set before the session opens — set afterwards it silently
        falls back to session-id / roast 0 naming (verified on hardware). The
        metadata call is best-effort: a failure is logged and the roast proceeds
        (recording naming must never block a roast). When either argument is
        ``None`` the call is skipped and the MCP falls back safely.

        Args:
            recording_origin: A bean/origin slug for the export filename, or
                ``None`` to let the MCP fall back to its default naming.
            recording_roast_num: The per-origin roast counter, or ``None``.
        """
        # A new session must not inherit the previous roast's cached read. Until
        # the first read_telemetry tick lands, last_state (and the age tracking)
        # would otherwise expose the prior session's state — e.g. a stale mic
        # icon on a back-to-back roast (#200/Codex), or a stale raw_state_json /
        # dev% / mcp_phase persisted for the new run's first tick. Reset on start.
        self._last_state = None
        self._last_elapsed = None
        self._last_change_monotonic = None
        if recording_origin is not None and recording_roast_num is not None:
            try:
                await self._client.set_recording_metadata(recording_origin, recording_roast_num)
            except Exception:
                # Recording naming is best-effort: never block the roast on it.
                # The MCP falls back to session-id / roast 0 naming.
                _log.warning(
                    "set_recording_metadata failed (origin=%r, roast_num=%r); "
                    "MCP will fall back to default recording naming",
                    recording_origin,
                    recording_roast_num,
                    exc_info=True,
                )
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

#: A best-effort, synchronous force-terminate of the spawned child process
#: tree, invoked only when graceful ``stop`` overruns ``stop_timeout_seconds``.
#: Returns ``True`` if a termination signal was actually delivered (a child was
#: known and alive), ``False`` otherwise. Injectable so the timeout path is
#: unit-testable without a real process.
ForceTerminate = Callable[[], bool]


def force_terminate_process_group(pid: int) -> bool:
    """Force-kill the child process *group* by pid (POSIX only).

    The transport spawns the child with ``start_new_session=True`` (the MCP
    SDK's ``_create_platform_compatible_process``), so the child is its own
    session/process-group leader and ``pgid == pid``. Sending ``SIGKILL`` to
    the group atomically reaps the child and anything it forked (the audio
    worker), which is exactly what a wedged-child shutdown needs.

    This is the uncatchable last resort: it runs only after graceful
    ``aclose`` has already overrun ``stop_timeout_seconds``, so SIGKILL (not a
    catchable SIGTERM the wedged child may never service) is deliberate.

    POSIX-only: ``os.killpg``/``os.getpgid`` do not exist on Windows. This
    repo targets darwin/linux; on any other platform the helper is a no-op.

    Args:
        pid: The spawned child's OS process id (also its process-group id).

    Returns:
        ``True`` if ``SIGKILL`` was delivered to the group, ``False`` if the
        platform is unsupported or the process group was already gone.
    """
    if not hasattr(os, "killpg"):  # pragma: no cover - non-POSIX (Windows)
        _log.error("cannot force-terminate MCP child %d: killpg unavailable", pid)
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        # Already exited between the timeout and the kill — nothing to do.
        return False
    except OSError as exc:  # pragma: no cover - defensive: permission/race
        _log.error("force-terminate of MCP child %d failed: %s", pid, exc)
        return False
    return True


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
    *,
    on_spawn: Callable[[int], None] | None = None,
) -> AsyncGenerator[ClientSession]:
    """Default factory: spawn the child and open a ClientSession over it.

    The MCP SDK's ``stdio_client`` owns the spawn and does not expose the
    child process, but the agent must be able to force-terminate a wedged
    child whose graceful ``aclose`` has overrun (#212). Rather than
    reimplement the transport, this shim briefly wraps the SDK's own
    ``_create_platform_compatible_process`` so it can read the spawned
    child's OS pid and hand it to ``on_spawn``; the wrap is removed before
    the session is yielded. The child is spawned with
    ``start_new_session=True`` (SDK), so that pid is also the process-group
    id used by :func:`force_terminate_process_group`.

    Args:
        params: The stdio spawn parameters (command, args, env).
        on_spawn: Optional callback invoked once with the child pid as soon
            as the process is created — the seam that lets the owning
            :class:`MCPServerProcess` register a force-terminate hook.

    Excluded from unit coverage: this is the thin real-IO shim that only
    runs with the actual coffee-roaster-mcp binary — exercised by
    test_real_child_process_round_trip, which auto-activates at E9.
    """
    import mcp.client.stdio as _stdio  # pragma: no cover - real-IO shim

    original = _stdio._create_platform_compatible_process  # pyright: ignore[reportPrivateUsage]
    # Forward through a permissive signature: this thin wrapper only reads the
    # spawned process's pid and re-emits it, never inspecting the SDK's args.
    spawn = cast("Callable[..., Awaitable[object]]", original)

    async def _capturing_create(*args: object, **kwargs: object) -> object:  # pragma: no cover
        process = await spawn(*args, **kwargs)
        pid = getattr(process, "pid", None)
        if on_spawn is not None and isinstance(pid, int):
            on_spawn(pid)
        return process

    # This save/restore is a PROCESS-WIDE monkeypatch of a module global; it is
    # safe only because the agent starts exactly one MCP child at a time (no
    # concurrent ``start``), so no other spawn can race the patched window.
    _stdio._create_platform_compatible_process = _capturing_create  # type: ignore[assignment]  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]
    try:  # pragma: no cover
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            yield session
    finally:  # pragma: no cover
        # Restore as soon as the spawn-and-yield scope exits (or raises): the
        # capturing wrap is only needed for the single spawn inside this block.
        _stdio._create_platform_compatible_process = original  # pyright: ignore[reportPrivateUsage]


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
        device_config: MCPDeviceConfig | None = None,
        session: ToolSession | None = None,
        session_factory: SessionFactory | None = None,
        force_terminate: ForceTerminate | None = None,
    ) -> None:
        """Initialize the transport.

        Args:
            config: MCP child settings (command, timeouts). Defaults applied.
            device_config: Managed device/config fields rendered into the MCP
                yaml on each (re)spawn via passthrough-merge (D78-4, #420).
                When ``None`` the yaml render step is skipped and the MCP
                child reads its yaml directly (the pre-S3 behaviour; preserves
                the E9-S2 ``MCPConfig.env`` path used by tests).
            session: A pre-attached ``ToolSession`` (test seam); skips spawn.
            session_factory: Override the real ``stdio_client`` spawn (test
                seam). When omitted, the default factory both spawns the child
                and registers a process-group force-terminate hook used if
                graceful :meth:`stop` overruns ``stop_timeout_seconds``.
            force_terminate: An injectable force-terminate hook (test seam).
                When provided, it is used directly on a stop timeout instead
                of the spawned-pid hook — letting the timeout path be unit
                tested without a real process.
        """
        self._config = config or MCPConfig()
        self._device_config: MCPDeviceConfig | None = device_config
        self._session: ToolSession | None = session  # injectable test seam
        self._session_factory: SessionFactory = (
            session_factory if session_factory is not None else self._default_session_factory
        )
        self._stack: AsyncExitStack | None = None
        #: Set on the timeout path of :meth:`stop` when the child did not
        #: confirm a clean shutdown and had to be force-terminated. #177 will
        #: persist it so a restart enters ``operator_recovery_required``.
        self._stop_unconfirmed = False
        #: Best-effort force-terminate of the spawned child group, populated by
        #: the default factory once the pid is known (or injected for tests).
        self._force_terminate: ForceTerminate | None = force_terminate
        #: True when ``force_terminate`` was supplied at construction (test seam).
        #: An injected hook must win over the auto-registered hook and must NOT
        #: be cleared between respawns — only the auto-registered (real-IO) hook
        #: is replaced per spawn.  Without this flag, the re-arm logic in
        #: :meth:`start` cannot distinguish "no hook yet" from "hook from prev
        #: pid" and would leave the seam intact on the wrong branch.
        self._force_terminate_injected: bool = force_terminate is not None
        #: Rendered yaml temp dir; created in :meth:`build_server_parameters`
        #: when ``_device_config`` is set, cleaned up in :meth:`stop`.
        self._rendered_yaml_dir: Path | None = None

    def _default_session_factory(
        self, params: StdioServerParameters
    ) -> AbstractAsyncContextManager[InitializableSession]:
        """The real spawn factory: spawn the child and capture its pid.

        Wires :func:`_spawn_stdio_session`'s ``on_spawn`` to
        :meth:`_register_force_terminate` so a wedged-child timeout in
        :meth:`stop` can force-kill the process group. An explicitly injected
        ``force_terminate`` (test seam) is left untouched — only an unset hook
        is populated by the spawn.

        Args:
            params: The stdio spawn parameters.

        Returns:
            An async context manager yielding the initialized session.
        """
        return _spawn_stdio_session(params, on_spawn=self._register_force_terminate)

    def _register_force_terminate(self, pid: int) -> None:
        """Record a process-group force-terminate hook for the spawned ``pid``.

        Skips registration if a force-terminate hook was injected at
        construction (test seam): the injected hook must win.

        Args:
            pid: The spawned child's OS pid (also its process-group id).
        """
        if self._force_terminate is None:  # pragma: no cover - real-IO path
            self._force_terminate = lambda: force_terminate_process_group(pid)

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
        ``None`` and the transport supplies its default safe environment.

        **MCP yaml render (D78-4, #420)** — when a
        :class:`~roastpilot_agent.config.MCPDeviceConfig` was supplied at
        construction, the managed device fields are rendered into a temp yaml
        via passthrough-merge and ``COFFEE_ROASTER_MCP_CONFIG`` is set to that
        file's path in the child's environment.  The temp dir is cleaned up in
        :meth:`stop`.  On each (re)spawn a fresh render is produced so config
        changes between sessions take effect.

        **Source-yaml resolution** (render source = operator's existing yaml):

        1. ``device_config.mcp_yaml_source_path`` — explicit path wins; passed
           to :func:`~roastpilot_agent.mcp_yaml.render_mcp_yaml` as-is (raises
           :class:`FileNotFoundError` if the file is absent — fail closed).
        2. ``COFFEE_ROASTER_MCP_CONFIG`` from ``MCPConfig.env`` — the value
           forwarded by ``forward_coffee_env`` from ``roast-live.sh``.
        3. ``COFFEE_ROASTER_MCP_CONFIG`` from ``os.environ`` — the ambient
           operator environment.  Steps 2–3 also raise if the resolved path
           is missing (same fail-closed guarantee).
        4. ``coffee-roaster-mcp.yaml`` in the current working directory — the
           MCP's own default fallback.  Only used if the file actually exists;
           silently skipped otherwise so a fresh install is not treated as a
           config error.
        5. ``None`` — no existing yaml; render from managed fields only
           (fresh install with no hand-authored config).

        **Skip condition**: if the overlay is entirely empty (all
        ``MCPDeviceConfig`` fields are ``None``) AND no source yaml is
        resolvable at steps 1–5, the render step is skipped and
        ``COFFEE_ROASTER_MCP_CONFIG`` is left exactly as the operator set it —
        so a default all-``None`` ``mcp_device`` on a ``roast-live.sh`` roast
        does not overwrite the operator's proven Hottop yaml with an empty one.
        """
        import tempfile  # noqa: PLC0415

        from roastpilot_agent.mcp_yaml import (  # noqa: PLC0415
            _device_config_to_overlay,  # pyright: ignore[reportPrivateUsage]
            render_mcp_yaml,
            resolve_mcp_yaml_source_path,
        )

        # Start from the MCPConfig.env overrides (E9-S2 mock-driver path).
        extra_env: dict[str, str] = dict(self._config.env)

        if self._device_config is not None:
            # Resolve the source yaml in priority order (#482: extracted into
            # resolve_mcp_yaml_source_path so GET /api/config's read-only yaml
            # lookup resolves the identical source file — one precedence
            # implementation, not two that could drift):
            #   1. explicit mcp_yaml_source_path
            #   2. COFFEE_ROASTER_MCP_CONFIG from MCPConfig.env (forward_coffee_env)
            #   3. COFFEE_ROASTER_MCP_CONFIG from os.environ
            #   4. ./coffee-roaster-mcp.yaml (the MCP's own CWD default — only if it exists)
            # render_mcp_yaml raises FileNotFoundError when a resolved (non-None)
            # source is missing (fail closed) — resolve_mcp_yaml_source_path itself
            # does not raise; it only resolves the candidate path.
            source = resolve_mcp_yaml_source_path(self._device_config, extra_env)

            # Build the overlay to decide whether we need to render at all.
            overlay = _device_config_to_overlay(self._device_config)

            if not overlay and source is None:
                # Nothing to overlay and no existing yaml to copy — skip the
                # render entirely so COFFEE_ROASTER_MCP_CONFIG is untouched.
                _log.debug("mcp_device: no managed fields and no source yaml — skipping render")
            else:
                # Clean up any leftover dir from a previous spawn.
                if self._rendered_yaml_dir is not None:
                    shutil.rmtree(self._rendered_yaml_dir, ignore_errors=True)
                    self._rendered_yaml_dir = None

                tmp_dir = Path(tempfile.mkdtemp(prefix="rp-mcp-yaml-"))
                self._rendered_yaml_dir = tmp_dir
                dest = tmp_dir / "coffee-roaster-mcp.yaml"
                try:
                    render_mcp_yaml(
                        self._device_config,
                        source_path=source,
                        dest_path=dest,
                    )
                except Exception:
                    # Render failed — clean up so we don't leak the temp dir.
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    self._rendered_yaml_dir = None
                    raise
                extra_env["COFFEE_ROASTER_MCP_CONFIG"] = str(dest)
                _log.debug("rendered MCP yaml → %s (source=%s)", dest, source)

        env: dict[str, str] | None = {**os.environ, **extra_env} if extra_env else None
        command = resolve_mcp_command(self._config.command)
        return StdioServerParameters(command=command, args=["serve"], env=env)

    @property
    def device_config(self) -> MCPDeviceConfig | None:
        """The device config currently set for the next (re)spawn, or ``None``.

        Read-only accessor so callers can detect whether the config that was
        used at the most recent :meth:`start` differs from a freshly-loaded
        config — the comparison that drives the between-roast respawn (#431).
        """
        return self._device_config

    def set_device_config(self, device_config: MCPDeviceConfig) -> None:
        """Update the device config rendered into the MCP yaml on the next spawn.

        Safe to call while the child is stopped (between roasts).  The updated
        config is rendered into the MCP yaml by :meth:`build_server_parameters`
        on the *next* :meth:`start` call — calling this while the child is
        running has no effect on the live session (the yaml was already
        rendered; the child reads it only on spawn).

        Args:
            device_config: The new managed device fields to render on the next
                (re)spawn via passthrough-merge (D78-4, #420).
        """
        self._device_config = device_config

    @property
    def running(self) -> bool:
        """Whether a session is attached (spawned or injected)."""
        return self._session is not None

    @property
    def stop_unconfirmed(self) -> bool:
        """Whether the *most recent* :meth:`stop` had to force-terminate the child.

        ``True`` means the last graceful teardown overran ``stop_timeout_seconds``
        and the child process group was force-killed, so a clean shutdown was
        never confirmed. It is reset to ``False`` at the top of :meth:`start`,
        so across a ``start → stop → start`` cycle a fresh run never inherits a
        previous run's unconfirmed flag — the flag describes the last completed
        teardown, not the lifetime. A clean stop leaves it ``False``. #177
        persists this to the decision trace so an unconfirmed stop is visible
        post-roast (observability for diagnosis / recovery — never an
        auto-resume trigger).
        """
        return self._stop_unconfirmed

    async def start(self) -> None:
        """Spawn the child, initialize the MCP session, health-check it.

        Resets :attr:`stop_unconfirmed` to ``False`` first: the flag describes
        the most recent teardown, so a reused process (start → stop → start)
        must not carry a prior run's unconfirmed verdict into the new run.

        Re-arms the force-terminate hook for each spawn: on the first start the
        auto-registered hook captures the spawned pid via
        :meth:`_register_force_terminate`; on a respawn the previous pid's
        closure would still be held, so the hook is cleared here (before the
        spawn) so :meth:`_register_force_terminate` re-registers with the new
        pid.  An injected hook (test seam, ``_force_terminate_injected=True``)
        is never cleared — it must win and be reused across respawns.
        """
        if self._session is not None:
            return
        self._stop_unconfirmed = False
        # Re-arm: clear the auto-registered hook before each spawn so
        # _register_force_terminate captures the new pid, not the previous one.
        # Injected hooks (test seam) are left untouched.
        if not self._force_terminate_injected:
            self._force_terminate = None
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
        """Shut the child down, bounded by ``stop_timeout_seconds`` (#212).

        Graceful teardown (``AsyncExitStack.aclose`` → the SDK's
        stdin-close → SIGTERM → SIGKILL sequence) can stall forever on a
        wedged native child (blocked PortAudio read) or a task group still
        awaiting an open pipe. A hung shutdown drives the operator to
        ``kill -9`` — the one uncatchable path that leaves the roaster
        commanded-hot — so this method NEVER blocks past the bound and NEVER
        re-raises: the agent must always be able to exit.

        On a clean stop within the bound, ``stop_unconfirmed`` is left
        ``False`` (it was reset by the preceding :meth:`start`, and the clean
        path never sets it) and the force-terminate hook is not invoked — so a
        ``start → stop`` cycle that confirms cleanly always reports
        ``stop_unconfirmed is False``, even after a previous run's stop went
        unconfirmed. On overrun the child process group is force-killed,
        ``stop_unconfirmed`` is set, and the method returns cleanly after
        logging at ERROR.
        """
        if self._stack is None:
            return
        stack = self._stack
        try:
            # asyncio.timeout (not wait_for) keeps aclose() running in THIS
            # task: the stdio_client context was entered in this task during
            # start(), and anyio cancel scopes must be exited in the task that
            # entered them — wait_for would re-parent aclose() into a child
            # task and trip "exit cancel scope in a different task".
            async with asyncio.timeout(self._config.stop_timeout_seconds):
                await stack.aclose()
        except TimeoutError:
            self._stop_unconfirmed = True
            _log.error(
                "MCP child did not confirm clean stop within %.1fs — "
                "force-terminating; restart will enter operator_recovery_required",
                self._config.stop_timeout_seconds,
            )
            if self._force_terminate is not None:
                # The hook must never block exit either: a raising hook (a
                # buggy injected seam — the real one absorbs OSError) is
                # logged and swallowed, not propagated out of teardown.
                try:
                    self._force_terminate()
                except Exception as ft_exc:
                    _log.error("force-terminate hook raised unexpectedly: %s", ft_exc)
            else:  # pragma: no cover - defensive: pid never captured
                _log.error(
                    "no force-terminate hook registered for wedged MCP child — "
                    "child may survive agent exit"
                )
        except Exception as exc:  # pragma: no cover - defensive: aclose error
            # A teardown error must not block exit either: log and move on.
            _log.error("error during MCP child stop: %s", exc)
        finally:
            self._stack = None
            self._session = None
            # Clean up the rendered yaml temp dir (D78-4, #420); best-effort —
            # a leftover temp dir is harmless, never blocks shutdown.
            if self._rendered_yaml_dir is not None:
                shutil.rmtree(self._rendered_yaml_dir, ignore_errors=True)
                self._rendered_yaml_dir = None

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
