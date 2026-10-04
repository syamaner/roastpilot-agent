"""Pure temperature conformance: archived policy 3 and current D210 policy 4 over V6.

``check_temperature_conformance`` decides whether one retained V6 run is internally
conformant temperature-screened cold-characterisation evidence.  It performs no
file, network, process or clock access, holds no actuator, transport or provider
surface, and never logs, formats or renders a caller value.
``TEMPERATURE_SCREENED_CONFORMANT`` means internal evidence conformance only: it is
not qualification, readiness, calibration, liveness, hardware, physical-safety or
provenance evidence, and it attests no installed bytes.

The historical entry point pins 1800-second phases; the current entry point pins
600-second phases through shared internal checks without historical success results.
Both require the same strict admission, temperature and provenance rules.

Historical policy 2 is preserved by composition, never copied, and policy 1 is reached only
through it: after hostile-carrier admission and re-admission of every new record, an
exact V3 projection of the admitted slots is judged by the unmodified advisory
policy 2, and only its fully determined conformant result is accepted.  That
internal projection and that result are never returned or exposed; every public
result is policy version 3.  Nothing reads the inner retained run, its headers,
streams or lifecycle before policy 2 has accepted it.  A run that retains a
failed-run terminal skips policy 2 and every later stage.

Admission order: exact class by identity; raw state read without attribute hooks;
an exact ``dict`` with the field count checked before any key scan; every key an
exact ``str`` before any lookup; empty extras; slots by exact type or member
identity; then the cross-slot invariants by identity, ``len`` and ``type()`` only.
No caller ``__eq__``, ``__hash__``, ``__repr__`` or coercion runs during admission.

Pairing compares run id, identity digest and recorded time by ``str`` equality,
phase by identity, tick by ``int`` equality, and only the monotonic seconds by exact
``float`` equality (signed zeros compare equal).  This is numeric and textual
consistency, not bitwise identity and not proof that the tick and its temperature
came from one observation; same-observation origin is a construction obligation of
the writer's caller.  The screen is replayed per phase from no previous snapshot,
carrying each snapshot across the 60-second boundary.

Residuals: an independent operator emergency stop is required.  The candidate
record is an operator assertion; digest equality is not authenticity.  The caller
owns the carrier graph and must not mutate it concurrently during this synchronous
call; no immunity to races outside that contract is claimed.
"""

import enum
import typing

import pydantic

from roastpilot_agent.cold_characterisation.advisory_conformance import (
    ADVISORY_CONFORMANCE_POLICY_VERSION,
    ColdAdvisoryConformanceOutcome,
    ColdAdvisoryConformanceResult,
    check_advisory_conformance,
)
from roastpilot_agent.cold_characterisation.advisory_conformance import (
    _evaluate as _evaluate_advisory,  # pyright: ignore[reportPrivateUsage]
)
from roastpilot_agent.cold_characterisation.duration_policy import ColdDurationGeneration
from roastpilot_agent.cold_characterisation.evidence_advisory import (
    ColdAdvisoryAttemptEvidenceState,
    ColdAdvisoryIntentRecord,
    ColdAdvisoryResolutionRecord,
)
from roastpilot_agent.cold_characterisation.evidence_lifecycle import (
    ColdLifecycleEvent,
    ColdLifecycleEvidenceState,
    validate_lifecycle_record,
)
from roastpilot_agent.cold_characterisation.evidence_reader import (
    ColdRetainedRun,
    ColdRetainedRunV3,
    ColdRetainedRunV6,
)
from roastpilot_agent.cold_characterisation.evidence_schema import (
    ColdEvidenceError,
    ColdEvidenceStream,
    ColdPhaseKind,
    ColdTickRecord,
    validate_record,
)
from roastpilot_agent.cold_characterisation.evidence_store import (
    ColdBindingState,
    ColdEvidenceStoreError,
    ColdRetainedIdentityV1,
    check_record_binding,
    check_temperature_run_binding,
    check_tick_temperature_binding,
)
from roastpilot_agent.cold_characterisation.evidence_temperature import (
    ColdTickTemperatureError,
    ColdTickTemperatureEvidenceState,
    ColdTickTemperatureRecord,
    check_tick_temperature_pairing,
    validate_tick_temperature_record,
)
from roastpilot_agent.cold_characterisation.evidence_temperature_run import (
    ColdMcpCandidateRecord,
    ColdTemperatureAbortRecord,
    validate_mcp_candidate_record,
    validate_temperature_abort_record,
)
from roastpilot_agent.cold_characterisation.evidence_terminal import (
    ColdFailedRunTerminalEvidenceState,
    ColdFailedRunTerminalRecord,
)
from roastpilot_agent.cold_characterisation.temperature_screen import evaluate_temperature

