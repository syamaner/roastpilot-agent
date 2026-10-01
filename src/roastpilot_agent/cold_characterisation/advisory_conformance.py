"""Pure, versioned advisory conformance policy 2 over one retained V3 cold run (#954).

``check_advisory_conformance`` decides whether one retained V3 run is internally
conformant advisory cold-characterisation evidence.  It performs no file, network,
process or clock access, holds no actuator, transport or provider surface, and never
logs, formats or renders a caller value.  ``ADVISORY_CONFORMANT`` means internal
evidence conformance only: it is not qualification, readiness, hardware, physical or
provenance evidence.  A hand-built V3 carrier is not manifest verification, and
re-admitting a container proves neither provenance nor append chronology.

Policy 1 is preserved by composition, never copied: after hostile-carrier admission,
an exact V2 projection of the admitted slots is judged by the unmodified
``check_pre_advisory_conformance``, its result is re-admitted by exact type and
member identity, and every policy-1 requirement stays conjunctive.  Nothing reads
the retained run or its lifecycle before policy 1 conforms.

Admission order (before the frozen 5a validator): exact class by identity; exact
``dict`` with the field count checked before any key scan; every key an exact
``str`` before any lookup, equality or hash; scalars and enum members by identity;
finite, integer, character and byte bounds.  No caller callback runs.

Timing (D199 OD3, D200), all comparisons inclusive and measured on the actual
invocation instant, never the intent instant: the window is fixed at
``[S - 360, S - 60]`` from the retained ``PHASE_ACTIVATED`` scheduled end ``S``; the
first invocation falls within one second of the window opening; each later one
within one second after the previous resolution plus the retained configured dwell;
every invocation lies inside the window; a call that fell due inside the window
must be retained; each call lasts at most its retained configured bound; the
retained dwell is at least the 5.0-second floor.  There is no seven-second advisor
rule, no phase-end-minus-five mechanism and no invented latency threshold.  The end
margin is not a timeout, grace, watchdog or stop guarantee.

Implied bounds: a record's context instant is at or before its intent instant, an
invocation is at or before its resolved instant and that at or before the resolution
recording instant (5a record grammar); a first observed instant is at or after its
intent, and a next intent at or after the last observed instant (5a sequence).  So
bounding every attempt record's recording instant by its phase's activation and
elapsed instants bounds every invocation and resolved instant too, and a bound
context tick lies inside policy 1's tick window.

Residuals: an independent operator emergency stop is required; advisory requests,
evaluations and ``ALLOW`` verdicts are data, never actuation.  Clock progress is a
port contract, not a watchdog.  A caller-asserted configuration digest is not
producer binding.  Outer V3 tuple cardinality is not globally bounded, and no global
resource limit is claimed.  The caller owns the carrier graph and must not mutate it
concurrently during this synchronous call.
"""

import enum
import math
import typing

import pydantic

from roastpilot_agent.cold_characterisation.advisory_window import (
    ADVISORY_INVOCATION_ALLOWANCE_SECONDS,
    MIN_POST_COMPLETION_DWELL_SECONDS,
    advisory_window_bounds,
)
from roastpilot_agent.cold_characterisation.conformance import (
    CONFORMANCE_POLICY_VERSION,
    ColdConformanceFinding,
    ColdConformanceOutcome,
    ColdConformanceResult,
    check_pre_advisory_conformance,
)
from roastpilot_agent.cold_characterisation.evidence_advisory import (
    ADMITTED_ENUM_TYPES,
    MAX_ADVISORY_CONTEXT_BYTES,
    ColdAdvisoryAttemptError,
    ColdAdvisoryAttemptEvidenceState,
    ColdAdvisoryEvaluationState,
    ColdAdvisoryIntentRecord,
    ColdAdvisoryInvocationState,
    ColdAdvisoryRationaleState,
    ColdAdvisoryResolution,
    ColdAdvisoryResolutionRecord,
    ColdAdvisorySequence,
    ColdAdvisoryUsageState,
    validate_advisory_attempt_record,
)
from roastpilot_agent.cold_characterisation.evidence_lifecycle import (
    ColdLifecycleEvidenceState,
    ColdLifecycleRecord,
    validate_lifecycle_record,
)
from roastpilot_agent.cold_characterisation.evidence_reader import (
    ColdRetainedRun,
    ColdRetainedRunV2,
    ColdRetainedRunV3,
)
from roastpilot_agent.cold_characterisation.evidence_schema import (
    MAX_INT_DIGITS,
    MAX_TEXT_FIELD_BYTES,
    ColdEvidenceError,
    ColdEvidenceStream,
    ColdPhaseKind,
    validate_record,
    walk_json_value,
)
from roastpilot_agent.cold_characterisation.evidence_store import (
    ColdBindingState,
    ColdEvidenceStoreFailure,
    check_advisory_attempt_binding,
    check_record_binding,
    load_strict_json,
)

