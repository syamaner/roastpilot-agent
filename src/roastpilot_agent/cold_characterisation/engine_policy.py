"""Pure single-tick policy for the non-actuating cold observation engine.

The policy reads only one validated, already retained ``ColdTickRecord``, so the
decision and the durable evidence are the same data.  It performs no I/O, reads
no clock, and returns closed abort reasons only; it never computes an outcome.

Commanded device values are commanded state, never physical sensing.  The
observed main-fan command (``device.fan_level_percent``, D197) is rejected when
non-zero, which is not continuous main-fan, drum, or solenoid/drop assurance;
those remain D195 finalisation only.  The session clock is an MCP software
heartbeat, not serial, driver-sample, or physical freshness.

A null device is unknown commanded state (heat, cooling, connection and driver
are all unknown), so it aborts immediately as ``MCP_RESPONSE_NOT_ADMITTED`` and
is never an absent-telemetry form.  Temperatures are checked only for presence;
no numeric plausibility range is applied, and finiteness alone is not claimed
to satisfy D187 plausibility, which remains an open hardware-readiness item.
"""

import typing

import pydantic

from roastpilot_agent.cold_characterisation.duration_policy import CURRENT_PHASE_SECONDS
from roastpilot_agent.cold_characterisation.evidence_schema import (
    ColdEngineAbortReason,
    ColdTickRecord,
    ColdTickRoastFanOutcome,
    ColdTickSessionPhase,
)

#: Fixed D194 observation tick; never configurable and never shortened for tests.
COLD_OBSERVATION_INTERVAL_SECONDS: typing.Final = 1.0
#: Fixed D197 startup interval during which only absent telemetry is tolerated.
COLD_STARTUP_TELEMETRY_DEADLINE_SECONDS: typing.Final = 60.0
#: Fixed length of one observed phase, measured from the admitted activation instant.
COLD_PHASE_OBSERVATION_SECONDS: typing.Final = CURRENT_PHASE_SECONDS


class ColdTickDecision(pydantic.BaseModel):
    """Closed result of evaluating one retained tick; carries no verdict or outcome.

    Attributes:
        reasons: Triggered abort reasons in ``ColdEngineAbortReason`` declaration order.
        next_previous_elapsed_seconds: The retained session heartbeat, supplied as
            ``previous_elapsed`` on the next evaluation.
    """

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    reasons: tuple[ColdEngineAbortReason, ...]
    next_previous_elapsed_seconds: float


def evaluate_tick(
    record: ColdTickRecord,
    *,
    established_session_id: str,
    frozen_driver: str,
    since_activation_seconds: float,
    previous_elapsed: float | None,
) -> ColdTickDecision:
    """Evaluate one retained tick against the fixed cold observation rules.

    Unknown, unsafe, and non-zero commanded values abort immediately with no
    startup grace (D187, D197).  Only absent telemetry is tolerated, and only
    while strictly less than the startup deadline has elapsed since activation.
    A single non-advancing session heartbeat aborts from the second read on.

    Args:
        record: The validated tick record exactly as retained.
        established_session_id: The cold session the client established.
        frozen_driver: The roaster driver frozen into the phase identity.
        since_activation_seconds: Admitted seconds since the activation instant.
        previous_elapsed: The previous tick's heartbeat, or ``None`` on the first read.

    Returns:
        The closed decision for this tick.
    """
    retained_session = record.session
    active = retained_session.active
    elapsed = retained_session.elapsed_monotonic_seconds
    device = record.device
    roast_fan = record.roast_fan
    audio = record.audio
    triggered: set[ColdEngineAbortReason] = set()
    absent = False
    if device is None:
        # Unknown commanded state: no startup grace (D187, D197).
        triggered.add(ColdEngineAbortReason.MCP_RESPONSE_NOT_ADMITTED)
    else:
        if (
            device.heat_level_percent != 0
            or device.cooling_on is True
            or device.fan_level_percent != 0
        ):
            triggered.add(ColdEngineAbortReason.COMMAND_STATE_NON_ZERO)
        if device.connected is False:
            triggered.add(ColdEngineAbortReason.DEVICE_DISCONNECTED)
        if device.driver != frozen_driver:
            triggered.add(ColdEngineAbortReason.DRIVER_IDENTITY_MISMATCH)
        if device.bean_temp_c is None or device.env_temp_c is None:
            absent = True
    if roast_fan.outcome is ColdTickRoastFanOutcome.OBSERVED:
        if roast_fan.roast_fan_level_percent != 0:
            triggered.add(ColdEngineAbortReason.COMMAND_STATE_NON_ZERO)
    else:
        # Every other closed outcome is unknown commanded state, never absence.
        triggered.add(ColdEngineAbortReason.ROAST_FAN_NOT_OBSERVABLE)
    if active is False:
        triggered.add(ColdEngineAbortReason.SESSION_INACTIVE)
    if retained_session.phase is not ColdTickSessionPhase.ROASTING:
        triggered.add(ColdEngineAbortReason.SESSION_PHASE_CHANGED)
    if (
        audio.status == "detected"
        or audio.detected_at_utc is not None
        or audio.detected_monotonic_seconds is not None
    ):
        triggered.add(ColdEngineAbortReason.FIRST_CRACK_CONFIRMED)
    if audio.mode != "audio" or audio.status == "faulted" or audio.status == "unavailable":
        triggered.add(ColdEngineAbortReason.INFERENCE_NOT_ACTIVE)
    if previous_elapsed is not None and elapsed <= previous_elapsed:
        triggered.add(ColdEngineAbortReason.SESSION_CLOCK_STALLED)
    if retained_session.session_id != established_session_id:
        triggered.add(ColdEngineAbortReason.MCP_SESSION_IDENTITY_CHANGED)
    if audio.status == "disabled" or audio.status == "manual" or audio.audio_running is False:
        absent = True
    if absent and since_activation_seconds >= COLD_STARTUP_TELEMETRY_DEADLINE_SECONDS:
        triggered.add(ColdEngineAbortReason.TELEMETRY_ABSENT_AFTER_STARTUP)
    return ColdTickDecision(
        reasons=tuple(reason for reason in ColdEngineAbortReason if reason in triggered),
        next_previous_elapsed_seconds=elapsed,
    )