__all__ = (
    "TEMPERATURE_CONFORMANCE_POLICY_VERSION",
    "ColdTemperatureConformanceFinding",
    "ColdTemperatureConformanceOutcome",
    "ColdTemperatureConformanceResult",
    "check_temperature_conformance",
    "CURRENT_CONFORMANCE_POLICY_VERSION",
    "ColdCurrentConformanceResult",
    "check_current_conformance",
)

CURRENT_CONFORMANCE_POLICY_VERSION: typing.Final = 4

TEMPERATURE_CONFORMANCE_POLICY_VERSION: typing.Final = 3
"""Temperature conformance policy 3.

Distinct from pre-advisory conformance policy 1 and advisory conformance policy 2,
which this policy composes.  The only extension axis is a later version of this
constant.
"""


class ColdTemperatureConformanceOutcome(enum.Enum):
    """The closed outcome; there is no qualification, readiness or hardware value."""

    TEMPERATURE_SCREENED_CONFORMANT = "temperature_screened_conformant"
    NOT_CONFORMANT = "not_conformant"


class ColdTemperatureConformanceFinding(enum.Enum):
    """Closed findings; a result lists them uniquely in declaration order."""

    CARRIER_NOT_ADMITTED = "carrier_not_admitted"
    RECORD_NOT_READMITTED = "record_not_readmitted"
    TICK_TEMPERATURE_ABSENT = "tick_temperature_absent"
    TEMPERATURE_ABORT_RECORDED = "temperature_abort_recorded"
    FAILED_RUN_TERMINAL_PRESENT = "failed_run_terminal_present"
    MCP_CANDIDATE_MISSING = "mcp_candidate_missing"
    MCP_CANDIDATE_NOT_UNIQUE = "mcp_candidate_not_unique"
    MCP_CANDIDATE_INCONSISTENT = "mcp_candidate_inconsistent"
    ADVISORY_POLICY_NOT_CONFORMANT = "advisory_policy_not_conformant"
    EVIDENCE_BINDING_REFUSED = "evidence_binding_refused"
    TICK_TEMPERATURE_NOT_PAIRED = "tick_temperature_not_paired"
    MCP_CANDIDATE_VERSION_MISMATCH = "mcp_candidate_version_mismatch"
    ACTIVATION_NOT_BOUND = "activation_not_bound"
    TEMPERATURE_SCREEN_VIOLATED = "temperature_screen_violated"
    CHECKER_INTERNAL_FAILURE = "checker_internal_failure"


_F: typing.TypeAlias = ColdTemperatureConformanceFinding
_T = typing.TypeVar("_T")

_OUTCOME_MEMBERS: typing.Final = tuple(ColdTemperatureConformanceOutcome)
_FINDING_MEMBERS: typing.Final = tuple(ColdTemperatureConformanceFinding)


def _is_member(value: object, members: tuple[enum.Enum, ...]) -> bool:
    """Whether a value is one of the precomputed members, by identity only."""
    return any(value is member for member in members)


def _strictly_declared(items: tuple[object, ...], order: tuple[enum.Enum, ...]) -> bool:
    """Whether each item's declaration index strictly exceeds the previous item's."""
    previous = -1
    for item in items:
        position = next((index for index, member in enumerate(order) if item is member), -1)
        if not position > previous:
            return False
        previous = position
    return True