__all__ = (
    "ADVISORY_CONFORMANCE_POLICY_VERSION",
    "ColdAdvisoryConformanceFinding",
    "ColdAdvisoryConformanceOutcome",
    "ColdAdvisoryConformanceResult",
    "check_advisory_conformance",
)

ADVISORY_CONFORMANCE_POLICY_VERSION: typing.Final = 2
"""Advisory conformance policy 2.

Distinct from ``QUALIFICATION_POLICY_VERSION`` (identity qualification) and from
``CONFORMANCE_POLICY_VERSION`` (pre-advisory conformance policy 1, which this policy
composes).  The only extension axis is a later version of this constant.
"""


class ColdAdvisoryConformanceOutcome(enum.Enum):
    """The closed advisory outcome; there is no qualification, readiness or hardware value."""

    ADVISORY_CONFORMANT = "advisory_conformant"
    NOT_CONFORMANT = "not_conformant"


class ColdAdvisoryConformanceFinding(enum.Enum):
    """Closed findings; a result lists them uniquely in declaration order."""

    CARRIER_NOT_ADMITTED = "carrier_not_admitted"
    PRE_ADVISORY_NOT_CONFORMANT = "pre_advisory_not_conformant"
    ATTEMPT_NOT_ADMITTED = "attempt_not_admitted"
    ATTEMPT_BINDING_REFUSED = "attempt_binding_refused"
    ATTEMPT_SEQUENCE_REFUSED = "attempt_sequence_refused"
    ATTEMPT_STATE_MISMATCH = "attempt_state_mismatch"
    ATTEMPTS_ABSENT = "attempts_absent"
    ATTEMPTS_OPEN_TAIL = "attempts_open_tail"
    ATTEMPT_RETURNED_FAILURE = "attempt_returned_failure"
    ATTEMPT_ABANDONED = "attempt_abandoned"
    ATTEMPT_UNRESOLVED_AT_PHASE_END = "attempt_unresolved_at_phase_end"
    ATTEMPT_NOT_INVOKED = "attempt_not_invoked"
    RATIONALE_NOT_RETAINED = "rationale_not_retained"
    EVALUATION_NOT_RECORDED = "evaluation_not_recorded"
    USAGE_NOT_RECORDED = "usage_not_recorded"
    CONFIGURATION_NOT_CONSTANT = "configuration_not_constant"
    DESCRIPTOR_NOT_BOUND = "descriptor_not_bound"
    DWELL_BELOW_MINIMUM = "dwell_below_minimum"
    CONTEXT_TICK_NOT_BOUND = "context_tick_not_bound"
    FIRST_INVOCATION_NOT_TIMELY = "first_invocation_not_timely"
    SUBSEQUENT_INVOCATION_NOT_TIMELY = "subsequent_invocation_not_timely"
    INVOCATION_OUTSIDE_WINDOW = "invocation_outside_window"
    WINDOW_CALL_MISSING = "window_call_missing"
    CALL_DURATION_EXCEEDED = "call_duration_exceeded"
    ATTEMPT_CAUSAL_ORDER_VIOLATED = "attempt_causal_order_violated"
    ATTEMPT_AFTER_TERMINATION = "attempt_after_termination"
    CHECKER_INTERNAL_FAILURE = "checker_internal_failure"


