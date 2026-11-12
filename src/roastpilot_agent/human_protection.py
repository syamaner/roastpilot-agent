"""D230 human-mode protection rules and run-local evidence types."""

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TypeGuard

from roastpilot_agent.models import RoastPhase

HUMAN_PROTECTION_BT_C = 196.0
HUMAN_CHARGED_ROAST_PHASES = frozenset(
    {RoastPhase.ROASTING_PRE_FIRST_CRACK, RoastPhase.DEVELOPMENT}
)


class ControlAuthority(Enum):
    """Source of roast control authority."""

    AUTOMATIC = "automatic"
    HUMAN = "human"


class WriteClass(Enum):
    """Class of a routine write for the human-mode admission fence."""

    HEAT = "heat"
    FAN = "fan"
    DROP = "drop"
    CONTAINMENT = "containment"
    SESSION_START = "session_start"
    NON_HEAT = "non_heat"


class ReadingFreshness(Enum):
    """Freshness proved by accepted temperature observations."""

    FRESH = "fresh"
    STALE = "stale"
    UNPROVEN = "unproven"
    INVALID = "invalid"


class CounterClass(Enum):
    """Validity of the accepted-reading counter inputs."""

    PRESENT = "present"
    ABSENT = "absent"
    INVALID = "invalid"


@dataclass(frozen=True)
class CounterEvidence:
    """Validated status and ignored counts, when both are present."""

    status: int | None
    ignored: int | None
    class_: CounterClass


def extract_accepted_counters(raw: Mapping[str, object] | None) -> CounterEvidence:
    """Extract exact, nonnegative counters with ignored no greater than status.

    Args:
        raw: Vendor data from the current child, if present.

    Returns:
        Classified counter evidence with absent or invalid counts left unset.
    """
    status_key = "status_packet_count"
    ignored_key = "ignored_temperature_packet_count"
    if raw is None or status_key not in raw or ignored_key not in raw:
        return CounterEvidence(None, None, CounterClass.ABSENT)
    status, ignored = raw[status_key], raw[ignored_key]
    if type(status) is not int or type(ignored) is not int:
        return CounterEvidence(None, None, CounterClass.INVALID)
    if status < 0 or ignored < 0 or ignored > status:
        return CounterEvidence(None, None, CounterClass.INVALID)
    return CounterEvidence(status, ignored, CounterClass.PRESENT)


def _is_finite_temperature(value: object) -> TypeGuard[int | float]:
    # Exact ints are finite without conversion to float, which can overflow.
    return type(value) is int or (type(value) is float and math.isfinite(value))


@dataclass(frozen=True)
class _Observation:
    epoch: int
    status: int
    ignored: int
    admitted_at: float

    @property
    def accepted(self) -> int:
        return self.status - self.ignored


class AcceptedReadingTracker:
    """Prove finite accepted-reading progress from one child's ordered history.

    Both times use the caller's run-local monotonic clock. The lower bound for
    any new accepted reading is the previous valid request's admission time.
    """

    def __init__(self, clock: Callable[[], float], max_age_s: float) -> None:
        self._clock = clock
        self._max_age_s = max_age_s
        self._previous: _Observation | None = None
        self._acceptance_bound: float | None = None
        self._without_bound = ReadingFreshness.UNPROVEN

    def observe(
        self,
        *,
        child_epoch: int,
        counters: CounterEvidence,
        bean_temp_c: object,
        env_temp_c: object,
        request_admitted_at: float,
    ) -> ReadingFreshness:
        """Update continuity from one completed telemetry request.

        Args:
            child_epoch: Generation of the child that supplied the observation.
            counters: Accepted-reading counter evidence for this response.
            bean_temp_c: Canonical bean temperature in Celsius.
            env_temp_c: Canonical environment temperature in Celsius.
            request_admitted_at: Run-local monotonic time before the request.

        Returns:
            Freshness evaluated when processing the response.
        """
        if counters.class_ is CounterClass.ABSENT:
            return self._clear(ReadingFreshness.UNPROVEN)
        if (
            counters.class_ is CounterClass.INVALID
            or counters.status is None
            or counters.ignored is None
            or not _is_finite_temperature(bean_temp_c)
            or not _is_finite_temperature(env_temp_c)
            or not math.isfinite(request_admitted_at)
        ):
            return self._clear(ReadingFreshness.INVALID)
        current = _Observation(child_epoch, counters.status, counters.ignored, request_admitted_at)
        previous = self._previous
        if previous is not None:
            # Status is accepted + ignored, so a status decrease also
            # decreases at least one of the two counts checked here.
            if (
                current.epoch != previous.epoch
                or current.ignored < previous.ignored
                or current.accepted < previous.accepted
                or current.admitted_at <= previous.admitted_at
            ):
                return self._clear(ReadingFreshness.INVALID)
            if current.admitted_at - previous.admitted_at > self._max_age_s:
                self._acceptance_bound = None
            elif current.accepted > previous.accepted:
                self._acceptance_bound = previous.admitted_at
        self._previous = current
        self._without_bound = ReadingFreshness.UNPROVEN
        return self.recheck()

    def record_read_failure(self) -> ReadingFreshness:
        """Break continuity on a failed read and return INVALID evidence."""
        return self._clear(ReadingFreshness.INVALID)

    def recheck(self) -> ReadingFreshness:
        """Evaluate the retained evidence at the current monotonic time."""
        previous = self._previous
        if previous is None:
            return self._without_bound
        now = self._clock()
        # The newest admission is at least as recent as any retained bound.
        if not math.isfinite(now) or now < previous.admitted_at:
            return self._clear(ReadingFreshness.INVALID)
        if self._acceptance_bound is None:
            return self._without_bound
        if now - self._acceptance_bound <= self._max_age_s:
            return ReadingFreshness.FRESH
        return ReadingFreshness.STALE

    def _clear(self, freshness: ReadingFreshness) -> ReadingFreshness:
        self._previous = None
        self._acceptance_bound = None
        self._without_bound = freshness
        return freshness