class ColdTemperatureConformanceResult(pydantic.BaseModel):
    """One closed temperature conformance result holding enums and the version only."""

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    policy_version: typing.Literal[3]
    outcome: ColdTemperatureConformanceOutcome
    findings: tuple[ColdTemperatureConformanceFinding, ...]

    @pydantic.field_validator("policy_version", mode="before")
    @classmethod
    def _require_exact_version(cls, value: object) -> object:
        """Refuse ``True``, ``3.0`` or any value that is not the exact int ``3``."""
        if type(value) is int and value == TEMPERATURE_CONFORMANCE_POLICY_VERSION:
            return value
        raise ValueError("policy version must be the exact int 3")

    @pydantic.field_validator("outcome", mode="before")
    @classmethod
    def _require_outcome_member(cls, value: object) -> object:
        """Refuse anything but a real outcome member, by identity."""
        if _is_member(value, _OUTCOME_MEMBERS):
            return value
        raise ValueError("outcome must be a closed member")

    @pydantic.field_validator("findings", mode="before")
    @classmethod
    def _require_finding_members(cls, value: object) -> object:
        """Refuse anything but an exact tuple of real finding members."""
        if type(value) is not tuple:
            raise ValueError("findings must be an exact tuple")
        items = typing.cast(tuple[object, ...], value)
        if all(_is_member(item, _FINDING_MEMBERS) for item in items):
            return items
        raise ValueError("findings must be closed members")

    @pydantic.model_validator(mode="after")
    def _require_closed_agreement(self) -> typing.Self:
        """Require ordered unique findings and outcome agreement."""
        if not _strictly_declared(self.findings, _FINDING_MEMBERS):
            raise ValueError("findings are not unique and ordered")
        conformant = (
            self.outcome is ColdTemperatureConformanceOutcome.TEMPERATURE_SCREENED_CONFORMANT
        )
        if conformant != (self.findings == ()):
            raise ValueError("findings and outcome disagree")
        return self


class ColdCurrentConformanceResult(pydantic.BaseModel):
    """One closed temperature conformance result holding enums and the version only."""

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    policy_version: typing.Literal[4]
    outcome: ColdTemperatureConformanceOutcome
    findings: tuple[ColdTemperatureConformanceFinding, ...]

    @pydantic.field_validator("policy_version", mode="before")
    @classmethod
    def _require_exact_version(cls, value: object) -> object:
        """Refuse ``True``, ``4.0`` or any value that is not the exact int ``4``."""
        if type(value) is int and value == CURRENT_CONFORMANCE_POLICY_VERSION:
            return value
        raise ValueError("policy version must be the exact int 4")

    @pydantic.field_validator("outcome", mode="before")
    @classmethod
    def _require_outcome_member(cls, value: object) -> object:
        """Refuse anything but a real outcome member, by identity."""
        if _is_member(value, _OUTCOME_MEMBERS):
            return value
        raise ValueError("outcome must be a closed member")

    @pydantic.field_validator("findings", mode="before")
    @classmethod
    def _require_finding_members(cls, value: object) -> object:
        """Refuse anything but an exact tuple of real finding members."""
        if type(value) is not tuple:
            raise ValueError("findings must be an exact tuple")
        items = typing.cast(tuple[object, ...], value)
        if all(_is_member(item, _FINDING_MEMBERS) for item in items):
            return items
        raise ValueError("findings must be closed members")

    @pydantic.model_validator(mode="after")
    def _require_closed_agreement(self) -> typing.Self:
        """Require ordered unique findings and outcome agreement."""
        if not _strictly_declared(self.findings, _FINDING_MEMBERS):
            raise ValueError("findings are not unique and ordered")
        conformant = (
            self.outcome is ColdTemperatureConformanceOutcome.TEMPERATURE_SCREENED_CONFORMANT
        )
        if conformant != (self.findings == ()):
            raise ValueError("findings and outcome disagree")
        return self


# ------------------------------------------------------ stage 1: admission

_ABSENT: typing.Final = object()
_V6_FIELDS: typing.Final = (
    "run",
    "lifecycle_state",
    "lifecycle",
    "advisory_attempt_state",
    "advisory_attempts",
    "terminal_state",
    "terminal",
    "tick_temperature_state",
    "tick_temperatures",
    "temperature_aborts",
    "mcp_candidates",
)
_ADVISORY_RESULT_FIELDS: typing.Final = (
    "policy_version",
    "outcome",
    "findings",
    "pre_advisory_findings",
)
_IDENTITY_FIELDS: typing.Final = tuple(ColdRetainedIdentityV1.model_fields)
_MCP_VERSION_KEY: typing.Final = "coffee_roaster_mcp_version"
_LIFECYCLE_STATES: typing.Final = tuple(ColdLifecycleEvidenceState)
_ATTEMPT_STATES: typing.Final = tuple(ColdAdvisoryAttemptEvidenceState)
_TERMINAL_STATES: typing.Final = tuple(ColdFailedRunTerminalEvidenceState)
_TEMPERATURE_STATES: typing.Final = tuple(ColdTickTemperatureEvidenceState)