_F: typing.TypeAlias = ColdAdvisoryConformanceFinding
_T = typing.TypeVar("_T")
_Attempt: typing.TypeAlias = ColdAdvisoryIntentRecord | ColdAdvisoryResolutionRecord
_PreFindings: typing.TypeAlias = tuple[ColdConformanceFinding, ...]

_OUTCOME_MEMBERS: typing.Final = tuple(ColdAdvisoryConformanceOutcome)
_FINDING_MEMBERS: typing.Final = tuple(ColdAdvisoryConformanceFinding)
_PRE_OUTCOME_MEMBERS: typing.Final = tuple(ColdConformanceOutcome)
_PRE_FINDING_MEMBERS: typing.Final = tuple(ColdConformanceFinding)
_LIFECYCLE_STATE_MEMBERS: typing.Final = tuple(ColdLifecycleEvidenceState)
_ATTEMPT_STATE_MEMBERS: typing.Final = tuple(ColdAdvisoryAttemptEvidenceState)
_ATTEMPT_ENUM_MEMBERS: typing.Final = tuple((kind, tuple(kind)) for kind in ADMITTED_ENUM_TYPES)


def _is_exact_version(value: object, expected: int) -> bool:
    """Whether a value is the exact ``int`` version, never ``True`` or a float."""
    return type(value) is int and value == expected


def _is_member(value: object, members: tuple[enum.Enum, ...]) -> bool:
    """Whether a value is one of the precomputed members, by identity only."""
    return any(value is member for member in members)


def _is_member_tuple(value: object, members: tuple[enum.Enum, ...]) -> bool:
    """Whether a value is an exact ``tuple`` whose every item is an identity member."""
    return type(value) is tuple and all(
        _is_member(item, members) for item in typing.cast(tuple[object, ...], value)
    )


def _strictly_declared(items: tuple[object, ...], order: tuple[enum.Enum, ...]) -> bool:
    """Whether each item's declaration index strictly exceeds the previous item's.

    One check refuses both a duplicate and a wrong order; a non-member never passes.
    """
    previous = -1
    for item in items:
        position = next((index for index, member in enumerate(order) if item is member), -1)
        if not position > previous:
            return False
        previous = position
    return True


class ColdAdvisoryConformanceResult(pydantic.BaseModel):
    """One closed advisory result holding enums and the policy version only."""

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    policy_version: typing.Literal[2]
    outcome: ColdAdvisoryConformanceOutcome
    findings: tuple[ColdAdvisoryConformanceFinding, ...]
    pre_advisory_findings: tuple[ColdConformanceFinding, ...]

    @pydantic.field_validator("policy_version", mode="before")
    @classmethod
    def _require_exact_version(cls, value: object) -> object:
        """Refuse ``True``, ``2.0`` or any value that is not the exact int ``2``."""
        if _is_exact_version(value, ADVISORY_CONFORMANCE_POLICY_VERSION):
            return value
        raise ValueError("policy version must be the exact int 2")

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
        """Refuse anything but an exact tuple of real advisory finding members."""
        if _is_member_tuple(value, _FINDING_MEMBERS):
            return value
        raise ValueError("findings must be closed members")

    @pydantic.field_validator("pre_advisory_findings", mode="before")
    @classmethod
    def _require_pre_finding_members(cls, value: object) -> object:
        """Refuse anything but an exact tuple of real policy-1 finding members."""
        if _is_member_tuple(value, _PRE_FINDING_MEMBERS):
            return value
        raise ValueError("pre-advisory findings must be closed members")

    @pydantic.model_validator(mode="after")
    def _require_closed_agreement(self) -> typing.Self:
        """Require ordered unique findings, outcome agreement and policy-1 agreement."""
        if not (
            _strictly_declared(self.findings, _FINDING_MEMBERS)
            and _strictly_declared(self.pre_advisory_findings, _PRE_FINDING_MEMBERS)
        ):
            raise ValueError("findings are not unique and ordered")
        conformant = self.outcome is ColdAdvisoryConformanceOutcome.ADVISORY_CONFORMANT
        if conformant != (self.findings == ()):
            raise ValueError("findings and outcome disagree")
        flagged = any(item is _F.PRE_ADVISORY_NOT_CONFORMANT for item in self.findings)
        if (self.pre_advisory_findings != ()) != flagged:
            raise ValueError("pre-advisory findings and findings disagree")
        return self


