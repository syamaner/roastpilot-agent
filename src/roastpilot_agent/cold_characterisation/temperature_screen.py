"""Pure D209 cold temperature screen over admitted version-1 temperature projections.

``evaluate_temperature`` returns the closed screen reasons for one observation,
given the previous observation the caller holds and the admitted seconds since
the phase's activation.  It is stateless and total: it performs no I/O, reads no
clock, never logs, formats or renders a caller value, and catches no
``BaseException``.  Every projection is re-admitted by content before use.

Meaning, honestly stated:

* ``()`` means only "no screen reason".  It is never qualification, readiness or
  physical evidence.
* Before the 60-second startup boundary, ``AWAITING_FIRST_PACKET`` with no counter
  regression returns ``()``: ordinary startup absence.  ``UNSUPPORTED`` and
  ``NOT_ELIGIBLE`` mean observability is unknown and return ``NOT_OBSERVABLE`` at
  any time; ``MALFORMED`` returns ``PROJECTION_MALFORMED`` at any time.  The
  outcome is branched on before ``last_packet_valid`` is read.
* A decline of any counter, or of the derived accepted count (status packets minus
  ignored-temperature packets), is an invalid counter history and returns
  ``COUNTER_REGRESSED`` before and after the boundary.  No per-session counter
  reset is assumed: the caller resets its previous snapshot per phase.
* At and after the boundary the first eligible observation compares against
  whatever previous snapshot the caller passes, including one taken before the
  boundary; a missing previous snapshot returns ``PRIOR_MISSING``.
* Packet progress means at least one accepted status packet arrived between the
  two observations.  It is not a maximum sample age, a one-second bound or a
  watchdog: a stalled observation clock produces no call at all, and so no reason.
* Raw/typed agreement is consistency only, not independent sensor corroboration,
  and ``observed`` does not imply liveness.
* The 5.0 to 40.0 °C range is inclusive engineering screening of both bean and
  environment temperatures.  It is not calibration and not a physical-safety
  statement.
* Requiring ``last_packet_valid`` alongside the Celsius unit is redundant on admitted
  data (the projection's shape rules tie validity to the reported unit); it is kept
  as defence in depth.

Bad-checksum frames that the device driver skips without counting them remain a
disclosed residual: they are invisible to every counter here.
"""

import math
import typing

from roastpilot_agent.cold_characterisation.engine_policy import (
    COLD_STARTUP_TELEMETRY_DEADLINE_SECONDS,
)
from roastpilot_agent.cold_characterisation.evidence_temperature_run import (
    ColdTemperatureScreenReason,
)
from roastpilot_agent.cold_characterisation.temperature_projection import (
    ColdTemperatureAgreement,
    ColdTemperatureOutcome,
    ColdTemperatureReportedUnit,
    ColdTickTemperatureProjection,
    readmit_cold_temperature_projection,
)

#: The inclusive lower bound of the engineering temperature screen, in Celsius.
COLD_TEMPERATURE_SCREEN_MIN_C: typing.Final = 5.0
#: The inclusive upper bound of the engineering temperature screen, in Celsius.
COLD_TEMPERATURE_SCREEN_MAX_C: typing.Final = 40.0

_R: typing.TypeAlias = ColdTemperatureScreenReason
_NOT_ADMITTED: typing.Final = (_R.SCREEN_INPUT_NOT_ADMITTED,)


class _Counters(typing.NamedTuple):
    """The four counters of one observation, all present."""

    status: int
    ignored: int
    read_errors: int
    loop_errors: int

    @property
    def accepted(self) -> int:
        """Status packets whose temperature was accepted (status minus ignored)."""
        return self.status - self.ignored


def _counters(projection: ColdTickTemperatureProjection) -> _Counters | None:
    """Return an observation's counters, or ``None`` when any counter is absent."""
    status = projection.status_packet_count
    ignored = projection.ignored_temperature_packet_count
    read_errors = projection.status_read_error_count
    loop_errors = projection.command_loop_error_count
    if status is None or ignored is None or read_errors is None or loop_errors is None:
        return None
    return _Counters(status, ignored, read_errors, loop_errors)