class _Admitted(typing.NamedTuple):
    """The admitted V6 root slots; legacy contents are admitted later by policy 2 and 1."""

    run: object
    lifecycle_state: ColdLifecycleEvidenceState
    lifecycle: tuple[object, ...]
    attempt_state: ColdAdvisoryAttemptEvidenceState
    attempts: tuple[object, ...]
    terminal: object
    temperatures: tuple[object, ...]
    aborts: tuple[object, ...]
    candidates: tuple[object, ...]


def _shape(node: object, names: tuple[str, ...]) -> tuple[object, ...] | None:
    """Return an identity-found model's declared raw values in order, or ``None``.

    The raw state is read without attribute hooks; an uninitialised instance is
    refused.  The field count is checked before any key is read, every key is an
    exact ``str`` before any lookup, and extras must be absent or an exact empty
    ``dict``.
    """
    try:
        raw: object = object.__getattribute__(node, "__dict__")
        extra: object = object.__getattribute__(node, "__pydantic_extra__")
    except AttributeError:
        return None
    if type(raw) is not dict:
        return None
    data = typing.cast(dict[object, object], raw)
    if len(data) != len(names):
        return None
    if not all(type(key) is str for key in data):
        return None
    if not (
        extra is None
        or (type(extra) is dict and len(typing.cast(dict[object, object], extra)) == 0)
    ):
        return None
    values = tuple(data.get(name, _ABSENT) for name in names)
    if any(value is _ABSENT for value in values):
        return None
    return values


def _attempt_state_holds(state: object, attempts: tuple[object, ...]) -> bool:
    """Whether the attempt state is the one the last attempt's exact class implies."""
    if not attempts:
        return state is ColdAdvisoryAttemptEvidenceState.ABSENT
    last = type(attempts[-1])
    if last is ColdAdvisoryIntentRecord:
        return state is ColdAdvisoryAttemptEvidenceState.OPEN_TAIL
    if last is ColdAdvisoryResolutionRecord:
        return state is ColdAdvisoryAttemptEvidenceState.COMPLETE
    return False


def _admit(run: object) -> _Admitted | None:
    """Admit the exact V6 root, its eleven slots, and the cross-slot invariants."""
    if type(run) is not ColdRetainedRunV6:
        return None
    values = _shape(run, _V6_FIELDS)
    if values is None:
        return None
    (
        retained,
        lifecycle_state,
        lifecycle,
        attempt_state,
        attempts,
        terminal_state,
        terminal,
        temperature_state,
        temperatures,
        aborts,
        candidates,
    ) = values
    if type(retained) is not ColdRetainedRun:
        return None
    if not (
        _is_member(lifecycle_state, _LIFECYCLE_STATES)
        and _is_member(attempt_state, _ATTEMPT_STATES)
        and _is_member(terminal_state, _TERMINAL_STATES)
        and _is_member(temperature_state, _TEMPERATURE_STATES)
    ):
        return None
    if not all(
        type(slot) is tuple for slot in (lifecycle, attempts, temperatures, aborts, candidates)
    ):
        return None
    if terminal is not None and type(terminal) is not ColdFailedRunTerminalRecord:
        return None
    lifecycle_items = typing.cast(tuple[object, ...], lifecycle)
    attempt_items = typing.cast(tuple[object, ...], attempts)
    temperature_items = typing.cast(tuple[object, ...], temperatures)
    abort_items = typing.cast(tuple[object, ...], aborts)
    if (lifecycle_state is ColdLifecycleEvidenceState.ABSENT) != (len(lifecycle_items) == 0):
        return None
    if not _attempt_state_holds(attempt_state, attempt_items):
        return None
    if (terminal_state is ColdFailedRunTerminalEvidenceState.ABSENT) != (terminal is None):
        return None
    if (temperature_state is ColdTickTemperatureEvidenceState.ABSENT) != (
        len(temperature_items) == 0
    ):
        return None
    if len(abort_items) > 0 and temperature_state is not ColdTickTemperatureEvidenceState.PRESENT:
        return None
    return _Admitted(
        retained,
        typing.cast(ColdLifecycleEvidenceState, lifecycle_state),
        lifecycle_items,
        typing.cast(ColdAdvisoryAttemptEvidenceState, attempt_state),
        attempt_items,
        terminal,
        temperature_items,
        abort_items,
        typing.cast(tuple[object, ...], candidates),
    )