# ------------------------------------------------------ stage 1: admission

_ABSENT: typing.Final = object()
_INT_BOUND: typing.Final = 10**MAX_INT_DIGITS - 1
_V3_FIELDS: typing.Final = (
    "run",
    "lifecycle_state",
    "lifecycle",
    "advisory_attempt_state",
    "advisory_attempts",
)
_PRE_RESULT_FIELDS: typing.Final = ("policy_version", "outcome", "findings")
_ATTEMPT_FIELDS: typing.Final = tuple(
    (model, tuple(model.model_fields))
    for model in (ColdAdvisoryIntentRecord, ColdAdvisoryResolutionRecord)
)
_CONTEXT_FIELD: typing.Final = "context_canonical_json"


class _AdmittedV3(typing.NamedTuple):
    """The admitted V3 root slots; ``run`` and ``lifecycle`` items are left to policy 1."""

    run: object
    lifecycle_state: ColdLifecycleEvidenceState
    lifecycle: tuple[object, ...]
    attempt_state: ColdAdvisoryAttemptEvidenceState
    attempts: tuple[object, ...]


def _admitted_text(value: object, limit: int) -> bool:
    """Whether a value is an exact encodable ``str`` within ``limit`` characters and bytes."""
    if type(value) is not str:
        return False
    if len(value) > limit:
        return False
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return size <= limit


def _shape(node: object, names: tuple[str, ...]) -> tuple[object, ...] | None:
    """Return an identity-found model's declared raw values in order, or ``None``.

    The caller has already found the class by identity.  The field count is checked
    before any key is read, and every key is an exact ``str`` before any lookup.
    """
    raw: object = object.__getattribute__(node, "__dict__")
    if type(raw) is not dict:
        return None
    data = typing.cast(dict[object, object], raw)
    if len(data) != len(names):
        return None
    if not all(type(key) is str for key in data):
        return None
    values = tuple(data.get(name, _ABSENT) for name in names)
    if any(value is _ABSENT for value in values):
        return None
    extra: object = object.__getattribute__(node, "__pydantic_extra__")
    if not (
        extra is None
        or (type(extra) is dict and len(typing.cast(dict[object, object], extra)) == 0)
    ):
        return None
    return values


def _admit_v3(run: object) -> _AdmittedV3 | None:
    """Admit the exact V3 root and its five slots; nested contents are admitted later."""
    if type(run) is not ColdRetainedRunV3:
        return None
    values = _shape(run, _V3_FIELDS)
    if values is None:
        return None
    retained, lifecycle_state, lifecycle, attempt_state, attempts = values
    if type(retained) is not ColdRetainedRun:
        return None
    if not _is_member(lifecycle_state, _LIFECYCLE_STATE_MEMBERS):
        return None
    if type(lifecycle) is not tuple:
        return None
    if not _is_member(attempt_state, _ATTEMPT_STATE_MEMBERS):
        return None
    if type(attempts) is not tuple:
        return None
    return _AdmittedV3(
        retained,
        typing.cast(ColdLifecycleEvidenceState, lifecycle_state),
        typing.cast(tuple[object, ...], lifecycle),
        typing.cast(ColdAdvisoryAttemptEvidenceState, attempt_state),
        typing.cast(tuple[object, ...], attempts),
    )


def _admit_value(name: str, value: object) -> bool:
    """Admit one flat attempt value: ``None``, a bounded exact scalar, or a real member."""
    if value is None:
        return True
    kind = type(value)
    if not (kind is bool or kind is int or kind is float or kind is str):
        for enum_type, members in _ATTEMPT_ENUM_MEMBERS:
            if kind is enum_type:
                return any(value is member for member in members)
        return False
    if kind is int:
        number = typing.cast(int, value)
        return -_INT_BOUND <= number <= _INT_BOUND
    if kind is float:
        return math.isfinite(typing.cast(float, value))
    if kind is str:
        limit = MAX_ADVISORY_CONTEXT_BYTES if name == _CONTEXT_FIELD else MAX_TEXT_FIELD_BYTES
        return _admitted_text(value, limit)
    return True