def _regressed(current: _Counters, previous: _Counters) -> bool:
    """Whether any counter, or the derived accepted count, declined."""
    return (
        any(now < before for now, before in zip(current, previous, strict=True))
        or current.accepted < previous.accepted
    )


def _within_screen(value: float | None) -> bool:
    """Whether a present temperature lies inside the inclusive screen."""
    return (
        value is not None
        and COLD_TEMPERATURE_SCREEN_MIN_C <= value <= COLD_TEMPERATURE_SCREEN_MAX_C
    )


def _observed_reasons(current: ColdTickTemperatureProjection, found: set[_R]) -> None:
    """Add the unit, validity, agreement and range reasons of one eligible observation."""
    if (
        current.reported_temperature_unit is not ColdTemperatureReportedUnit.CELSIUS
        or current.last_packet_valid is not True
    ):
        found.add(_R.LAST_PACKET_NOT_VALID_CELSIUS)
        return
    if current.value_agreement is not ColdTemperatureAgreement.AGREE:
        found.add(_R.VALUES_DISAGREE)
    if not (
        _within_screen(current.last_packet_bean_temp_c)
        and _within_screen(current.last_packet_env_temp_c)
    ):
        found.add(_R.OUTSIDE_SCREEN)


def _progress_reasons(current: _Counters, previous: _Counters, found: set[_R]) -> None:
    """Add the packet-progress and newly counted fault reasons of one eligible pair."""
    if current.accepted <= previous.accepted:
        found.add(_R.PACKET_NOT_PROGRESSED)
    if (
        current.ignored > previous.ignored
        or current.read_errors > previous.read_errors
        or current.loop_errors > previous.loop_errors
    ):
        found.add(_R.FAULT_COUNTED)


def evaluate_temperature(
    current: object, *, previous: object, since_activation_seconds: object
) -> tuple[ColdTemperatureScreenReason, ...]:
    """Screen one cold temperature observation against the D209 rules.

    Args:
        current: The observation's projection; re-admitted by content.
        previous: The caller's previous projection for this phase, or ``None``.
        since_activation_seconds: Exact finite, non-negative ``float`` seconds since
            the phase's admitted activation instant.

    Returns:
        The unique screen reasons in declaration order; ``(SCREEN_INPUT_NOT_ADMITTED,)``
        alone if any input is not admitted.  ``()`` means only "no screen reason".
    """
    fresh = readmit_cold_temperature_projection(current)
    if type(fresh) is not ColdTickTemperatureProjection:
        return _NOT_ADMITTED
    since = since_activation_seconds
    if type(since) is not float or not math.isfinite(since) or since < 0.0:
        return _NOT_ADMITTED
    prior: ColdTickTemperatureProjection | None = None
    if previous is not None:
        readmitted = readmit_cold_temperature_projection(previous)
        if type(readmitted) is not ColdTickTemperatureProjection:
            return _NOT_ADMITTED
        prior = readmitted
    found: set[_R] = set()
    outcome = fresh.outcome
    if outcome is ColdTemperatureOutcome.MALFORMED:
        found.add(_R.PROJECTION_MALFORMED)
    elif (
        outcome is ColdTemperatureOutcome.UNSUPPORTED
        or outcome is ColdTemperatureOutcome.NOT_ELIGIBLE
    ):
        found.add(_R.NOT_OBSERVABLE)
    now = _counters(fresh)
    before = None if prior is None else _counters(prior)
    if now is not None and before is not None and _regressed(now, before):
        found.add(_R.COUNTER_REGRESSED)
    if since >= COLD_STARTUP_TELEMETRY_DEADLINE_SECONDS:
        if before is None:
            found.add(_R.PRIOR_MISSING)
        if outcome is ColdTemperatureOutcome.AWAITING_FIRST_PACKET:
            found.add(_R.NOT_OBSERVED_AFTER_STARTUP)
        elif outcome is ColdTemperatureOutcome.OBSERVED:
            _observed_reasons(fresh, found)
        if now is not None and before is not None:
            _progress_reasons(now, before, found)
    return tuple(reason for reason in ColdTemperatureScreenReason if reason in found)