# --------------------------------------------- stages 2 and 3: new records only


class _Fresh(typing.NamedTuple):
    """Fresh snapshots of every new record, in carrier order."""

    temperatures: tuple[ColdTickTemperatureRecord, ...]
    aborts: tuple[ColdTemperatureAbortRecord, ...]
    candidates: tuple[ColdMcpCandidateRecord, ...]


def _readmit(admitted: _Admitted) -> _Fresh | None:
    """Re-admit every new record through its public boundary, or return ``None``."""
    try:
        fresh = _Fresh(
            tuple(validate_tick_temperature_record(item) for item in admitted.temperatures),
            tuple(validate_temperature_abort_record(item) for item in admitted.aborts),
            tuple(validate_mcp_candidate_record(item) for item in admitted.candidates),
        )
    except ColdEvidenceError:
        return None
    return fresh


def _record_findings(admitted: _Admitted, fresh: _Fresh, found: set[_F]) -> None:
    """Stage 3: findings from the admitted slots and fresh new records only."""
    if len(admitted.temperatures) == 0:
        found.add(_F.TICK_TEMPERATURE_ABSENT)
    if fresh.aborts:
        found.add(_F.TEMPERATURE_ABORT_RECORDED)
    if admitted.terminal is not None:
        found.add(_F.FAILED_RUN_TERMINAL_PRESENT)
    for phase in ColdPhaseKind:
        count = sum(1 for record in fresh.candidates if record.phase is phase)
        if count == 0:
            found.add(_F.MCP_CANDIDATE_MISSING)
        elif count > 1:
            found.add(_F.MCP_CANDIDATE_NOT_UNIQUE)
    if fresh.candidates:
        first = fresh.candidates[0]
        if any(
            record.candidate != first.candidate or record.run_id != first.run_id
            for record in fresh.candidates[1:]
        ):
            found.add(_F.MCP_CANDIDATE_INCONSISTENT)


# ------------------------------------------------------- stage 4: policy 2


def _project(admitted: _Admitted) -> ColdRetainedRunV3:
    """Build the internal V3 projection from admitted slots only, never by validating them.

    Policy 2 admits this root itself before reading anything; the projection is
    never returned or exposed.
    """
    values: dict[str, typing.Any] = {
        "run": admitted.run,
        "lifecycle_state": admitted.lifecycle_state,
        "lifecycle": admitted.lifecycle,
        "advisory_attempt_state": admitted.attempt_state,
        "advisory_attempts": admitted.attempts,
    }
    return ColdRetainedRunV3.model_construct(**values)


def _advisory_conformant(result: object) -> bool:
    """Accept only policy 2's fully determined conformant result shape.

    The exact class, a declared state of exactly four exact-``str`` keys with empty
    extras, the exact ``int`` version 2, the conformant outcome by identity, and two
    exact empty tuples.  Empty tuples make order, uniqueness and agreement hold.
    """
    if type(result) is not ColdAdvisoryConformanceResult:
        return False
    values = _shape(result, _ADVISORY_RESULT_FIELDS)
    if values is None:
        return False
    version, outcome, findings, pre_findings = values
    return (
        type(version) is int
        and version == ADVISORY_CONFORMANCE_POLICY_VERSION
        and outcome is ColdAdvisoryConformanceOutcome.ADVISORY_CONFORMANT
        and type(findings) is tuple
        and len(typing.cast(tuple[object, ...], findings)) == 0
        and type(pre_findings) is tuple
        and len(typing.cast(tuple[object, ...], pre_findings)) == 0
    )


# ------------------------------------------------- stage 5: accepted legacy reads

_OFF: typing.Final = ColdPhaseKind.RECORDING_OFF
_ON: typing.Final = ColdPhaseKind.RECORDING_ON
#: Lifecycle positions of each phase's activation record (policy 2's anchors).
_ACTIVATIONS: typing.Final = ((_OFF, 0), (_ON, 5))

_Ticks: typing.TypeAlias = dict[ColdPhaseKind, tuple[ColdTickRecord, ...]]