def _admit_attempt(record: object) -> bool:
    """Admit one exact attempt record's shape and flat values; never calls 5a."""
    kind = type(record)
    names: tuple[str, ...] = ()
    for model, fields in _ATTEMPT_FIELDS:
        if kind is model:
            names = fields
    if not names:
        return False
    values = _shape(record, names)
    if values is None:
        return False
    return all(_admit_value(name, value) for name, value in zip(names, values, strict=True))


def _snapshot_attempt(record: object) -> _Attempt | None:
    """Return the frozen 5a validator's fresh snapshot of an admitted record, or ``None``."""
    if not _admit_attempt(record):
        return None
    try:
        snapshot = validate_advisory_attempt_record(typing.cast(_Attempt, record))
    except ColdEvidenceError:
        return None
    return snapshot


def _project(admitted: _AdmittedV3) -> ColdRetainedRunV2:
    """Build the V2 projection from admitted slots only, never by validating them."""
    values: dict[str, typing.Any] = {
        "run": admitted.run,
        "lifecycle_state": admitted.lifecycle_state,
        "lifecycle": admitted.lifecycle,
    }
    return ColdRetainedRunV2.model_construct(**values)


def _admit_pre_advisory_result(value: object) -> ColdConformanceResult | None:
    """Re-admit policy 1's result by exact type, shape, version and member identity."""
    if type(value) is not ColdConformanceResult:
        return None
    fields = _shape(value, _PRE_RESULT_FIELDS)
    if fields is None:
        return None
    version, outcome, findings = fields
    if not _is_exact_version(version, CONFORMANCE_POLICY_VERSION):
        return None
    if not _is_member(outcome, _PRE_OUTCOME_MEMBERS):
        return None
    if type(findings) is not tuple:
        return None
    items = typing.cast(tuple[object, ...], findings)
    if not all(_is_member(item, _PRE_FINDING_MEMBERS) for item in items):
        return None
    if not _strictly_declared(items, _PRE_FINDING_MEMBERS):
        return None
    if (outcome is ColdConformanceOutcome.PRE_ADVISORY_CONFORMANT) != (len(items) == 0):
        return None
    return value


# ----------------------------------------------- stages 2 to 4: structure, rules

_OFF: typing.Final = ColdPhaseKind.RECORDING_OFF
_ON: typing.Final = ColdPhaseKind.RECORDING_ON
#: Lifecycle positions of each phase's activation and elapsed records, and the terminal.
_ANCHORS: typing.Final = ((_OFF, 0, 1), (_ON, 5, 7))
_TERMINAL: typing.Final = 10
_DESCRIPTOR_NAMES: typing.Final = ("advisor_provider", "advisor_model", "advisor_prompt_version")
_RETURNED_FAILURES: typing.Final = (
    ColdAdvisoryResolution.RETURNED_UNSAFE_OUTPUT,
    ColdAdvisoryResolution.RETURNED_MALFORMED_OUTPUT,
    ColdAdvisoryResolution.RETURNED_PROVIDER_ERROR,
    ColdAdvisoryResolution.RAISED_UNCLASSIFIED,
)


class _PhaseView(typing.NamedTuple):
    """Fresh per-phase anchors, retained ticks and attempts, read after policy 1 conforms."""

    activated_at: float
    elapsed_at: float
    window: tuple[float, float]
    ticks: tuple[tuple[int, float], ...]
    descriptor: tuple[object, ...]
    intents: tuple[ColdAdvisoryIntentRecord, ...]
    resolutions: tuple[ColdAdvisoryResolutionRecord, ...]


def _fresh(record: _T) -> _T:
    """Return ``validate_record``'s fresh snapshot of one admitted v1 record, same class."""
    return typing.cast(_T, validate_record(typing.cast(typing.Any, record)))


