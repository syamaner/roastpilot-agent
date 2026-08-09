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
import contextlib
import json
import logging
import math
import os
import secrets
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
from pydantic import BaseModel, ConfigDict, ValidationError

from roastpilot_agent.config import DEFAULT_MCP_COMMAND, MCPConfig, MCPDeviceConfig
from roastpilot_agent.models import AppliedRoasterState, MicStatus, RoastTelemetry

_log = logging.getLogger(__name__)

#: Grace added to ``startup_timeout_seconds`` for the outer ``await ready`` bound
#: in :meth:`MCPServerProcess.start` (#484). The owner task's own
#: ``initialize()`` already carries ``startup_timeout_seconds``, so this outer
#: bound is a backstop against an owner that never reports readiness at all; the
#: margin keeps it from racing the inner timeout on a merely-slow spawn.
_READY_TIMEOUT_MARGIN_SECONDS = 5.0

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
    # Overflow diagnostics (MCP 0.1.13, coffee-roaster-mcp#190): capture-side
    # frame-loss visibility. Defaults keep pre-0.1.13 payloads valid.
    overflow_count_last_minute: int = 0
    estimated_lost_audio_ms_last_minute: float = 0.0
    total_overflow_count: int = 0


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
    performance constraint: no per-window level work, #33) — including the
    overflow diagnostics trio (MCP 0.1.13, coffee-roaster-mcp#190, #539), which
    were parsed into the ``FirstCrackStatus`` mirror by #538 but dropped here
    until this fold — and lets :meth:`MicStatus.from_first_crack_status`
    derive the health the icon maps to.

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
        overflow_count_last_minute=status.overflow_count_last_minute,
        estimated_lost_audio_ms_last_minute=status.estimated_lost_audio_ms_last_minute,
        total_overflow_count=status.total_overflow_count,
    )


def ambient_reading_is_live(status: AmbientStatus) -> bool:
    """Whether the MCP's ambient RUNTIME is live (#732, #741).

    Deliberately says nothing about whether a reading exists or is usable: it
    inspects neither the triad nor the stamp, so a runtime that is up but has
    never read anything is "live" here (:func:`ambient_reading_token` returns
    ``None`` for it, and :func:`project_live_ambient` returns an all-``None``
    triad — the composite is what callers should reason about, not this
    predicate alone).

    This is the liveness half of :func:`project_live_ambient`'s gate, named
    separately because two different questions are asked of an ambient status
    and #752 made the difference load-bearing:

    * **Liveness** — is anything still refreshing the reading? A property of the
      *runtime*: a stopped or unavailable probe holds no live reading, and the
      one it preserved will never change again.
    * **Usability** — are the values it published representable? A property of
      *this poll's payload*, and a bad one says nothing about whether the
      runtime is alive.

    :meth:`RoasterControlAdapter._observe_ambient_age` keys its freshness clock
    on liveness alone, deliberately: a malformed payload must not reset the
    clock, because resetting it would re-base a demonstrably unrefreshed
    reading to age ``0.0`` and hand a stale value back as fresh — the exact
    laundering #732/#741/#745 exist to prevent (local Codex pass, pre-open).
    A runtime that genuinely stops still resets it, which is #732's own
    deliberate trade and is unchanged here.

    Args:
        status: The MCP ambient status from ``RoastSessionState``.

    Returns:
        ``True`` when the MCP reports ``"ok"`` **and** its ambient runtime is
        still running.
    """
    return status.status == "ok" and status.ambient_running