class EndpointDisposition(Enum):
    """Run-local knowledge of the drop endpoint."""

    NONE = "none"
    ADMITTED = "admitted"
    IN_FLIGHT = "in_flight"
    ISSUED = "issued"
    UNKNOWN = "unknown"
    WITHHELD = "withheld"
    HISTORY_UNKNOWN = "history_unknown"


class EndpointOwner(Enum):
    """Authority that admitted the drop endpoint."""

    PROTECTION = "protection"
    OPERATOR = "operator"
    OPERATOR_RECOVERY = "operator_recovery"
    FAULT_CONTROL = "fault_control"


class CommandDisposition(Enum):
    """Whether prior command ownership is settled for this run."""

    SETTLED = "settled"
    UNKNOWN = "unknown"


class ContainmentOutcome(Enum):
    """Classified outcomes of the fixed-zero containment capability."""

    REFUSED_IDENTITY = "refused_identity"
    REFUSED_CAPABILITY = "refused_capability"
    REFUSED_BUSY = "refused_busy"
    COMMAND_STATE_RETURNED = "command_state_returned"
    UNKNOWN_DRIVER_RESULT = "unknown_driver_result"
    UNKNOWN_COMPLETION = "unknown_completion"
    UNKNOWN_TRANSPORT = "unknown_transport"
    UNKNOWN_MALFORMED_RESPONSE = "unknown_malformed_response"
    NOT_ATTEMPTED = "not_attempted"

    @property
    def is_unknown(self) -> bool:
        """Return whether the operation has an unknown outcome."""
        return self in {
            ContainmentOutcome.UNKNOWN_DRIVER_RESULT,
            ContainmentOutcome.UNKNOWN_COMPLETION,
            ContainmentOutcome.UNKNOWN_TRANSPORT,
            ContainmentOutcome.UNKNOWN_MALFORMED_RESPONSE,
        }


class ContainmentReason(Enum):
    """Closed fixed-zero/v1 wire reason codes."""

    UNKNOWN_SESSION = "unknown_session"
    NOT_LATEST_SESSION = "not_latest_session"
    SESSION_NOT_ACTIVE = "session_not_active"
    SESSION_FAULTED = "session_faulted"
    CAPABILITY_ABSENT = "capability_absent"
    CAPABILITY_VERSION_UNSUPPORTED = "capability_version_unsupported"
    CAPABILITY_MALFORMED = "capability_malformed"
    CAPABILITY_UNREADABLE = "capability_unreadable"
    CAPABILITY_PORT_MISSING = "capability_port_missing"
    FINALISATION_IN_PROGRESS = "finalisation_in_progress"
    COMMAND_IN_PROGRESS = "command_in_progress"
    DRIVER_RAISED = "driver_raised"
    DRIVER_RESULT_MISSING = "driver_result_missing"
    DRIVER_RESULT_INVALID = "driver_result_invalid"
    COMPLETION_OWNERSHIP_CHANGED = "completion_ownership_changed"
    PROJECTION_UNCONFIRMED = "projection_unconfirmed"


@dataclass(frozen=True)
class ContainmentResult:
    """Classified containment evidence, independent of MCP and physical state."""

    outcome: ContainmentOutcome
    reason: ContainmentReason | None = None
    evidence_heat_zero_commanded: bool | None = None
    evidence_is_pre_fallback: bool = False


class ToolDiscovery(Enum):
    """Whether the current child advertises the fixed-zero wire contract."""

    DISCOVERED = "discovered"
    ABSENT = "absent"


class ContainmentAttempt(Enum):
    """Run-local one-attempt containment budget."""

    AVAILABLE = "available"
    CONSUMED = "consumed"


def protection_eligible(
    *,
    authority: ControlAuthority,
    charged: bool,
    phase: RoastPhase,
    freshness: ReadingFreshness,
    bean_temp_c: object,
    endpoint: EndpointDisposition,
    command: CommandDisposition,
) -> bool:
    """Evaluate the closed eligibility rule for the D230 protective drop.

    Args:
        authority: Authority selected for the run.
        charged: Whether charge is confirmed.
        phase: Current effective control phase.
        freshness: Freshness of the accepted reading.
        bean_temp_c: Canonical bean temperature in Celsius.
        endpoint: Current drop disposition.
        command: Current command disposition.

    Returns:
        Whether the protective drop is eligible for safety admission.
    """
    return (
        authority is ControlAuthority.HUMAN
        and charged
        and phase in HUMAN_CHARGED_ROAST_PHASES
        and freshness is ReadingFreshness.FRESH
        and _is_finite_temperature(bean_temp_c)
        and bean_temp_c >= HUMAN_PROTECTION_BT_C
        and endpoint is EndpointDisposition.NONE
        and command is CommandDisposition.SETTLED
    )