def _bind(retained: ColdRetainedRun, snapshots: tuple[_Attempt, ...]) -> dict[object, str] | None:
    """Bind fresh headers, then every attempt; return each phase's identity text, or ``None``."""
    try:
        state = ColdBindingState(retained.run_id)
        identities: dict[object, str] = {}
        for item in retained.headers:
            header = _fresh(item.header)
            check_record_binding(state, header, writer_root=None)
            identities[header.phase] = header.identity.canonical_json
        for snapshot in snapshots:
            check_advisory_attempt_binding(state, snapshot)
    except Exception:
        return None
    return identities


def _replays(snapshots: tuple[_Attempt, ...]) -> bool:
    """Whether the snapshots replay through a fresh run-wide attempt sequence."""
    sequence = ColdAdvisorySequence()
    try:
        for snapshot in snapshots:
            sequence.check(snapshot)
            sequence.commit(snapshot)
    except ColdAdvisoryAttemptError:
        return False
    return True


def _recomputed_state(snapshots: tuple[_Attempt, ...]) -> ColdAdvisoryAttemptEvidenceState:
    """Return the structural state the snapshots imply; ``COMPLETE`` is never health."""
    if not snapshots:
        return ColdAdvisoryAttemptEvidenceState.ABSENT
    if type(snapshots[-1]) is ColdAdvisoryIntentRecord:
        return ColdAdvisoryAttemptEvidenceState.OPEN_TAIL
    return ColdAdvisoryAttemptEvidenceState.COMPLETE


def _identity_descriptor(text: str) -> tuple[object, ...]:
    """Freshly parse one bound identity and return its three ``advisor_*`` values."""
    document = load_strict_json(
        text.encode("utf-8"), malformed=ColdEvidenceStoreFailure.IDENTITY_NOT_V1
    )
    walk_json_value(typing.cast(pydantic.JsonValue, document))
    top = typing.cast(dict[str, object], document) if type(document) is dict else {}
    values = tuple(top.get(name) for name in _DESCRIPTOR_NAMES)
    return values if all(type(value) is str for value in values) else ()


def _views(
    admitted: _AdmittedV3, identities: dict[object, str], snapshots: tuple[_Attempt, ...]
) -> tuple[tuple[_PhaseView, ...], float]:
    """Build fresh per-phase views and the terminal instant from conforming evidence."""
    retained = typing.cast(ColdRetainedRun, admitted.run)
    lifecycle = typing.cast(tuple[ColdLifecycleRecord, ...], admitted.lifecycle)
    ticks: dict[ColdPhaseKind, list[tuple[int, float]]] = {phase: [] for phase in ColdPhaseKind}
    for stream in retained.streams:
        if stream.stream is ColdEvidenceStream.TICK:
            ticks[stream.phase].extend(
                (record.tick, record.monotonic_seconds)
                for record in map(validate_record, stream.records)
                if record.stream == "tick"
            )
    views: list[_PhaseView] = []
    for phase, start, end in _ANCHORS:
        activated = validate_lifecycle_record(lifecycle[start])
        elapsed = validate_lifecycle_record(lifecycle[end])
        views.append(
            _PhaseView(
                activated_at=activated.event_monotonic_seconds,
                elapsed_at=elapsed.event_monotonic_seconds,
                window=advisory_window_bounds(
                    typing.cast(float, activated.scheduled_end_monotonic)
                ),
                ticks=tuple(ticks[phase]),
                descriptor=_identity_descriptor(identities[phase]),
                intents=tuple(
                    s for s in snapshots if s.phase is phase and type(s) is ColdAdvisoryIntentRecord
                ),
                resolutions=tuple(
                    s
                    for s in snapshots
                    if s.phase is phase and type(s) is ColdAdvisoryResolutionRecord
                ),
            )
        )
    terminal = validate_lifecycle_record(lifecycle[_TERMINAL])
    return tuple(views), terminal.event_monotonic_seconds