def _bind(retained: ColdRetainedRun, fresh: _Fresh, found: set[_F]) -> ColdBindingState | None:
    """Bind fresh headers, then every new record; return the state, or record a refusal."""
    state = ColdBindingState(retained.run_id)
    try:
        for item in retained.headers:
            check_record_binding(state, validate_record(item.header), writer_root=None)
        for temperature in fresh.temperatures:
            check_tick_temperature_binding(state, temperature)
        for record in (*fresh.candidates, *fresh.aborts):
            check_temperature_run_binding(state, record)
    except (ColdEvidenceStoreError, ColdEvidenceError):
        found.add(_F.EVIDENCE_BINDING_REFUSED)
        return None
    return state


def _fresh_ticks(retained: ColdRetainedRun) -> _Ticks:
    """Return each phase's fresh v1 tick snapshots in file order."""
    ticks: dict[ColdPhaseKind, list[ColdTickRecord]] = {phase: [] for phase in ColdPhaseKind}
    for stream in retained.streams:
        if stream.stream is ColdEvidenceStream.TICK:
            ticks[stream.phase].extend(
                snapshot
                for snapshot in map(validate_record, stream.records)
                if type(snapshot) is ColdTickRecord
            )
    return {phase: tuple(items) for phase, items in ticks.items()}


def _of_phase(
    temperatures: tuple[ColdTickTemperatureRecord, ...], phase: ColdPhaseKind
) -> tuple[ColdTickTemperatureRecord, ...]:
    """Return one phase's temperature snapshots in carrier order."""
    return tuple(record for record in temperatures if record.phase is phase)


def _paired(ticks: _Ticks, fresh: _Fresh, found: set[_F]) -> bool:
    """Whether every phase's ticks pair one-to-one with its temperatures; record if not."""
    try:
        for phase in ColdPhaseKind:
            check_tick_temperature_pairing(ticks[phase], _of_phase(fresh.temperatures, phase))
    except ColdTickTemperatureError:
        found.add(_F.TICK_TEMPERATURE_NOT_PAIRED)
        return False
    return True


def _reported_version(identity: ColdRetainedIdentityV1) -> str | None:
    """Return a fresh identity's exact ``str`` MCP version, read without caller hooks."""
    values = _shape(identity, _IDENTITY_FIELDS)
    if values is None:
        return None
    known = values[_IDENTITY_FIELDS.index("known")]
    if type(known) is not dict:
        return None
    mapping = typing.cast(dict[object, object], known)
    if not all(type(key) is str for key in mapping):
        return None
    version = mapping.get(_MCP_VERSION_KEY)
    return version if type(version) is str else None


def _check_versions(state: ColdBindingState, fresh: _Fresh, found: set[_F]) -> None:
    """Require each phase's single candidate to name its bound identity's MCP version."""
    identities = {header.phase: identity for header, identity in state.headers}
    for phase in ColdPhaseKind:
        records = [record for record in fresh.candidates if record.phase is phase]
        if len(records) != 1:
            continue
        identity = identities.get(phase)
        version = None if identity is None else _reported_version(identity)
        if version is None or version != records[0].candidate.reported_version:
            found.add(_F.MCP_CANDIDATE_VERSION_MISMATCH)


def _activations(
    lifecycle: tuple[object, ...], found: set[_F]
) -> dict[ColdPhaseKind, float] | None:
    """Return each phase's fresh activation instant, or record ``ACTIVATION_NOT_BOUND``."""
    instants: dict[ColdPhaseKind, float] = {}
    for phase, position in _ACTIVATIONS:
        record = (
            validate_lifecycle_record(typing.cast(typing.Any, lifecycle[position]))
            if position < len(lifecycle)
            else None
        )
        if (
            record is None
            or record.event is not ColdLifecycleEvent.PHASE_ACTIVATED
            or record.phase is not phase
        ):
            found.add(_F.ACTIVATION_NOT_BOUND)
            return None
        instants[phase] = record.event_monotonic_seconds
    return instants


def _replay(
    ticks: _Ticks, fresh: _Fresh, activated: dict[ColdPhaseKind, float], found: set[_F]
) -> None:
    """Replay the screen per phase from no previous snapshot over every positional pair."""
    for phase in ColdPhaseKind:
        previous: object = None
        for tick, temperature in zip(
            ticks[phase], _of_phase(fresh.temperatures, phase), strict=True
        ):
            reasons = evaluate_temperature(
                temperature.temperature,
                previous=previous,
                since_activation_seconds=tick.monotonic_seconds - activated[phase],
            )
            if reasons:
                found.add(_F.TEMPERATURE_SCREEN_VIOLATED)
            previous = temperature.temperature