def project_live_ambient(status: AmbientStatus) -> tuple[float | None, float | None, float | None]:
    """Project an MCP ``AmbientStatus`` into the live ambient triad (#464, D86).

    A pure, read-only observability projection — no safety logic, no MCP
    write — mirroring :func:`project_mic_status`'s precedent exactly: it
    forwards the MCP's own already-computed ``status`` gate rather than
    re-deriving anything. This is the LATEST/live reading, refreshed every
    tick — distinct from the one-time charge-instant capture
    :meth:`RoastStore.set_ambient` persists (#342, D85), which this function
    does not touch.

    **``ambient_running`` is part of the gate (#732).** ``status == "ok"`` alone
    is *not* sufficient to call a reading live, because the MCP's
    ``AmbientSessionRuntime._stop_locked`` drops its reader (``ambient_running``
    goes ``False``) while deliberately leaving ``status`` at ``"ok"`` and
    *preserving the last reading* — after which
    ``AmbientSessionRuntime.poll``'s ``self._reader is None`` early return means
    that reading can never change again. Forwarding it would hand a permanently
    frozen room temperature to every downstream consumer, indistinguishable from
    a fresh one. This mattered little while ambient was observability-only, but
    ``c11`` (#709) selects a fan regime on it, and a stale *low* reading holds
    the model in the graduated regime after the room has warmed — the direction
    #498 warns about. A stopped runtime therefore degrades to the all-``None``
    absent-ambient triad, which ``c11`` already handles correctly.

    This deliberately changes OBSERVABILITY too, not just the advisor path: the
    dashboard's live "Room" readout (#464, D86) blanks when the runtime stops,
    where it previously showed the frozen reading indefinitely. That is the
    intended reading of the tile — it reports the current room, and once nothing
    is measuring the room there is nothing current to report.

    **A non-finite MEMBER voids the whole triad (#752).** #745 gave the reading's
    identity *stamp* this treatment (:func:`ambient_reading_token`); the measured
    values themselves had no such guard, so **on any transport that delivers a
    non-finite float intact** a ``NaN``/``±inf`` ``temperature_c`` reached
    :class:`RoastTelemetry`, the ``c11`` doctrine, and the corpus column
    unchanged. That qualifier is load-bearing and is spelled out at the end of
    this docstring: today's live child replies on ``structuredContent``, which
    launders a non-finite member to ``null`` before this function ever sees it,
    so on the current live path this guard is **defence in depth** rather than
    the thing standing between the doctrine and a bad value. It is still worth
    having — the text-content path does deliver one intact, and the hazard is
    real at the source: the MCP's ``YoctoMeteoAmbientReader._current_value``
    rejects only the Yoctopuce ``CURRENTVALUE_INVALID`` sentinel, with ``==``,
    which ``NaN`` defeats. A ``NaN`` member is producible at the probe; it is
    the transport that currently launders it.

    The failure is quiet rather than loud — ``NaN`` compares ``False``
    against the doctrine's ``threshold_c`` in BOTH directions, so it does not
    raise, it seats the model in whichever fan regime the comparison falls
    through to. The advisor path appears harmless today only by accident:
    ``AdvisorContext.model_dump_json()`` emits ``null`` for non-finite floats
    under pydantic v2's default ``ser_json_inf_nan="null"`` (verified), implicit
    version-dependent protection which ``model_dump()`` in python mode already
    does not provide. The *other* consumers of the same value have no such
    accident: :meth:`SseEvent.render` dumps with ``json.dumps``, which emits a
    bare ``NaN``/``Infinity`` token that a strict ``JSON.parse`` rejects for the
    whole frame (verified), and SQLite round-trips ``±inf`` into the corpus
    column faithfully — which is what ``scripts/rpd_corpus_score.py``'s
    ``_finite_or_none`` normalises. That shim is a reachability guard rather
    than a record of an observed row (it arrived with the scorer itself, from a
    reviewer's reachability argument), so the claim here is capability, not
    history; the capability is enough.

    Three choices worth stating rather than assuming:

    * **The unit is the TRIAD, not the member.** The three values are one poll of
      one device, published with one stamp, and the MCP's own
      ``AmbientRuntimeSnapshot`` nulls them together, so *from the live producer*
      a partially-populated triad is not a shape any consumer has had to
      interpret: a temperature-less reading still rendering humidity on the Room
      tile, or a corpus row claiming "a real, dateable reading of the room" for a
      reading whose room temperature is missing. A member that came back
      unrepresentable is therefore evidence the *reading* is malformed, not that
      one channel is. The cost of over-rejecting is concrete and worth owning:
      the charge capture is once-only and never retried, so a non-finite
      ``pressure_hpa`` on the charge tick discards a perfectly good
      ``temperature_c`` for the whole run. Under-rejecting costs a mislabelled
      RP-B arm (#709), which is worse.
    * **``humidity_percent`` is included on its own merits**, not just by
      atomicity: it reaches the advisor context through
      :meth:`RoastController._doctrine_ambient` exactly as the temperature does.
      ``c11`` tells the model humidity is background only, but "background" text
      in a prompt is still text in the prompt.
    * **``pressure_hpa`` is included even though it is corpus-only**, and that is
      not over-reach: it lands in the same ``roast_runs`` columns and carries the
      same non-finite-through-SQLite / bare-``Infinity``-in-JSON hazard the
      temperature does. Guarding the reading at its boundary is cheaper than one
      downstream normalisation per consumer.

    No type guard is needed alongside ``math.isfinite`` (unlike
    :func:`_payload_float`, which reads an untyped payload mapping): these are
    pydantic-validated ``float | None`` fields, so the only way a value survives
    validation and is still unusable is by being non-finite (pydantic's
    ``allow_inf_nan`` default). ``None`` members are untouched — absent is a
    legitimate state and must not be conflated with malformed.

    **What this does NOT close, stated so the atomicity claim above is not read
    wider than it is.** Which of the two MCP transport paths a reading arrives
    on decides whether this guard ever sees a non-finite value at all
    (``tests/test_mcp_client.py`` pins both):

    * ``structuredContent`` — the MCP serialises it with pydantic, so a
      non-finite member is already ``null`` by the time
      :func:`parse_tool_result` reads it. The guard is a no-op there, and the
      triad arrives partially populated instead of malformed. **This is the
      live path today**: the child's ``get_roast_state`` is a FastMCP
      ``@mcp.tool()`` returning a dataclass, so its reply carries
      ``structuredContent``, and :func:`parse_tool_result` prefers that
      whenever it is a multi-key dict and never reaches the text block.
    * the text content block — parsed with :func:`json.loads`, whose default
      ``parse_constant`` accepts the bare ``Infinity``/``NaN`` tokens, so the
      value does arrive non-finite. This is the path the guard is load-bearing
      on, and it is what an older or non-FastMCP server, or the scalar-wrapper
      shape, would use.

    So a *partially populated* triad is reachable regardless — and on the live
    transport it is the ONLY reachable shape of this fault — and this function
    deliberately still forwards one. Whether an incomplete triad should itself
    be voided is a separate decision that turns on whether any supported ambient
    probe legitimately reports fewer than three members. The evidence points at
    "no" (the MCP's ``AmbientReading`` requires all three floats, and
    ``AmbientRuntimeSnapshot`` nulls them together), but the ``AmbientStatus``
    mirror allows it and that evidence is one MCP version deep, so it is filed
    rather than guessed at here: guessing wrong silently disables ambient for
    that hardware.

    Guarding here rather than at each call site follows #745's shape: this is the
    boundary every consumer already goes through, so
    :func:`project_recordable_ambient`, :func:`project_session_state` and the
    freshness tracker inherit it and cannot disagree. A non-finite reading then
    reads as an ABSENT reading — the branch ``c11`` already handles, and the
    #498-safe direction, since declining can only leave the graduated fan regime
    and never enter it.

    Args:
        status: The MCP ambient status from ``RoastSessionState``.

    Returns:
        The ``(temperature_c, humidity_percent, pressure_hpa)`` triad when
        ``status.status == "ok"``, ``status.ambient_running``, **and** every
        present member is finite; else ``(None, None, None)`` — the MCP's own
        fail-soft contract for a disabled/unavailable probe, extended to a
        stopped-but-``ok`` runtime and to a malformed reading.
    """
    if not ambient_reading_is_live(status):
        return None, None, None
    triad = (status.temperature_c, status.humidity_percent, status.pressure_hpa)
    if any(value is not None and not math.isfinite(value) for value in triad):
        return None, None, None
    return triad


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


class MalformedCommandResultError(Exception):
    """An MCP command result's event payload is missing an expected field, or
    carries a well-typed but out-of-range one.

    Raised by :func:`applied_state_from_event` when ``heat_level_percent`` /
    ``fan_level_percent`` / ``cooling_on`` are absent, the wrong type, or (for
    the two integers) outside the 0-100 percent bound
    :class:`~roastpilot_agent.models.AppliedRoasterState` enforces — the MCP's
    own contract guarantees all three, in range, on ``beans_dropped`` and
    ``fault`` (`coffee_roaster_mcp.session.complete_reserved_driver_drop_snapshot`
    / `default_emergency_safety_payload`), so this only fires against a
    genuinely malformed or out-of-contract payload. Every caller of this
    function (the adapter's ``_applied_state_or_none``, the replay fallback)
    catches ONLY this one exception type to detect "payload could not be
    parsed" — so a pydantic ``ValidationError`` from an out-of-range value
    must never escape this function directly; it is translated into this type
    (Codex follow-up on #509/#507: an out-of-range value previously escaped as
    a raw ``ValidationError``, bypassing that single choke point and
    recreating the exact treat-drop-as-failed divergence the ``None`` design
    exists to prevent). Never fabricate a guessed applied state from a
    partial/invalid payload.
    """