def _check_outcomes(
    views: tuple[_PhaseView, ...], state: ColdAdvisoryAttemptEvidenceState, found: set[_F]
) -> None:
    """Presence, open-tail, resolution-kind, invocation and decision-payload rules."""
    if state is ColdAdvisoryAttemptEvidenceState.OPEN_TAIL:
        found.add(_F.ATTEMPTS_OPEN_TAIL)
    for view in views:
        if not view.intents:
            found.add(_F.ATTEMPTS_ABSENT)
        for record in view.resolutions:
            kind = record.resolution
            if _is_member(kind, _RETURNED_FAILURES):
                found.add(_F.ATTEMPT_RETURNED_FAILURE)
            if kind is ColdAdvisoryResolution.ABANDONED_AFTER_BOUND:
                found.add(_F.ATTEMPT_ABANDONED)
            if kind is ColdAdvisoryResolution.UNRESOLVED_AT_PHASE_END:
                found.add(_F.ATTEMPT_UNRESOLVED_AT_PHASE_END)
            if record.invocation_state is ColdAdvisoryInvocationState.NOT_INVOKED:
                found.add(_F.ATTEMPT_NOT_INVOKED)
            if kind is not ColdAdvisoryResolution.RETURNED_DECISION:
                continue
            if record.rationale_state is not ColdAdvisoryRationaleState.RETAINED:
                found.add(_F.RATIONALE_NOT_RETAINED)
            if record.evaluation_state is not ColdAdvisoryEvaluationState.RECORDED:
                found.add(_F.EVALUATION_NOT_RECORDED)
            if record.usage_state is not ColdAdvisoryUsageState.RECORDED:
                found.add(_F.USAGE_NOT_RECORDED)


def _configuration(intent: ColdAdvisoryIntentRecord) -> tuple[object, ...]:
    """Return one intent's per-run configuration 9-tuple (OD1)."""
    return (
        intent.context_profile_name,
        intent.context_target_drop_temp_c,
        intent.context_charge_guidance_min_c,
        intent.context_charge_guidance_max_c,
        intent.descriptor_provider,
        intent.descriptor_model,
        intent.descriptor_prompt_version,
        intent.configured_call_bound_seconds,
        intent.configured_dwell_seconds,
    )


def _same(left: tuple[object, ...], right: tuple[object, ...]) -> bool:
    """Whether two configurations agree in exact type and value at every position."""
    return all(type(a) is type(b) and a == b for a, b in zip(left, right, strict=True))


def _check_configuration(views: tuple[_PhaseView, ...], found: set[_F]) -> None:
    """Configuration constancy, descriptor binding, dwell floor and context-tick binding."""
    configurations = [_configuration(intent) for view in views for intent in view.intents]
    if any(not _same(configurations[0], item) for item in configurations[1:]):
        found.add(_F.CONFIGURATION_NOT_CONSTANT)
    for view in views:
        for intent in view.intents:
            descriptor = (
                intent.descriptor_provider,
                intent.descriptor_model,
                intent.descriptor_prompt_version,
            )
            if descriptor != view.descriptor:
                found.add(_F.DESCRIPTOR_NOT_BOUND)
            if intent.configured_dwell_seconds < MIN_POST_COMPLETION_DWELL_SECONDS:
                found.add(_F.DWELL_BELOW_MINIMUM)
            if not any(
                tick == intent.context_tick and instant == intent.context_tick_monotonic
                for tick, instant in view.ticks
            ):
                found.add(_F.CONTEXT_TICK_NOT_BOUND)


def _check_timing(views: tuple[_PhaseView, ...], found: set[_F]) -> None:
    """D199 OD3 and D200 timing on actual invocation instants, inclusive throughout."""
    allowance = ADVISORY_INVOCATION_ALLOWANCE_SECONDS
    for view in views:
        opened, closed = view.window
        pairs = tuple(zip(view.intents, view.resolutions, strict=False))
        invocations: list[float] = []
        for intent, resolution in pairs:
            invoked = resolution.invocation_monotonic
            if invoked is None:
                continue
            invocations.append(invoked)
            if not opened <= invoked <= closed:
                found.add(_F.INVOCATION_OUTSIDE_WINDOW)
            if resolution.resolved_monotonic - invoked > intent.configured_call_bound_seconds:
                found.add(_F.CALL_DURATION_EXCEEDED)
        if not pairs or len(invocations) != len(view.intents):
            continue
        if not opened <= invocations[0] <= opened + allowance:
            found.add(_F.FIRST_INVOCATION_NOT_TIMELY)
        for index in range(1, len(pairs)):
            due = pairs[index - 1][1].resolved_monotonic + pairs[index][0].configured_dwell_seconds
            if not due <= invocations[index] <= due + allowance:
                found.add(_F.SUBSEQUENT_INVOCATION_NOT_TIMELY)
        last_intent, last_resolution = pairs[-1]
        if last_resolution.resolved_monotonic + last_intent.configured_dwell_seconds <= closed:
            found.add(_F.WINDOW_CALL_MISSING)