def _guarded(found: set[_F], call: typing.Callable[[], _T]) -> _T | None:
    """Run one rule group; any ``Exception`` records an internal failure and retains nothing."""
    try:
        value: _T | None = call()
    except Exception:
        value = None
        failed = True
    else:
        failed = False
    if failed:
        found.add(_F.CHECKER_INTERNAL_FAILURE)
    return value


def _evaluate(
    run: object, generation: ColdDurationGeneration = ColdDurationGeneration.HISTORICAL
) -> set[_F]:
    """Stages 1 to 5 with their early returns; stage-5 rule groups are individually guarded."""
    admitted = _admit(run)
    if admitted is None:
        return {_F.CARRIER_NOT_ADMITTED}
    fresh = _readmit(admitted)
    if fresh is None:
        return {_F.RECORD_NOT_READMITTED}
    found: set[_F] = set()
    _record_findings(admitted, fresh, found)
    if admitted.terminal is not None:
        return found
    advisory_ok = (
        _advisory_conformant(check_advisory_conformance(_project(admitted)))
        if generation is ColdDurationGeneration.HISTORICAL
        else _evaluate_advisory(_project(admitted), generation) == (set(), ())
    )
    if not advisory_ok:
        found.add(_F.ADVISORY_POLICY_NOT_CONFORMANT)
        return found
    retained = typing.cast(ColdRetainedRun, admitted.run)
    state = _guarded(found, lambda: _bind(retained, fresh, found))
    ticks = _guarded(found, lambda: _fresh_ticks(retained))
    paired = ticks is not None and _guarded(found, lambda: _paired(ticks, fresh, found)) is True
    if state is not None:
        bound = state
        _guarded(found, lambda: _check_versions(bound, fresh, found))
    activated = _guarded(found, lambda: _activations(admitted.lifecycle, found))
    if state is not None and paired and ticks is not None and activated is not None:
        replay_ticks, instants = ticks, activated
        _guarded(found, lambda: _replay(replay_ticks, fresh, instants, found))
    return found


def check_temperature_conformance(run: object) -> ColdTemperatureConformanceResult:
    """Check one retained V6 run against temperature conformance policy 3.

    The run conforms if and only if every new record re-admits, tick temperatures
    are present and pair with the ticks, no temperature abort or failed-run terminal
    is retained, each phase carries exactly one consistent MCP candidate naming its
    bound identity's MCP version, policy 2 conforms on the exact V3 projection, both
    activations are bound, and the per-phase screen replay records no reason.  A
    refusal or internal exception never conforms; a ``BaseException`` that is not an
    ``Exception`` propagates.  The result carries enums and the version only.

    Args:
        run: A candidate ``ColdRetainedRunV6``, as the strict V6 reader returns it.

    Returns:
        The closed temperature conformance result.
    """
    evaluated: set[_F] | None = None
    try:
        evaluated = _evaluate(run)
    except Exception:
        evaluated = None
    found = {_F.CHECKER_INTERNAL_FAILURE} if evaluated is None else evaluated
    findings = tuple(member for member in _FINDING_MEMBERS if member in found)
    return ColdTemperatureConformanceResult(
        policy_version=TEMPERATURE_CONFORMANCE_POLICY_VERSION,
        outcome=(
            ColdTemperatureConformanceOutcome.NOT_CONFORMANT
            if findings
            else ColdTemperatureConformanceOutcome.TEMPERATURE_SCREENED_CONFORMANT
        ),
        findings=findings,
    )


def check_current_conformance(run: object) -> ColdCurrentConformanceResult:
    """Check retained V6 evidence against current D210 policy 4.

    Args:
        run: Candidate retained V6 run, admitted before semantic use.

    Returns:
        Strict policy-4 result; software conformance only.
    """
    try:
        found = _evaluate(run, ColdDurationGeneration.D210)
    except Exception:
        found = {_F.CHECKER_INTERNAL_FAILURE}
    findings = tuple(member for member in _FINDING_MEMBERS if member in found)
    return ColdCurrentConformanceResult(
        policy_version=CURRENT_CONFORMANCE_POLICY_VERSION,
        outcome=(
            ColdTemperatureConformanceOutcome.NOT_CONFORMANT
            if findings
            else ColdTemperatureConformanceOutcome.TEMPERATURE_SCREENED_CONFORMANT
        ),
        findings=findings,
    )