def applied_state_from_event(event: EventSnapshot) -> AppliedRoasterState:
    """Extract the driver's applied heat/fan/cooling state from a command event.

    ``drop_beans`` and ``emergency_stop`` change heat/fan/cooling as a
    hardware side effect of the MCP command itself (not through a separate
    ``set_targets`` call), so the applied state has to come from the
    command's own result rather than a later telemetry poll. The MCP always
    carries it on the resulting event's payload (``beans_dropped`` /
    ``fault``) — see :class:`MalformedCommandResultError`.

    Args:
        event: The ``EventSnapshot`` returned by the drop/emergency-stop tool
            call (``EventCommandResult.event``).

    Returns:
        The applied roaster state the driver actually set.

    Raises:
        MalformedCommandResultError: A required field is missing, the wrong
            type, or (heat/fan) outside the 0-100 percent bound.
    """
    heat = event.payload.get("heat_level_percent")
    fan = event.payload.get("fan_level_percent")
    cooling_on = event.payload.get("cooling_on")
    if isinstance(heat, bool) or not isinstance(heat, int):
        raise MalformedCommandResultError(
            f"{event.kind!r} event payload missing integer heat_level_percent: {heat!r}"
        )
    if isinstance(fan, bool) or not isinstance(fan, int):
        raise MalformedCommandResultError(
            f"{event.kind!r} event payload missing integer fan_level_percent: {fan!r}"
        )
    if not isinstance(cooling_on, bool):
        raise MalformedCommandResultError(
            f"{event.kind!r} event payload missing boolean cooling_on: {cooling_on!r}"
        )
    try:
        return AppliedRoasterState(
            heat_level_percent=heat,
            fan_level_percent=fan,
            cooling_on=cooling_on,
        )
    except ValidationError as exc:
        # A well-typed-but-out-of-range value (e.g. heat=101 or fan=-1) —
        # AppliedRoasterState's own Field(ge=0, le=100) bounds raise here.
        # Translated into the ONE exception type every caller catches (see
        # MalformedCommandResultError's docstring) rather than letting a raw
        # ValidationError escape this function.
        raise MalformedCommandResultError(
            f"{event.kind!r} event payload has an out-of-range applied state "
            f"(heat_level_percent={heat!r}, fan_level_percent={fan!r}): {exc}"
        ) from exc


def ambient_reading_token(status: AmbientStatus) -> float | None:
    """Return an opaque identity token for the MCP's current ambient reading.

    The token is ``last_reading_monotonic_seconds`` — an absolute ``time.monotonic``
    stamp from the **MCP child process** (D6). It is used **only for equality
    comparison against a previously observed token**, never as a timestamp and
    never in arithmetic against the agent's clock: cross-process monotonic
    clocks are not comparable (the rule :meth:`RoastController._backdated_now`
    states and this module's backdating deltas already observe). A change in the
    token means "the MCP took a new reading"; the elapsed time since that change
    is then measured entirely in the agent's own clock domain by
    :meth:`RoasterControlAdapter.read_telemetry`.

    **A non-finite stamp is not a token (#745b).** Equality is the whole
    mechanism here, and ``NaN`` is unequal to itself under IEEE-754, so a
    malformed MCP or child response carrying ``NaN`` makes every tick look like
    a *new* reading: the derived age is re-based to ``now`` every tick, never
    advances past ``0.0``, and the controller's range check upstream — which is
    written to fail closed on ``NaN``/negative *ages* — never sees anything but
    a perfectly fresh one. A frozen value stays permanently "fresh", which is
    the one thing the freshness clock exists to prevent. ``±inf`` is rejected
    with it: it compares equal to itself and so would not defeat the clock, but
    a non-finite stamp is malformed either way and there is no reason to carry
    one. No type guard is needed alongside it, unlike :func:`_payload_float`:
    that reads an untyped ``EventPayloadValue`` mapping, whereas
    ``AmbientStatus.last_reading_monotonic_seconds`` is a pydantic-validated
    ``float | None``, so the only way a value survives validation and is still
    unusable is by being non-finite (pydantic's ``allow_inf_nan`` default).

    Rejecting here rather than at the call site is deliberate: the token's
    identity semantics are what ``NaN`` breaks, so the guard belongs at the
    boundary every consumer already goes through. An unusable stamp reads as
    "no reading", which the age tracker already treats as age-unknown and the
    controller already fails closed on — the reading is declined and ``c11``
    takes its absent-ambient path, the #498-safe direction.

    Args:
        status: The MCP ambient status from ``RoastSessionState``.

    Returns:
        The reading's identity token, or ``None`` when the runtime holds no
        reading at all (its ``AmbientRuntimeSnapshot`` nulls the stamp and the
        triad together, so an absent token means an absent reading) or when the
        stamp is not a finite number.
    """
    stamp = status.last_reading_monotonic_seconds
    if stamp is None or not math.isfinite(stamp):
        return None
    return stamp


def project_recordable_ambient(
    status: AmbientStatus,
) -> tuple[float | None, float | None, float | None]:
    """The ambient triad, but only when it is worth RECORDING (#745).

    Strictly narrower than :func:`project_live_ambient`: it additionally
    requires a usable :func:`ambient_reading_token`, i.e. a reading whose
    freshness can even be established. Everything the live projection rejects is
    rejected here too — including a triad with a non-finite member (#752), which
    is guarded there rather than here precisely so this predicate stays a pure
    narrowing. Without that extra clause the two
    #745 fixes leave a hole between them — a ``NaN`` stamp on an otherwise
    ``ok``/running status makes the live advisor DECLINE the reading (the age
    is unknown, and the controller fails closed on that), while the
    charge-instant capture would still have persisted the numeric triad. The
    run would read back as "had ambient", and #737's offline eval would stamp
    that value into every replayed context — reasoning on a reading the live
    advisor rejected, which is precisely the mislabelled RP-B arm (#709) that
    #745 exists to remove. One predicate, so the two cannot disagree **about
    whether a real, dateable reading existed at charge**.

    That is deliberately narrower than "the advisor reasoned on ambient", and
    the difference is worth stating precisely rather than leaving a half-true
    claim in a safety-adjacent docstring. The doctrine applies two further
    clauses this predicate does not (see below), so a run CAN carry a recorded
    triad while every advisory tick declined the reading: a running-but-not-
    polled probe holding a finite, unchanging stamp — the one freeze path
    ``ambient_running`` cannot catch, documented on
    :meth:`RoasterControlAdapter._observe_ambient_age` — or a runtime that stops
    AFTER charge. Today the only signal for that is a run-id-less process log,
    which is the same gap #742 raises for doctrine retirement and is tracked
    there. **A populated ambient column is therefore not evidence that the
    doctrine saw ambient**; scoring a c11 arm needs #742's per-run record.

    It deliberately does **not** apply the doctrine's
    ``max_reading_age_seconds`` bound, and is not gated on the doctrine being
    enabled. Those are c11 policy about what may be REASONED on this tick; this
    answers the different and older question (#342/D85) of whether there is a
    real, dateable reading of the room to record at charge. A corpus that only
    captured ambient while c11 happened to be enabled would be useless for the
    analysis the column was added for.

    :func:`project_live_ambient` is deliberately left alone. The dashboard's
    Room tile reports what the probe last said and #741 already settled when it
    blanks; this column reports the room at charge for later analysis and must
    not carry a reading nothing can date. Different questions, so a stricter
    predicate rather than a change to the shared one.

    Args:
        status: The MCP ambient status from ``RoastSessionState``.

    Returns:
        The ``(temperature_c, humidity_percent, pressure_hpa)`` triad when the
        reading is both live and dateable, else ``(None, None, None)``.
    """
    if ambient_reading_token(status) is None:
        return None, None, None
    return project_live_ambient(status)