def _check_bounds(views: tuple[_PhaseView, ...], terminal_at: float, found: set[_F]) -> None:
    """Causal phase bounds and the post-terminal bound for every attempt record."""
    for view in views:
        for record in (*view.intents, *view.resolutions):
            if not view.activated_at <= record.monotonic_seconds <= view.elapsed_at:
                found.add(_F.ATTEMPT_CAUSAL_ORDER_VIOLATED)
            if record.monotonic_seconds > terminal_at:
                found.add(_F.ATTEMPT_AFTER_TERMINATION)


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


def _evaluate(run: object) -> tuple[set[_F], _PreFindings]:
    """Stages 1 to 4 with their early returns; rule groups are individually guarded."""
    admitted = _admit_v3(run)
    if admitted is None:
        return {_F.CARRIER_NOT_ADMITTED}, ()
    collected: list[_Attempt] = []
    for record in admitted.attempts:
        snapshot = _snapshot_attempt(record)
        if snapshot is None:
            return {_F.ATTEMPT_NOT_ADMITTED}, ()
        collected.append(snapshot)
    snapshots = tuple(collected)
    pre = _admit_pre_advisory_result(check_pre_advisory_conformance(_project(admitted)))
    if pre is None:
        return {_F.CHECKER_INTERNAL_FAILURE}, ()
    if pre.outcome is not ColdConformanceOutcome.PRE_ADVISORY_CONFORMANT:
        return {_F.PRE_ADVISORY_NOT_CONFORMANT}, pre.findings
    identities = _bind(typing.cast(ColdRetainedRun, admitted.run), snapshots)
    if identities is None:
        return {_F.ATTEMPT_BINDING_REFUSED}, ()
    if not _replays(snapshots):
        return {_F.ATTEMPT_SEQUENCE_REFUSED}, ()
    if _recomputed_state(snapshots) is not admitted.attempt_state:
        return {_F.ATTEMPT_STATE_MISMATCH}, ()
    found: set[_F] = set()
    built = _guarded(found, lambda: _views(admitted, identities, snapshots))
    if built is not None:
        views, terminal_at = built
        _guarded(found, lambda: _check_outcomes(views, admitted.attempt_state, found))
        _guarded(found, lambda: _check_configuration(views, found))
        _guarded(found, lambda: _check_timing(views, found))
        _guarded(found, lambda: _check_bounds(views, terminal_at, found))
    return found, ()


def check_advisory_conformance(run: object) -> ColdAdvisoryConformanceResult:
    """Check one retained V3 run against advisory conformance policy 2.

    The run conforms if and only if policy 1 conforms on its exact V2 projection and
    no advisory finding is recorded.  A refusal or internal exception never conforms;
    a ``BaseException`` that is not an ``Exception`` propagates.  The result carries
    enums and the version only; no carrier, record or error text is formatted.

    Args:
        run: A candidate ``ColdRetainedRunV3``, as the strict V3 reader returns it.

    Returns:
        The closed advisory conformance result.
    """
    evaluated: tuple[set[_F], _PreFindings] | None = None
    try:
        evaluated = _evaluate(run)
    except Exception:
        evaluated = None
    found, pre = ({_F.CHECKER_INTERNAL_FAILURE}, ()) if evaluated is None else evaluated
    findings = tuple(member for member in _FINDING_MEMBERS if member in found)
    return ColdAdvisoryConformanceResult(
        policy_version=ADVISORY_CONFORMANCE_POLICY_VERSION,
        outcome=(
            ColdAdvisoryConformanceOutcome.NOT_CONFORMANT
            if findings
            else ColdAdvisoryConformanceOutcome.ADVISORY_CONFORMANT
        ),
        findings=findings,
        pre_advisory_findings=pre,
    )