def project_session_state(
    state: RoastSessionState,
    *,
    age_seconds: float,
    ambient_age_seconds: float | None = None,
) -> RoastTelemetry | None:
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
    per-reading wall-clock age. ``ambient_age_seconds`` is likewise
    caller-supplied (#732) for the same reason, and defaults to ``None`` =
    "age unknown", the fail-closed value for callers that do not track it."""
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
        ambient_age_seconds=None if ambient_temp_c is None else ambient_age_seconds,
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
        self._last_ambient_token: float | None = None
        self._ambient_token_change_monotonic: float | None = None

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
        return project_session_state(
            state,
            age_seconds=age,
            ambient_age_seconds=self._observe_ambient_age(state.ambient_status, now=now),
        )

    def _observe_ambient_age(self, status: AmbientStatus, *, now: float) -> float | None:
        """Track how long the MCP's current ambient reading has been current (#732).

        Deliberately the same shape as the ``age_seconds`` derivation above, for
        the same reason: the freshness signal must live in the **agent's** clock
        domain. The MCP's ``last_reading_monotonic_seconds`` is used only as an
        opaque identity token (see :func:`ambient_reading_token`) — compared for
        *change*, never subtracted from anything — because cross-process
        ``time.monotonic`` clocks are not comparable.

        Freshness is tracked for a reading the MCP still HOLDS LIVE
        (:func:`ambient_reading_is_live`), not for whatever stamp the status
        happens to carry — one source of truth for "is there a live reading".
        That matters for the stopped-runtime case in particular: the MCP keeps
        reporting the frozen reading's stamp after ``ambient_running`` goes
        ``False``, so a token-only tracker would go on ageing a reading nothing
        consumes, and a runtime that resumed on that same preserved reading
        would inherit an age from before the outage. A reading that is no longer
        live therefore resets the tracker, and a probe that drops out and
        returns is aged from the reading it comes back with.

        **Liveness, NOT usability (#752).** The gate here is deliberately the
        liveness half only, where the projection this feeds applies both halves.
        A live runtime that publishes a non-finite value on one poll has not
        stopped holding its reading — the stamp still identifies the same
        reading — so the clock keeps running against it. Resetting on a
        malformed payload instead would re-base a demonstrably unrefreshed
        reading to ``0.0``, so a single bad tick would launder a stale value
        back into the doctrine's freshness bound and could re-enter the
        graduated fan regime. The age returned during such a tick is never
        consumed: :func:`project_session_state` nulls it along with the triad,
        because that projection applies the usability half too.

        Known and deliberate under-report: the age is measured from the agent's
        *first observation* of a token, so a reading already old when first seen
        reads as 0.0. Two distinct freeze paths bound that residue, and they are
        caught by different mechanisms — worth stating precisely, because the
        obvious reading (that ``ambient_running`` covers freezing generally) is
        wrong:

        * **Runtime stopped** — ``_stop_locked`` drops the reader while leaving
          ``status`` at ``"ok"``. Caught structurally by
          :func:`project_live_ambient`'s ``ambient_running`` gate; the age never
          has to notice.
        * **Runtime running but not polled** — the MCP polls ambient only for an
          active, id-matched session, so with no active session ``poll`` is
          skipped while the reader stays non-``None`` and ``status`` stays
          ``"ok"``. ``ambient_running`` is ``True`` here, so **only the age gate
          catches this one.** Unreachable on today's live path (the agent omits
          the session id, and the adapter is constructed once over a freshly
          spawned child, so first observation cannot predate the session), but
          it is the case that makes the age gate load-bearing rather than
          belt-and-braces.

        While a session IS being polled, an unchanging token means a live probe
        returning the same instant: ``poll`` attempts a read every
        ``poll_interval_seconds`` and demotes ``status`` to ``"unavailable"`` on
        failure.

        Args:
            status: The MCP ambient status from this tick's session state.
            now: The agent-clock instant of this read.

        Returns:
            Seconds since the current reading was first observed, or ``None``
            when the MCP holds no live, dateable reading — "age unknown", the
            fail-closed value.
        """
        token = ambient_reading_token(status) if ambient_reading_is_live(status) else None
        if token is None:
            self._last_ambient_token = None
            self._ambient_token_change_monotonic = None
            return None
        if self._ambient_token_change_monotonic is None or token != self._last_ambient_token:
            self._last_ambient_token = token
            self._ambient_token_change_monotonic = now
        return now - self._ambient_token_change_monotonic

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
        # #732: the ambient tracker is age-tracking state exactly like the two
        # above and must reset with them. The MCP's stop path deliberately
        # PRESERVES the last ambient reading and its stamp, and a stop/start
        # pair need not have an intervening telemetry read — so on back-to-back
        # roasts through one adapter the first state of the new session can
        # carry the previous roast's token. Left unreset, that either ages the
        # new run's first reading from the old run's clock (passing a stale
        # reading as fresh) or declines it immediately and burns the new run's
        # one-shot decline warning on a phantom.
        self._last_ambient_token = None
        self._ambient_token_change_monotonic = None
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

    async def drop_beans(self) -> AppliedRoasterState | None:
        """Drop the beans and return the driver's applied post-drop state.

        ``drop_beans`` sets heat/fan/cooling as a hardware side effect of the
        command itself (mirrors: 0 % heat, 100 % fan, cooling on), so the
        applied state is read from the ``beans_dropped`` event's own payload
        (#507) rather than assumed by the controller.

        Returns ``None`` (rather than raising) when the payload is malformed
        or out-of-contract (#507 safety-review fix): the hardware drop has
        ALREADY happened by this point — the beans are out — so a payload
        parse failure must never surface as a caller-side exception. Every
        drop caller treats an exception as "the write itself failed" (no
        transition, ``COMMAND_FAILED``, safe to retry next tick); if this
        raised on a malformed-but-successful drop, the caller would wrongly
        hold DEVELOPMENT and re-fire ``drop_beans`` on a machine that already
        dropped — an FSM-vs-physical divergence. ``None`` lets every caller
        proceed exactly as it does today (transition, ``COMMAND_EXECUTED``)
        while simply not adopting a value into the commanded-value mirrors —
        stale-but-honest mirrors, never a fabricated value and never a
        spurious failure.
        """
        result = await self._client.drop_beans()
        return self._applied_state_or_none(result.event)

    async def start_cooling(self) -> None:
        await self._client.start_cooling()

    async def stop_cooling(self) -> None:
        await self._client.stop_cooling()

    async def emergency_stop(self, *, reason: str) -> AppliedRoasterState | None:
        """Fire the MCP emergency stop and return the driver's applied state.

        Mirrors :meth:`drop_beans`: ``emergency_stop`` sets heat/fan/cooling as
        a hardware side effect (heat 0 %, safe fan, cooling on), read from the
        resulting ``fault`` event's own payload (#507). Returns ``None`` on a
        malformed payload for the identical reason ``drop_beans`` does — the
        hardware stop already happened, so a parse failure must not surface as
        a caller-side exception (which every e-stop caller would otherwise
        treat as "the stop itself failed", queuing a needless heat-off retry
        on a machine that is already safely stopped)."""
        result = await self._client.emergency_stop(reason)
        return self._applied_state_or_none(result.event)

    @staticmethod
    def _applied_state_or_none(event: EventSnapshot) -> AppliedRoasterState | None:
        """Parse the applied state, degrading a malformed payload to ``None``.

        Logs at WARNING with the malformed payload's keys (never the values —
        avoid amplifying whatever garbage is in a genuinely malformed
        payload into the log) so a real MCP contract regression is visible,
        without ever fabricating a guessed heat/fan/cooling value.
        """
        try:
            return applied_state_from_event(event)
        except MalformedCommandResultError:
            _log.warning(
                "%s event payload missing/malformed applied-state fields "
                "(keys present: %s); the hardware command already ran — "
                "proceeding without adopting a value into the commanded mirrors",
                event.kind,
                sorted(event.payload.keys()),
                exc_info=True,
            )
            return None

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
#: tree, invoked whenever the current lifecycle cannot confirm clean teardown.
#: Returns ``True`` if a termination signal was actually delivered (a child was
#: known and alive), ``False`` otherwise. Injectable so fail-closed paths are
#: unit-testable without a real process.
ForceTerminate = Callable[[], bool]


def force_terminate_process_group(pid: int) -> bool:
    """Force-kill the child process *group* by pid (POSIX only).

    The transport spawns the child with ``start_new_session=True`` (the MCP
    SDK's ``_create_platform_compatible_process``), so the child is its own
    session/process-group leader and ``pgid == pid``. Sending ``SIGKILL`` to
    the group atomically reaps the child and anything it forked (the audio
    worker), which is exactly what a wedged-child shutdown needs.

    This is the uncatchable last resort after the owner exits unexpectedly or
    graceful teardown cannot be confirmed. SIGKILL (not a catchable SIGTERM the
    uncertain or wedged child may never service) is deliberate.

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
                and registers a process-group force-terminate hook used when
                the current lifecycle cannot confirm clean teardown.
            force_terminate: An injectable force-terminate hook (test seam).
                When provided, it is used directly on fail-closed teardown
                instead of the spawned-pid hook, letting uncertain-lifecycle
                paths be unit tested without a real process.
        """
        self._config = config or MCPConfig()
        self._device_config: MCPDeviceConfig | None = device_config
        self._session: ToolSession | None = session  # injectable test seam
        self._session_factory: SessionFactory = (
            session_factory if session_factory is not None else self._default_session_factory
        )
        #: The task that owns the spawned session's context stack for its whole
        #: lifetime (#484).  The ``stdio_client``/``ClientSession`` anyio cancel
        #: scopes MUST be entered and exited in the SAME task, so a single owner
        #: coroutine (:meth:`_run_session`) enters the ``async with`` stack, holds
        #: it open across an :attr:`_stop_requested` wait, and exits it in-task on
        #: request.  :meth:`start` / :meth:`stop` are cross-task REQUESTS to this
        #: owner — they never enter or exit the stack themselves, so a respawn
        #: driven from a request-handler task can no longer trip "exit cancel
        #: scope in a different task".  ``None`` when no spawn is live (injected
        #: session, or before :meth:`start` / after :meth:`stop`).
        self._owner_task: asyncio.Task[None] | None = None
        #: Set by :meth:`stop` to ask the owner task to exit its context stack.
        #: Created per spawn so a fresh start never inherits a set event.
        self._stop_requested: asyncio.Event | None = None
        #: Set when an owner exits unexpectedly or teardown cannot confirm a
        #: clean shutdown. Force-termination is best-effort; #177 persists the
        #: flag so a restart enters ``operator_recovery_required``.
        self._stop_unconfirmed = False
        #: Opaque identity for the current uncertain lifecycle. Acknowledgements
        #: must name this exact incident so a delayed retry cannot clear a later
        #: generation's unrelated teardown failure (#668).
        self._teardown_incident_id: str | None = None
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
        :meth:`_register_force_terminate` so any unconfirmed current lifecycle
        can force-kill the process group. An explicitly injected
        ``force_terminate`` (test seam) is left untouched; only an unset hook is
        populated by the spawn.

        Args:
            params: The stdio spawn parameters.

        Returns:
            An async context manager yielding the initialized session.
        """
        return _spawn_stdio_session(params, on_spawn=self._register_force_terminate)

    def _register_force_terminate(self, pid: int) -> None:
        """Record the current child's fail-closed process-group kill hook.

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
        """Whether the most recent child lifecycle lacked confirmed clean teardown.

        ``True`` means an owner exited unexpectedly or graceful teardown could
        not be confirmed within ``stop_timeout_seconds``. Force-termination is
        attempted when a current child hook is available, but the flag does not
        claim that attempt succeeded. It remains ``True`` until the operator
        acknowledges the matching teardown incident after physical verification;
        :meth:`start` refuses to spawn a new owner while it is set. A confirmed
        clean stop leaves it ``False``. #177 persists this to the decision trace so an unconfirmed
        lifecycle is visible post-roast (observability for diagnosis / recovery
        — never an auto-resume trigger).
        """
        return self._stop_unconfirmed

    @property
    def teardown_incident_id(self) -> str | None:
        """Opaque identity of the current unconfirmed teardown, if any (#668)."""
        return self._teardown_incident_id

    def acknowledge_hardware_clear(self, teardown_incident_id: str) -> None:
        """Clear an idle generation after explicit physical verification (#668).

        This is process-state cleanup only: it never starts a child, opens an
        MCP session, or writes heat/fan/cooling state. The service records the
        operator's durable acknowledgement before calling this method.

        Raises:
            MCPConnectionError: Teardown is not unconfirmed, or a session/owner
                can still be live or unwinding.
        """
        blocker = self.hardware_clear_acknowledgement_blocker(teardown_incident_id)
        if blocker is not None:
            raise MCPConnectionError(blocker)
        owner = self._owner_task

        if owner is not None:
            with contextlib.suppress(BaseException):
                owner.result()
        self._owner_task = None
        self._stop_requested = None
        self._stop_unconfirmed = False
        self._teardown_incident_id = None
        if not self._force_terminate_injected:
            self._force_terminate = None
        if self._rendered_yaml_dir is not None:
            shutil.rmtree(self._rendered_yaml_dir, ignore_errors=True)
            self._rendered_yaml_dir = None

    def hardware_clear_acknowledgement_blocker(self, teardown_incident_id: str) -> str | None:
        """Return why generation state cannot currently be acknowledged (#668)."""
        if not self._stop_unconfirmed:
            return "no unconfirmed MCP teardown requires acknowledgement"
        if self._teardown_incident_id is None:
            return "unconfirmed MCP teardown has no acknowledgement identity"
        if teardown_incident_id != self._teardown_incident_id:
            return "teardown incident does not match the current unconfirmed lifecycle"
        if self._session is not None:
            return "an MCP session is still attached"
        owner = self._owner_task
        if owner is not None and not owner.done():
            return "the MCP owner is still tearing down"
        return None

    async def _run_session(self, ready: asyncio.Future[ToolSession]) -> None:
        """Own the spawned session's context stack for its whole lifetime (#484).

        Entered as a dedicated task by :meth:`start` so the ``stdio_client`` /
        ``ClientSession`` anyio cancel scopes are entered AND exited in ONE task
        — the same-task invariant anyio enforces.  The flow is:

        1. Enter the factory context (spawn the child + open the session),
           initialize it, and health-check it through the public surface.
        2. Resolve ``ready`` with the live session (or its startup error) so the
           awaiting :meth:`start` returns.
        3. Hold the ``async with`` stack open, parked on :attr:`_stop_requested`,
           until :meth:`stop` (from ANY task) asks the owner to shut down.
        4. Exit the stack here, in this task, so no cross-task scope exit occurs.

        A startup failure resolves ``ready`` with the exception and returns
        WITHOUT parking (the context has already unwound in-task), so
        :meth:`start` re-raises it and leaves the process not-running.
        Any BaseException from the owned context body fails closed before
        context unwind begins, while this generation's PID hook is current.
        The outer guard also handles context-enter/exit failures without
        repeating that action. A later :meth:`start` never signals a completed
        owner's PID.

        Args:
            ready: Future the caller (``start``) awaits; resolved with the live
                session on success or the startup exception on failure.
        """
        stop_requested = self._stop_requested
        try:
            if stop_requested is None:  # pragma: no cover - start() always sets it
                # Defensive: start() sets _stop_requested before launching us, so
                # this is unreachable — but never leave ``ready`` dangling, or
                # ``start`` would hang. Fail closed with a clear error.
                raise MCPConnectionError("MCP owner task started without a stop signal")
            async with AsyncExitStack() as stack:
                try:
                    try:
                        session = await stack.enter_async_context(
                            self._session_factory(self.build_server_parameters())
                        )
                        await asyncio.wait_for(
                            session.initialize(), timeout=self._config.startup_timeout_seconds
                        )
                        self._session = session
                        # Health check through the public surface before we report ready.
                        await self.call_tool("get_server_info", {})
                    except Exception as exc:  # startup failure: unwind + report to start()
                        self._session = None
                        # First (and only) resolution on this path — ready is still
                        # pending here (nothing else resolves it), so set directly; a
                        # hypothetical double-set would raise InvalidStateError, which
                        # the outer BaseException guard + the finally backstop catch.
                        ready.set_exception(exc)
                        return  # exits the `async with` here, in THIS task
                    # Startup succeeded: hand the session to start() and park until stop.
                    ready.set_result(session)
                    await stop_requested.wait()
                    # Falls out of the `async with` → stack.aclose() runs IN THIS TASK,
                    # so the stdio_client cancel scope exits where it was entered.
                except BaseException as exc:
                    # Act before AsyncExitStack unwinds. A slow __aexit__ may outlive
                    # the child and make this generation's PID unsafe to signal.
                    if self._owner_task is asyncio.current_task() and not self._stop_unconfirmed:
                        self._fail_closed_teardown(f"MCP owner task exited uncleanly: {exc!r}")
                    raise
        except BaseException as exc:  # noqa: BLE001 — ready must never dangle
            # Fail closed while this owner still identifies the current child
            # generation. Delaying this until a later start() would reuse a PID
            # hook after the child may have exited and its PID been recycled.
            if self._owner_task is asyncio.current_task() and not self._stop_unconfirmed:
                reason = (
                    f"MCP child stop raised during teardown: {exc}"
                    if stop_requested is not None and stop_requested.is_set()
                    else f"MCP owner task exited uncleanly: {exc!r}"
                )
                self._fail_closed_teardown(reason)
            # Any exit path that reaches here (a raise before/after the inner
            # try, a cancellation, an aclose error) MUST resolve ``ready`` or
            # ``start``'s ``await ready`` hangs forever. The inner success/failure
            # paths already resolved it on the common paths, so guard against a
            # double-set here — this fires only when the raise happened BEFORE the
            # inner resolution (e.g. the stop_requested-None guard, or a spawn that
            # raised a BaseException in __aenter__).
            if not ready.done():
                ready.set_exception(
                    exc
                    if isinstance(exc, Exception)
                    else MCPConnectionError(f"MCP owner task aborted: {exc!r}")
                )
            raise
        finally:
            self._session = None
            # Absolute backstop: if some path left ``ready`` unresolved (should be
            # impossible after the guards above), fail it closed rather than hang.
            if not ready.done():  # pragma: no cover - defensive: unreachable
                ready.set_exception(
                    MCPConnectionError("MCP owner task exited without reporting readiness")
                )

    async def start(self) -> None:
        """Spawn the child, initialize the MCP session, health-check it.

        The spawned session's context stack is owned end-to-end by a dedicated
        :meth:`_run_session` task (#484) so its anyio cancel scopes are entered
        and exited in the SAME task; ``start`` merely launches that task and
        awaits its ``ready`` signal.  This is what lets a respawn driven from a
        request-handler task tear the child down without a cross-task scope exit.

        Within one running agent process, an unconfirmed teardown and its
        incident identity block every new owner until the explicit
        hardware-clear acknowledgement consumes that exact incident (#668).
        ``start`` never clears that process-local verdict itself. A controlled
        full agent restart after physical verification remains the legacy
        recovery boundary; persisting incidents across processes is outside
        this in-process contract.
        A prior owner retained after a bounded teardown attempt must be
        finalized by :meth:`stop` before another child can start, even if the
        owner task has since finished. Otherwise an old stop body/finalizer
        could act on the replacement generation. Unexpected owner failure
        fails closed inside the owner task while its PID hook is still current;
        start never signals a completed generation's potentially stale PID.

        Re-arms the force-terminate hook for each spawn: on the first start the
        auto-registered hook captures the spawned pid via
        :meth:`_register_force_terminate`; on a respawn the previous pid's
        closure would still be held, so the hook is cleared here (before the
        spawn) so :meth:`_register_force_terminate` re-registers with the new
        pid.  An injected hook (test seam, ``_force_terminate_injected=True``)
        is never cleared — it must win and be reused across respawns.
        """
        stop_requested = self._stop_requested
        if self._session is not None and (stop_requested is None or not stop_requested.is_set()):
            return
        previous_owner = self._owner_task
        if previous_owner is not None:
            if not previous_owner.done():
                raise MCPConnectionError(
                    "previous MCP owner is still tearing down; refusing to start a second child"
                )
            raise MCPConnectionError(
                "previous MCP owner is awaiting stop finalization; refusing to start a second child"
            )
        if self._stop_unconfirmed:
            raise MCPConnectionError(
                "previous MCP teardown was unconfirmed; explicit hardware-clear "
                "acknowledgement is required before a fresh child can start"
            )
        # Re-arm: clear the auto-registered hook before each spawn so
        # _register_force_terminate captures the new pid, not the previous one.
        # Injected hooks (test seam) are left untouched.
        if not self._force_terminate_injected:
            self._force_terminate = None
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[ToolSession] = loop.create_future()
        self._stop_requested = asyncio.Event()
        owner = asyncio.create_task(self._run_session(ready))
        self._owner_task = owner
        try:
            # Bound the wait: _run_session resolves ``ready`` on success, on
            # startup failure, and via its own last-resort guards — but a bound
            # here means even a pathological owner that never reports readiness
            # becomes a clean startup failure instead of an unbounded hang.
            #
            # The bound must COVER how the owner composes its own inner bounds
            # before it resolves ``ready`` (Codex #492-2): the owner runs
            # ``initialize()`` (bounded by startup_timeout_seconds) THEN the
            # ``get_server_info`` health check (bounded by call_timeout_seconds),
            # sequentially, so the worst-case time-to-ready is their SUM. Using
            # only startup_timeout_seconds would fire a FALSE startup failure on a
            # deployment with a large call_timeout_seconds. Add both inner bounds
            # plus a small margin so this outer bound only trips on a wholly
            # unresponsive owner, never on a merely-slow-but-progressing one.
            # shield: a wait_for timeout must not cancel ``ready`` out from under
            # the owner — _await_owner_finished reaps the owner on every failure.
            await asyncio.wait_for(
                asyncio.shield(ready),
                timeout=self._ready_timeout_seconds(),
            )
        except MCPConnectionError:
            await self._await_owner_finished()
            raise
        except Exception as exc:
            await self._await_owner_finished()
            raise MCPConnectionError(f"failed to start coffee-roaster-mcp: {exc}") from exc
        except BaseException:
            # A BaseException surfaced via ``ready`` (e.g. a KeyboardInterrupt at
            # spawn) or wait_for's own cancellation must still reap the owner so
            # no child/task is orphaned, then propagate unchanged.
            await self._await_owner_finished()
            raise

    def _ready_timeout_seconds(self) -> float:
        """Outer bound for ``start()``'s ``await ready`` (Codex #492-2).

        ``_run_session`` resolves ``ready`` only after running ``initialize()``
        (bounded by ``startup_timeout_seconds``) and THEN the ``get_server_info``
        health check (bounded by ``call_timeout_seconds``) in sequence, so the
        worst-case time-to-ready is their SUM. Bounding only on
        ``startup_timeout_seconds`` would fire a false startup failure whenever
        ``call_timeout_seconds`` is configured large. The margin keeps this outer
        bound from racing a merely-slow-but-progressing spawn.

        Returns:
            The ``await ready`` timeout in seconds:
            ``startup_timeout_seconds + call_timeout_seconds + margin``.
        """
        return (
            self._config.startup_timeout_seconds
            + self._config.call_timeout_seconds
            + _READY_TIMEOUT_MARGIN_SECONDS
        )

    async def _await_owner_finished(self) -> None:
        """Reap + clear the owner task after a start failure — natural first.

        Ordering matters (Codex #492-1): on the normal startup-failure path
        (``initialize()`` timeout / ``get_server_info`` failure) the owner has
        ALREADY entered the session context and resolved ``ready`` exceptionally
        while its ``async with`` stack is still exiting IN ITS OWN TASK. Cancelling
        it there could abort that in-task child cleanup and orphan the child. So we
        **await the owner's natural completion FIRST, bounded**, and only
        ``cancel()`` if that bound overruns — which is the genuinely-stuck case
        (blocked in the factory ``__aenter__`` after a ``start()`` cancelled
        mid-spawn, where a bare await would hang forever). The task result (of
        ANY type — already delivered via ``ready``) is drained without
        re-raising. Bounded by
        ``stop_timeout_seconds`` so a wedged native child during a startup abort
        bounds this reap call; on overrun the force-kill hook (if armed) reaps the
        child group, mirroring :meth:`stop`. Owner identity is cleared only
        after confirmed task completion; a cancellation-resistant owner remains
        retained so :meth:`start` refuses a replacement until :meth:`stop`
        finalizes it.
        """
        owner = self._owner_task
        if owner is None:  # pragma: no cover - defensive: start() always set it
            return
        # Natural completion first: an already-tearing-down owner must be let
        # finish its in-task child cleanup, not cancelled mid-unwind. ``wait``
        # (not wait_for) does NOT cancel the owner on timeout — it just reports
        # whether it finished — so a still-progressing teardown is never aborted.
        done, _pending = await asyncio.wait({owner}, timeout=self._config.stop_timeout_seconds)
        if owner in done:
            # Completed on its own. Drain the stored exception (of ANY type — it
            # was already delivered to start() via ``ready``) so it is retrieved,
            # never re-raised out of this best-effort reap.
            with contextlib.suppress(BaseException):
                owner.result()
            if self._owner_task is owner:
                self._owner_task = None
                self._stop_requested = None
            return
        # Did NOT complete in time — genuinely stuck (blocked in __aenter__ after
        # a cancelled start, or a wedged native child). NOW cancel to unblock it,
        # fail closed (force-kill + unconfirmed), and bounded-reap; a startup
        # teardown call must stay bounded, so any reap outcome is swallowed.
        if self._owner_task is owner and not self._stop_unconfirmed:
            self._fail_closed_teardown("MCP child did not unwind after start failure")
            owner.cancel()
        done, _pending = await asyncio.wait({owner}, timeout=self._config.stop_timeout_seconds)
        if owner in done:
            with contextlib.suppress(BaseException):
                owner.result()
            if self._owner_task is owner:
                self._owner_task = None
                self._stop_requested = None

    async def stop(self) -> None:
        """Shut the child down, bounded by ``stop_timeout_seconds`` (#212).

        A cross-task-safe REQUEST to the owner task (#484): ``stop`` sets
        :attr:`_stop_requested` and awaits the owner, which exits the session's
        context stack IN ITS OWN task.  Because the stack is never entered or
        exited from ``stop``'s (possibly different) task, a between-roast respawn
        driven from a request handler can no longer trip "exit cancel scope in a
        different task" — the bug #484 fixes.

        Graceful teardown (the owner's ``AsyncExitStack.aclose`` → the SDK's
        stdin-close → SIGTERM → SIGKILL sequence) can stall forever on a wedged
        native child (blocked PortAudio read) or a task group still awaiting an
        open pipe. A hung shutdown drives the operator to ``kill -9`` — the one
        uncatchable path that leaves the roaster commanded-hot — so this method
        NEVER blocks this stop call past the bound and never re-raises; #667 owns
        top-level exit when a retained task suppresses cancellation indefinitely.

        On a clean stop within the bound, ``stop_unconfirmed`` is left ``False``
        (the clean path never sets it) and the force-terminate hook is not invoked — so a
        ``start → stop`` cycle that confirms cleanly always reports
        ``stop_unconfirmed is False``, even after a previous run's stop went
        unconfirmed. **Any UNCERTAIN teardown fails closed identically**: both a
        timeout (the owner overran the bound) and a *raising* ``aclose`` (the
        owner's ``stack.aclose`` re-raised — e.g. ``BrokenResourceError`` /
        ``ClosedResourceError`` after a child segfault broke the stdio pipes,
        roast 2) mean we could NOT confirm the child stopped cleanly, so both
        force-kill the child process group and set ``stop_unconfirmed = True``.
        That keeps the #431 respawn guard and restart→recovery honest: a stop we
        could not confirm must never masquerade as clean. The stop call still
        stays bounded and never re-raises; #667 owns the top-level exit policy.
        """
        owner = self._owner_task
        if owner is None:
            return
        stop_deadline = asyncio.get_running_loop().time() + self._config.stop_timeout_seconds
        stop_requested = self._stop_requested
        try:
            # Ask the owner to exit its context stack (in its own task) and wait
            # for it, bounded. ``asyncio.wait`` observes completion without
            # propagating the owner's stored CancelledError into this task, so an
            # owner cancelled during an earlier bounded reap cannot masquerade as
            # cancellation of this stop() caller.
            if stop_requested is not None:
                stop_requested.set()
            done, _pending = await asyncio.wait({owner}, timeout=self._config.stop_timeout_seconds)
            if owner in done:
                # _run_session fail-closes every cancelled/raising outcome before
                # task completion. A retained completed owner is therefore drain
                # only: its PID hook may already be stale and must not be signalled.
                with contextlib.suppress(BaseException):
                    owner.result()
            else:
                # The owner overran the bound (a wedged native child / open pipe).
                if self._owner_task is owner and not self._stop_unconfirmed:
                    self._fail_closed_teardown(
                        "MCP child did not confirm clean stop within "
                        f"{self._config.stop_timeout_seconds:.1f}s"
                    )
                    # Cancel once: a retry must not inject a second cancellation
                    # into an owner that is already unwinding the first one.
                    owner.cancel()
                # Force-terminate has killed the child, so the aclose it was
                # blocked on should unwind. Bounded reap within the original
                # deadline; even a failed kill cannot unbound this stop call.
                remaining = max(0.0, stop_deadline - asyncio.get_running_loop().time())
                done, _pending = await asyncio.wait({owner}, timeout=remaining)
                if owner in done:
                    with contextlib.suppress(BaseException):
                        owner.result()
        except asyncio.CancelledError:
            # stop()'s OWN task was cancelled mid-wait (Codex #492-3). A
            # cancellation is a BaseException, so it would bypass the handlers
            # below and the finally would wipe our state with the child possibly
            # still alive and NO fail-closed marking — a stop we could not confirm
            # silently recorded as clean. Mark unconfirmed + force-kill first,
            # then cancel and bounded-reap the owner. If it resists cancellation,
            # the finally block retains it so start() cannot create a competing
            # owner. RE-RAISE afterwards: cancellation must always propagate,
            # never be swallowed.
            if self._owner_task is owner and not owner.done() and not self._stop_unconfirmed:
                self._fail_closed_teardown("MCP child stop was cancelled mid-teardown")
                owner.cancel()
            remaining = max(0.0, stop_deadline - asyncio.get_running_loop().time())
            with contextlib.suppress(BaseException):
                done, _pending = await asyncio.wait({owner}, timeout=remaining)
                if owner in done:
                    owner.result()
            raise
        finally:
            # A replacement can start after this owner finishes but before this
            # stop task resumes. Only the stop that still owns the current
            # generation may clear its session or rendered config.
            if self._owner_task is owner:
                if owner.done():
                    self._owner_task = None
                    self._stop_requested = None
                self._session = None
                # Clean up the rendered yaml temp dir (D78-4, #420); best-effort —
                # a leftover temp dir is harmless, never blocks shutdown.
                if self._rendered_yaml_dir is not None:
                    shutil.rmtree(self._rendered_yaml_dir, ignore_errors=True)
                    self._rendered_yaml_dir = None

    def _fail_closed_teardown(self, reason: str) -> None:
        """Mark an unconfirmed stop and force-kill the child group (#484 MEDIUM).

        Used by the owner task's immediate unexpected-exit path and by bounded
        startup/shutdown cleanup. Whenever the child's state is unknown, fail
        closed: set ``stop_unconfirmed`` (which gates the #431 respawn guard and
        drives restart→``operator_recovery_required``) and best-effort
        force-kill the current process group. Never raises: a buggy
        force-terminate hook is logged and swallowed, because this runs on a
        shutdown path where cleanup failures must not mask the primary error.

        Args:
            reason: Human-readable cause, logged at ERROR for post-roast diagnosis.
        """
        if not self._stop_unconfirmed or self._teardown_incident_id is None:
            self._teardown_incident_id = secrets.token_hex(16)
        self._stop_unconfirmed = True
        _log.error(
            "%s — force-terminating; restart will enter operator_recovery_required",
            reason,
        )
        if self._force_terminate is not None:
            try:
                self._force_terminate()
            except Exception as ft_exc:
                _log.error("force-terminate hook raised unexpectedly: %s", ft_exc)
        else:  # pragma: no cover - defensive: pid never captured
            _log.error(
                "no force-terminate hook registered for wedged MCP child — "
                "child may survive agent exit"
            )

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
