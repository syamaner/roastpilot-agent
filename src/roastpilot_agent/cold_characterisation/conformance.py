"""Pure, versioned pre-advisory conformance check over one retained cold run (#954).

``check_pre_advisory_conformance`` decides whether one retained v2 run is internally
consistent pre-advisory cold-characterisation evidence.  It performs no file, network,
process or clock access, holds no actuator, transport or provider surface, and never
logs or renders a caller value.  ``PRE_ADVISORY_CONFORMANT`` means internal
pre-advisory evidence conformance only: it is not qualification, readiness, hardware,
physical, detector or provenance evidence.  Any retained advisory record is a
finding, and absent advisory evidence is never read as meeting a future advisory
criterion.

Pre-admission guarantee: before any existing validator, hash, equality, attribute
access, serialisation or pydantic validation touches the caller's value, one bounded
iterative walk admits the whole carrier graph.  After admission, every value the
checker or an existing validator reaches is an exact builtin with exact ``str`` keys,
an exact package model with an exact ``__dict__``, or a real package enum member; no
caller-defined code runs.  The walk borrows the caller's graph and snapshots nothing:
it holds only temporary references into it.  A package model's ``__dict__`` must have
exactly its declared field count before any key is scanned; a nested model's
remaining node budget is checked after that bounded extraction.  Each variable-size
record ``dict``/``list`` is checked against its existing collection-length and
remaining-node allowances, and each identity ``dict``/``list`` against its existing
remaining-node allowance (identity has no separate collection-length cap here),
before its keys are scanned or its items listed.  The outer carrier tuples are
iterated, never copied or capped.
The caller owns that graph and must not mutate it concurrently during this
synchronous call; hostile data, not hostile in-process code, is the threat addressed.
Every later rule reads only fresh validator snapshots and the interpretation
capability.

Append-provenance limitation (within the admitted provenance boundary; not a
waiver): v1 records carry one instant and no append instant, so this checker cannot
prove that a backdated recording-off record was appended before the recording-on
header bound, that a record stamped at or before the terminal event instant was
appended before termination, or the order of appends that share an instant.  Equal
instants are admitted because no rule depends on strict order at equality.  The
inclusive chains place recording-off headers, ticks and hosts at or before
recording-on admission; a recording-off finalisation record is bounded above only by
its lifecycle recording instant.  A tick before its activation instant is refused by
the window rule and is never replayed with a negative since-activation input.  The
checker proves internal evidence consistency only, never actual append provenance.

Residuals: an independent operator emergency stop is required.  Per-tick evidence
covers heat, roast fan and cooling; all six command dimensions are checked only at
D195 finalisation.  Clock progress is a port contract, not a watchdog: a stalled
clock or sleep can leave a phase unfinished, and such a run never conforms.  A stall
that later resumes can still yield a completed, conforming phase with sparse retained
ticks, because policy v1 has no tick-density or maximum-gap rule; conformance does
not establish continuous observation.  A finite temperature is not a plausibility
claim.  ``MCP_RESPONSE_NOT_ADMITTED`` covers
both null-device and projection refusals.  A missing or failed append never proves
that the commanded state was safe.  The absence of a model is unknown, not safe.
"""

import enum
import math
import typing

import pydantic

from roastpilot_agent.cold_characterisation.acceptance import (
    D191_N_LIMIT,
    D191_X_LIMIT_MS,
    ColdCheckOutcome,
    ColdInterpretation,
    ColdReboundPhase,
    interpret_retained_run,
)
from roastpilot_agent.cold_characterisation.engine_policy import evaluate_tick
from roastpilot_agent.cold_characterisation.evidence_lifecycle import (
    ADMITTED_ENUM_TYPES as LIFECYCLE_ENUM_TYPES,
)
from roastpilot_agent.cold_characterisation.evidence_lifecycle import (
    COLD_TRANSITION_BUDGET_SECONDS,
    ColdLifecycleChildStart,
    ColdLifecycleChildStop,
    ColdLifecycleEvent,
    ColdLifecycleEvidenceState,
    ColdLifecycleFinalisationResult,
    ColdLifecycleRecord,
    ColdLifecycleSequence,
    ColdRunTermination,
    validate_lifecycle_record,
)
from roastpilot_agent.cold_characterisation.evidence_reader import (
    ColdRetainedHeader,
    ColdRetainedRun,
    ColdRetainedRunV2,
    ColdRetainedStream,
)
from roastpilot_agent.cold_characterisation.evidence_schema import (
    ADMITTED_ENUM_TYPES as SCHEMA_ENUM_TYPES,
)
from roastpilot_agent.cold_characterisation.evidence_schema import (
    MAX_COLLECTION_LENGTH,
    MAX_ENVELOPE_BYTES,
    MAX_INPUT_AGGREGATE_BYTES,
    MAX_INT_DIGITS,
    MAX_JSON_DEPTH,
    MAX_JSON_KEY_BYTES,
    MAX_JSON_NODES,
    MAX_TEXT_FIELD_BYTES,
    ColdAbortRecord,
    ColdAdvisoryRecord,
    ColdEvidenceStream,
    ColdFinalisationRecord,
    ColdHostRecord,
    ColdHostSample,
    ColdPhaseKind,
    ColdRunHeader,
    ColdSafetyEvaluation,
    ColdSealedEnvelope,
    ColdTickAudioSample,
    ColdTickDeviceEvidence,
    ColdTickRecord,
    ColdTickRoastFanEvidence,
    ColdTickSessionEvidence,
    validate_record,
    walk_json_value,
)
from roastpilot_agent.cold_characterisation.evidence_store import (
    ColdBindingState,
    ColdEvidenceStoreFailure,
    ColdRetainedIdentityV1,
    canonical_json,
    check_lifecycle_binding,
    check_record_binding,
    load_strict_json,
)
from roastpilot_agent.cold_characterisation.host_policy import (
    HOST_MIN_FREE_BYTES_DURING,
    free_bytes_meets_floor,
    mem_available_is_admitted,
    parse_retained_throttle_hex,
    soc_temp_below_limit,
    throttle_word_is_clear,
)
from roastpilot_agent.cold_characterisation.mcp import (
    finalisation_has_required_safety_evidence,
    finalisation_is_clean,
)

__all__ = (
    "CONFORMANCE_POLICY_VERSION",
    "ColdConformanceFinding",
    "ColdConformanceOutcome",
    "ColdConformanceResult",
    "check_pre_advisory_conformance",
)

#: Pre-advisory policy; slice 5 bumps it when it adds advisory policy.
CONFORMANCE_POLICY_VERSION: typing.Final = 1


class ColdConformanceOutcome(enum.Enum):
    """The closed conformance outcome; there is no qualification or readiness value."""

    PRE_ADVISORY_CONFORMANT = "pre_advisory_conformant"
    NOT_CONFORMANT = "not_conformant"


class ColdConformanceFinding(enum.Enum):
    """Closed findings; a result lists them uniquely in declaration order."""

    CARRIER_NOT_ADMITTED = "carrier_not_admitted"
    INTERPRETATION_REFUSED = "interpretation_refused"
    LIFECYCLE_ABSENT = "lifecycle_absent"
    LIFECYCLE_NOT_ADMITTED = "lifecycle_not_admitted"
    PHASE_MISSING = "phase_missing"
    TICKS_ABSENT = "ticks_absent"
    ABORT_RECORDED = "abort_recorded"
    ADVISORY_EVIDENCE_PRESENT = "advisory_evidence_present"
    ACCEPTANCE_CHECK_FAILED = "acceptance_check_failed"
    D191_METRICS_UNAVAILABLE = "d191_metrics_unavailable"
    D191_N_EXCEEDED = "d191_n_exceeded"
    D191_X_EXCEEDED = "d191_x_exceeded"
    FINALISATION_NOT_UNIQUE = "finalisation_not_unique"
    FINALISATION_NOT_COLD_PURPOSE = "finalisation_not_cold_purpose"
    FINALISATION_NOT_CLEAN = "finalisation_not_clean"
    FINALISATION_SAFETY_EVIDENCE_MISSING = "finalisation_safety_evidence_missing"
    FINALISATION_SESSION_MISMATCH = "finalisation_session_mismatch"
    IDENTITY_DELTA_NOT_ADMITTED = "identity_delta_not_admitted"
    LIFECYCLE_GRAMMAR_MISMATCH = "lifecycle_grammar_mismatch"
    TERMINAL_ABSENT = "terminal_absent"
    TERMINATION_NOT_COMPLETED = "termination_not_completed"
    PHASE_ABORTED_NOT_FINALISED = "phase_aborted_not_finalised"
    FINALISATION_RESULT_NOT_RECORDED_CLEAN = "finalisation_result_not_recorded_clean"
    CHILD_OPERATION_NOT_CLEAN = "child_operation_not_clean"
    SESSION_BINDING_MISMATCH = "session_binding_mismatch"
    PHASE_SESSIONS_NOT_DISTINCT = "phase_sessions_not_distinct"
    SCHEDULED_END_MISMATCH = "scheduled_end_mismatch"
    TICK_COUNT_MISMATCH = "tick_count_mismatch"
    TICK_OUTSIDE_WINDOW = "tick_outside_window"
    HOST_EVIDENCE_MISMATCH = "host_evidence_mismatch"
    HOST_BOUND_VALUE_NOT_ADMITTED = "host_bound_value_not_admitted"
    RETAINED_TICK_POLICY_VIOLATED = "retained_tick_policy_violated"
    TRANSITION_NOT_BOUND = "transition_not_bound"
    TRANSITION_BUDGET_EXCEEDED = "transition_budget_exceeded"
    CAUSAL_ORDER_VIOLATED = "causal_order_violated"
    RECORD_AFTER_TERMINATION = "record_after_termination"
    CHECKER_INTERNAL_FAILURE = "checker_internal_failure"


class ColdConformanceResult(pydantic.BaseModel):
    """One closed conformance result holding enums and the policy version only."""

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    policy_version: typing.Literal[1]
    outcome: ColdConformanceOutcome
    findings: tuple[ColdConformanceFinding, ...]

    @pydantic.field_validator("policy_version", mode="before")
    @classmethod
    def _require_exact_version(cls, value: object) -> object:
        """Refuse ``True``, ``1.0`` or any value that is not the exact int ``1``."""
        if type(value) is int and value == CONFORMANCE_POLICY_VERSION:
            return value
        raise ValueError("policy version must be the exact int 1")

    @pydantic.model_validator(mode="after")
    def _require_closed_findings(self) -> typing.Self:
        """Require unique ordered findings, and conformance exactly when there are none."""
        ordered = tuple(member for member in ColdConformanceFinding if member in self.findings)
        conformant = self.outcome is ColdConformanceOutcome.PRE_ADVISORY_CONFORMANT
        if self.findings != ordered or conformant == bool(self.findings):
            raise ValueError("findings and outcome disagree")
        return self


_F: typing.TypeAlias = ColdConformanceFinding
_T = typing.TypeVar("_T")
_Members: typing.TypeAlias = tuple[tuple[type[enum.Enum], tuple[enum.Enum, ...]], ...]

# ------------------------------------------------------ stage 1: pre-admission

_INT_BOUND: typing.Final = 10**MAX_INT_DIGITS - 1
#: The identity model node, its five fields, and the two tolerant-extras roots.
_IDENTITY_SCAFFOLD_NODES: typing.Final = 8
_IDENTITY_TEXT_BUDGET: typing.Final = 3 * MAX_ENVELOPE_BYTES + (
    MAX_JSON_NODES + _IDENTITY_SCAFFOLD_NODES
) * (MAX_INT_DIGITS + 32 + 5)
_RECORD_CLASSES: typing.Final[tuple[type[pydantic.BaseModel], ...]] = (
    ColdRunHeader,
    ColdTickRecord,
    ColdHostRecord,
    ColdAdvisoryRecord,
    ColdFinalisationRecord,
    ColdAbortRecord,
)
_MODEL_CLASSES: typing.Final[tuple[type[pydantic.BaseModel], ...]] = (
    ColdTickAudioSample,
    ColdHostSample,
    ColdTickDeviceEvidence,
    ColdTickRoastFanEvidence,
    ColdTickSessionEvidence,
    ColdSafetyEvaluation,
    ColdSealedEnvelope,
    *_RECORD_CLASSES,
)
_CARRIER_CLASSES: typing.Final[tuple[type[pydantic.BaseModel], ...]] = (
    ColdRetainedRunV2,
    ColdRetainedRun,
    ColdRetainedHeader,
    ColdRetainedStream,
    ColdRetainedIdentityV1,
    ColdLifecycleRecord,
)
_FIELD_NAMES: typing.Final = tuple(
    (model, tuple(model.model_fields)) for model in (*_MODEL_CLASSES, *_CARRIER_CLASSES)
)
#: Each model's trusted field-name bytes, charged as ``_extract_model_fields`` charges them.
_NAME_BYTES: typing.Final = tuple(
    (model, sum(len(name.encode("utf-8")) for name in names)) for model, names in _FIELD_NAMES
)
_ALL_ENUMS: typing.Final[tuple[type[enum.Enum], ...]] = (*SCHEMA_ENUM_TYPES, *LIFECYCLE_ENUM_TYPES)
_ENUM_MEMBERS: typing.Final[_Members] = tuple(
    (kind, tuple(kind))
    for index, kind in enumerate(_ALL_ENUMS)
    if not any(kind is earlier for earlier in _ALL_ENUMS[:index])
)
_STATE_MEMBERS: typing.Final[_Members] = (
    (ColdLifecycleEvidenceState, tuple(ColdLifecycleEvidenceState)),
)
_PHASE_MEMBERS: typing.Final[_Members] = ((ColdPhaseKind, tuple(ColdPhaseKind)),)
_STREAM_MEMBERS: typing.Final[_Members] = ((ColdEvidenceStream, tuple(ColdEvidenceStream)),)


def _scan(
    kind: type[object], table: tuple[type[pydantic.BaseModel], ...]
) -> type[pydantic.BaseModel] | None:
    """Return the closed class that ``kind`` is, by identity only, or ``None``."""
    for candidate in table:
        if kind is candidate:
            return candidate
    return None


def _is_member(value: object, table: _Members = _ENUM_MEMBERS) -> bool:
    """Whether a value is a real member, by identity, of one admitted enum type."""
    kind = type(value)
    for enum_type, members in table:
        if kind is enum_type:
            return any(value is member for member in members)
    return False


def _charge(text: str, budget: list[int], limit: int) -> int | None:
    """Charge one exact string's UTF-8 size to a budget; ``None`` when refused."""
    if budget[0] + len(text) > limit:
        return None
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if budget[0] + size > limit:
        return None
    budget[0] += size
    return size


def _fields(node: object, model: type[pydantic.BaseModel]) -> tuple[object, ...] | None:
    """Return an exact package model's declared raw values in order, or ``None``.

    The caller has already found ``type(node) is model`` by identity.
    """
    names = next(names for candidate, names in _FIELD_NAMES if candidate is model)
    data: object = object.__getattribute__(node, "__dict__")
    extra: object = object.__getattribute__(node, "__pydantic_extra__")
    if type(data) is not dict:
        return None
    if extra is not None and not (
        type(extra) is dict and len(typing.cast(dict[object, object], extra)) == 0
    ):
        return None
    raw = typing.cast(dict[object, object], data)
    if len(raw) != len(names) or not _exact_str_keys(raw):
        return None
    if not all(name in raw for name in names):
        return None
    return tuple(raw[name] for name in names)


def _exact_str_keys(mapping: dict[object, object]) -> bool:
    """Whether every key is an exact ``str``; each caller bounds ``len(mapping)`` first."""
    return all(type(key) is str for key in mapping)


def _items(items: list[object]) -> list[object]:
    """List borrowed references from an exact list; each caller bounds its length first."""
    return list(items)


def _scalar(value: object, budget: list[int], limit: int) -> bool:
    """Admit and charge one JSON scalar or string exactly as the record walker does."""
    kind = type(value)
    if kind is str:
        return _charge(typing.cast(str, value), budget, limit) is not None
    if value is None or kind is bool:
        budget[0] += 5
    elif kind is int:
        number = typing.cast(int, value)
        if number > _INT_BOUND or number < -_INT_BOUND:
            return False
        budget[0] += MAX_INT_DIGITS + 1
    elif kind is float:
        if not math.isfinite(typing.cast(float, value)):
            return False
        budget[0] += 32
    else:
        return False
    return True


def _keys(mapping: dict[object, object], budget: list[int], limit: int) -> list[str] | None:
    """Admit every key as an exact bounded ``str`` before any key is used."""
    if not _exact_str_keys(mapping):
        return None
    keys = typing.cast(list[str], list(mapping))
    for key in keys:
        size = _charge(key, budget, limit)
        if size is None or size > MAX_JSON_KEY_BYTES:
            return None
    return keys


def _admit_record(record: object, model: type[pydantic.BaseModel]) -> bool:
    """Walk one v1 record with ``validate_record``'s own limits and charging."""
    budget = [0]
    fields = _charged_fields(record, model, budget)
    if fields is None:
        return False
    nodes = 1
    stack: list[tuple[object, int]] = [(value, 1) for value in reversed(fields)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        nested = _scan(type(current), _MODEL_CLASSES)
        children: list[object]
        if nested is not None:
            if depth >= MAX_JSON_DEPTH:
                return False
            values = _charged_fields(current, nested, budget)
            if values is None or nodes + len(stack) + len(values) > MAX_JSON_NODES:
                return False
            children = list(values)
        elif type(current) is dict or type(current) is list:
            container = typing.cast(dict[object, object] | list[object], current)
            size = len(container)
            if (
                size > MAX_COLLECTION_LENGTH
                or (depth >= MAX_JSON_DEPTH and size > 0)
                or nodes + len(stack) + size > MAX_JSON_NODES
            ):
                return False
            if type(container) is dict:
                keys = _keys(container, budget, MAX_INPUT_AGGREGATE_BYTES)
                if keys is None:
                    return False
                children = [container[key] for key in keys]
            else:
                children = _items(typing.cast(list[object], container))
        elif _is_member(current) or _scalar(current, budget, MAX_INPUT_AGGREGATE_BYTES):
            children = []
        else:
            return False
        stack.extend((child, depth + 1) for child in reversed(children))
        if budget[0] > MAX_INPUT_AGGREGATE_BYTES:
            return False
    return True


def _charged_fields(
    node: object, model: type[pydantic.BaseModel], budget: list[int]
) -> tuple[object, ...] | None:
    """Return a model's raw values and charge its trusted field names to the budget.

    An overflow is refused by the walk's aggregate check, as the validator refuses it.
    """
    budget[0] += next(size for candidate, size in _NAME_BYTES if candidate is model)
    return _fields(node, model)


def _admit_lifecycle(record: object) -> bool:
    """Admit one flat lifecycle record: exact scalars, bounded text, real members."""
    values = _fields(record, ColdLifecycleRecord)
    if values is None:
        return False
    budget = [0]
    return all(
        _is_member(value) or _scalar(value, budget, MAX_INPUT_AGGREGATE_BYTES) for value in values
    )


def _admit_identity(identity: object) -> bool:
    """Walk one retained v1 identity within the derived envelope and scaffold bounds."""
    values = _fields(identity, ColdRetainedIdentityV1)
    if values is None:
        return False
    if type(values[0]) is not str or type(values[1]) is not str:
        return False
    if any(type(value) is not dict for value in values[2:]):
        return False
    budget = [0]
    nodes = 1
    stack: list[tuple[object, int]] = [(value, 1) for value in values]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES + _IDENTITY_SCAFFOLD_NODES or depth > MAX_JSON_DEPTH + 1:
            return False
        children: list[object]
        if type(current) is dict or type(current) is list:
            container = typing.cast(dict[object, object] | list[object], current)
            if nodes + len(stack) + len(container) > MAX_JSON_NODES + _IDENTITY_SCAFFOLD_NODES:
                return False
            if type(container) is dict:
                keys = _keys(container, budget, _IDENTITY_TEXT_BUDGET)
                if keys is None:
                    return False
                children = [container[key] for key in keys]
            else:
                children = _items(typing.cast(list[object], container))
        elif _scalar(current, budget, _IDENTITY_TEXT_BUDGET):
            children = []
        else:
            return False
        stack.extend((child, depth + 1) for child in children)
        if budget[0] > _IDENTITY_TEXT_BUDGET:
            return False
    return True


def _bounded_text(value: object) -> bool:
    """Whether a value is an exact ``str`` of at most ``MAX_TEXT_FIELD_BYTES`` bytes."""
    return type(value) is str and _charge(value, [0], MAX_TEXT_FIELD_BYTES) is not None


def _admit_run(retained: object) -> bool:
    """Admit the fixed ``ColdRetainedRun`` positions and every record and identity root."""
    if type(retained) is not ColdRetainedRun:
        return False
    fields = _fields(retained, ColdRetainedRun)
    if fields is None:
        return False
    run_id, digest, headers, streams = fields
    if not (_bounded_text(run_id) and _bounded_text(digest)):
        return False
    if type(headers) is not tuple or type(streams) is not tuple:
        return False
    for item in typing.cast(tuple[object, ...], headers):
        pair = _fields(item, ColdRetainedHeader) if type(item) is ColdRetainedHeader else None
        if pair is None or type(pair[0]) is not ColdRunHeader:
            return False
        if type(pair[1]) is not ColdRetainedIdentityV1:
            return False
        if not (_admit_record(pair[0], ColdRunHeader) and _admit_identity(pair[1])):
            return False
    for item in typing.cast(tuple[object, ...], streams):
        triple = _fields(item, ColdRetainedStream) if type(item) is ColdRetainedStream else None
        if triple is None or type(triple[2]) is not tuple:
            return False
        if not (_is_member(triple[0], _PHASE_MEMBERS) and _is_member(triple[1], _STREAM_MEMBERS)):
            return False
        for record in typing.cast(tuple[object, ...], triple[2]):
            model = _scan(type(record), _RECORD_CLASSES)
            if model is None or not _admit_record(record, model):
                return False
    return True


def _admit(run: object) -> bool:
    """Stage 1: admit the whole carrier graph before anything else touches it."""
    if type(run) is not ColdRetainedRunV2:
        return False
    fields = _fields(run, ColdRetainedRunV2)
    if fields is None:
        return False
    retained, state, lifecycle = fields
    if type(lifecycle) is not tuple or not _is_member(state, _STATE_MEMBERS):
        return False
    items = typing.cast(tuple[object, ...], lifecycle)
    if (state is ColdLifecycleEvidenceState.ABSENT) != (len(items) == 0):
        return False
    for item in items:
        if type(item) is not ColdLifecycleRecord or not _admit_lifecycle(item):
            return False
    return _admit_run(retained)


# ------------------------------------------- stages 2 and 3: interpreted rules

_OFF: typing.Final = ColdPhaseKind.RECORDING_OFF
_ON: typing.Final = ColdPhaseKind.RECORDING_ON
_E: typing.TypeAlias = ColdLifecycleEvent
#: The exact v1 success grammar of ``(phase, event)`` lifecycle pairs.
_GRAMMAR: typing.Final = (
    (_OFF, _E.PHASE_ACTIVATED),
    (_OFF, _E.OBSERVATION_WINDOW_ELAPSED),
    (_OFF, _E.FINALISATION_RETURNED),
    (_OFF, _E.CHILD_STOPPED),
    (_OFF, _E.CHILD_STARTED),
    (_ON, _E.PHASE_ACTIVATED),
    (_ON, _E.TRANSITION_MEASURED),
    (_ON, _E.OBSERVATION_WINDOW_ELAPSED),
    (_ON, _E.FINALISATION_RETURNED),
    (_ON, _E.CHILD_STOPPED),
    (_ON, _E.RUN_TERMINATED),
)
#: The only identity leaves that may differ between the two phases.
_MASKED_LEAVES: typing.Final = (
    ("device_config", "recording_enabled"),
    ("device_config", "recording_autocapture"),
    ("server_info", "started_at_utc"),
    ("effective_mcp_profile", "source_sha256"),
    ("effective_mcp_profile", "source_byte_length"),
)
_Snapshots: typing.TypeAlias = tuple[ColdLifecycleRecord, ...]


def _guarded(
    found: set[ColdConformanceFinding],
    failure: ColdConformanceFinding,
    call: typing.Callable[[], _T],
) -> _T | None:
    """Run one rule group; any ``Exception`` records ``failure`` and retains nothing."""
    try:
        value: _T | None = call()
    except Exception:
        value = None
        failed = True
    else:
        failed = False
    if failed:
        found.add(failure)
    return value


def _check_phases(interpretation: ColdInterpretation, found: set[ColdConformanceFinding]) -> None:
    """Presence, abort, advisory, acceptance, D191 and D195 rules for each present phase."""
    rebound = interpretation.rebound.phases
    if tuple(phase.phase for phase in rebound) != tuple(ColdPhaseKind):
        found.add(_F.PHASE_MISSING)
    for phase, item in zip(rebound, interpretation.phases, strict=True):
        if phase.aborts:
            found.add(_F.ABORT_RECORDED)
        if phase.advisories:
            found.add(_F.ADVISORY_EVIDENCE_PRESENT)
        if not phase.ticks:
            found.add(_F.TICKS_ABSENT)
        if not all(_host_values_admitted(record.sample) for record in phase.hosts):
            found.add(_F.HOST_BOUND_VALUE_NOT_ADMITTED)
        if any(result.outcome is ColdCheckOutcome.FAIL for result in item.results):
            found.add(_F.ACCEPTANCE_CHECK_FAILED)
        metrics = item.d191
        if metrics is None:
            found.add(_F.D191_METRICS_UNAVAILABLE)
        else:
            if metrics.max_consecutive_overflow_count > D191_N_LIMIT:
                found.add(_F.D191_N_EXCEEDED)
            if metrics.peak_trailing_lost_audio_ms > D191_X_LIMIT_MS:
                found.add(_F.D191_X_EXCEEDED)
        result = phase.finalisation
        if len(phase.finalisations) != 1 or result is None:
            found.add(_F.FINALISATION_NOT_UNIQUE)
            continue
        if result.session_purpose != "cold_characterisation":
            found.add(_F.FINALISATION_NOT_COLD_PURPOSE)
        if not finalisation_is_clean(result):
            found.add(_F.FINALISATION_NOT_CLEAN)
        if not finalisation_has_required_safety_evidence(result):
            found.add(_F.FINALISATION_SAFETY_EVIDENCE_MISSING)


def _host_values_admitted(sample: ColdHostSample) -> bool:
    """Whether one retained host sample meets every AC15 during-run bound (shared policy)."""
    word = parse_retained_throttle_hex(sample.throttled_word_hex)
    return (
        soc_temp_below_limit(sample.soc_temp_c)
        and word is not None
        and throttle_word_is_clear(word)
        and mem_available_is_admitted(sample.mem_available_bytes)
        and free_bytes_meets_floor(sample.free_bytes, HOST_MIN_FREE_BYTES_DURING)
    )


def _exact_map(value: object) -> dict[str, object]:
    """Return one identity section known to be an exact JSON object."""
    if type(value) is not dict:  # pragma: no cover - read_identity_v1 requires every section.
        raise ValueError("identity section is not an exact object")
    return typing.cast(dict[str, object], value)


def _identity_document(phase: ColdReboundPhase) -> dict[str, object]:
    """Freshly parse one bound phase header's bounded, walked identity JSON."""
    document = load_strict_json(
        phase.header.identity.canonical_json.encode("utf-8"),
        malformed=ColdEvidenceStoreFailure.IDENTITY_NOT_V1,
    )
    walk_json_value(typing.cast(pydantic.JsonValue, document))
    return _exact_map(document)


def _masked_identity(phase: ColdReboundPhase, recording: bool) -> str | None:
    """Return canonical identity text with the five allowlisted leaves masked, or ``None``."""
    top = _identity_document(phase)
    device = _exact_map(top["device_config"])
    server = _exact_map(top["server_info"])
    profile = _exact_map(top["effective_mcp_profile"])
    admitted = (
        type(device["recording_enabled"]) is bool
        and device["recording_enabled"] is recording
        and type(device["recording_autocapture"]) is bool
        and device["recording_autocapture"] is recording
        and type(server["started_at_utc"]) is str
        and type(profile["source_sha256"]) is str
        and type(profile["source_byte_length"]) is int
    )
    if not admitted:
        return None
    for section, leaf in _MASKED_LEAVES:
        _exact_map(top[section])[leaf] = None
    return canonical_json(top)


def _check_identity_delta(
    interpretation: ColdInterpretation, found: set[ColdConformanceFinding]
) -> None:
    """Require canonically equal phase identities outside the five allowlisted leaves."""
    rebound = interpretation.rebound.phases
    if len(rebound) != len(ColdPhaseKind):
        return
    off = _masked_identity(rebound[0], False)
    on = _masked_identity(rebound[1], True)
    if off is None or on is None or off != on:
        found.add(_F.IDENTITY_DELTA_NOT_ADMITTED)


def _admit_lifecycle_evidence(
    carrier: ColdRetainedRunV2,
    interpretation: ColdInterpretation,
    found: set[ColdConformanceFinding],
) -> _Snapshots | None:
    """Snapshot, bind and order every lifecycle record against fresh header bindings."""
    if not carrier.lifecycle:
        found.add(_F.LIFECYCLE_ABSENT)
        return None
    snapshots = tuple(validate_lifecycle_record(record) for record in carrier.lifecycle)
    state = ColdBindingState(interpretation.rebound.run_id)
    for phase in interpretation.rebound.phases:
        check_record_binding(state, validate_record(phase.header), writer_root=None)
    sequence = ColdLifecycleSequence()
    for snapshot in snapshots:
        check_lifecycle_binding(state, snapshot)
        sequence.check(snapshot)
        sequence.commit(snapshot)
    return snapshots


def _check_lifecycle_fields(snapshots: _Snapshots, found: set[ColdConformanceFinding]) -> bool:
    """Apply the v1 success grammar and the independent field rules; return the match."""
    matched = len(snapshots) == len(_GRAMMAR) and all(
        record.phase is phase and record.event is event
        for record, (phase, event) in zip(snapshots, _GRAMMAR, strict=True)
    )
    if not matched:
        found.add(_F.LIFECYCLE_GRAMMAR_MISMATCH)
    terminals = [record for record in snapshots if record.event is _E.RUN_TERMINATED]
    if not terminals:
        found.add(_F.TERMINAL_ABSENT)
    if any(record.termination is ColdRunTermination.FAILED for record in terminals):
        found.add(_F.TERMINATION_NOT_COMPLETED)
    for record in snapshots:
        if record.event is _E.PHASE_ABORTED_NOT_FINALISED:
            found.add(_F.PHASE_ABORTED_NOT_FINALISED)
        if (
            record.event is _E.FINALISATION_RETURNED
            and record.finalisation_result is not ColdLifecycleFinalisationResult.CLEAN_RECORDED
        ):
            found.add(_F.FINALISATION_RESULT_NOT_RECORDED_CLEAN)
        if (
            record.child_stop is ColdLifecycleChildStop.UNCONFIRMED
            or record.child_start is ColdLifecycleChildStart.FAILED
        ):
            found.add(_F.CHILD_OPERATION_NOT_CLEAN)
    return matched


def _check_matched(
    interpretation: ColdInterpretation,
    snapshots: _Snapshots,
    found: set[ColdConformanceFinding],
) -> None:
    """Session, window, transition, causal-order and post-terminal rules (grammar matched)."""
    a_off, e_off, f_off, cs_off, ct_off, a_on, measured, e_on, f_on, cs_on, terminal = snapshots
    off, on = interpretation.rebound.phases
    s_off, s_on = a_off.session_id, a_on.session_id
    pairs = [
        (e_off.session_id, s_off),
        (f_off.session_id, s_off),
        (e_on.session_id, s_on),
        (f_on.session_id, s_on),
        (measured.session_id, s_on),
        (measured.previous_phase_session_id, s_off),
    ]
    pairs += [(tick.session.session_id, s_off) for tick in off.ticks]
    pairs += [(tick.session.session_id, s_on) for tick in on.ticks]
    if any(left != right for left, right in pairs):
        found.add(_F.SESSION_BINDING_MISMATCH)
    if s_off == s_on:
        found.add(_F.PHASE_SESSIONS_NOT_DISTINCT)
    for phase, activated, elapsed in ((off, a_off, e_off), (on, a_on, e_on)):
        result = phase.finalisation
        unique = len(phase.finalisations) == 1
        if unique and result is not None and result.session_id != activated.session_id:
            found.add(_F.FINALISATION_SESSION_MISMATCH)
        if elapsed.scheduled_end_monotonic != activated.scheduled_end_monotonic:
            found.add(_F.SCHEDULED_END_MISMATCH)
        indices = [tick.tick for tick in phase.ticks]
        if elapsed.tick_count != len(indices) or indices != list(range(len(indices))):
            found.add(_F.TICK_COUNT_MISMATCH)
        start, end = activated.event_monotonic_seconds, elapsed.event_monotonic_seconds
        observed = (*phase.ticks, *phase.hosts)
        if any(not start <= record.monotonic_seconds <= end for record in observed):
            found.add(_F.TICK_OUTSIDE_WINDOW)
        if len(phase.hosts) != len(phase.ticks):
            found.add(_F.HOST_EVIDENCE_MISMATCH)
    scheduled_end = typing.cast(float, a_off.scheduled_end_monotonic)
    if (
        measured.transition_start_monotonic != scheduled_end
        or measured.event_monotonic_seconds != a_on.event_monotonic_seconds
    ):
        found.add(_F.TRANSITION_NOT_BOUND)
    duration = a_on.event_monotonic_seconds - scheduled_end
    if not (
        0.0 <= duration <= COLD_TRANSITION_BUDGET_SECONDS
        and measured.transition_budget_seconds == COLD_TRANSITION_BUDGET_SECONDS
        and measured.transition_within_budget is True
    ):
        found.add(_F.TRANSITION_BUDGET_EXCEEDED)
    chain = [
        (off.header.monotonic_seconds, a_off.event_monotonic_seconds),
        (on.header.monotonic_seconds, a_on.event_monotonic_seconds),
        (e_off.event_monotonic_seconds, f_off.event_monotonic_seconds),
        (f_off.event_monotonic_seconds, cs_off.event_monotonic_seconds),
        (cs_off.event_monotonic_seconds, ct_off.event_monotonic_seconds),
        (ct_off.event_monotonic_seconds, on.header.monotonic_seconds),
        (e_on.event_monotonic_seconds, f_on.event_monotonic_seconds),
        (f_on.event_monotonic_seconds, cs_on.event_monotonic_seconds),
        (cs_on.event_monotonic_seconds, terminal.event_monotonic_seconds),
    ]
    for phase, elapsed, returned in ((off, e_off, f_off), (on, e_on, f_on)):
        for record in phase.finalisations:
            chain.append((elapsed.event_monotonic_seconds, record.monotonic_seconds))
            chain.append((record.monotonic_seconds, returned.monotonic_seconds))
    if any(not earlier <= later for earlier, later in chain):
        found.add(_F.CAUSAL_ORDER_VIOLATED)
    retained = [
        record
        for phase in (off, on)
        for record in (
            phase.header,
            *phase.ticks,
            *phase.hosts,
            *phase.finalisations,
            *phase.aborts,
            *phase.advisories,
        )
    ]
    if any(record.monotonic_seconds > terminal.event_monotonic_seconds for record in retained):
        found.add(_F.RECORD_AFTER_TERMINATION)


def _replay_ticks(
    interpretation: ColdInterpretation,
    snapshots: _Snapshots,
    found: set[ColdConformanceFinding],
) -> None:
    """Replay every in-window retained tick through the imported ``evaluate_tick``.

    A tick before its activation instant is skipped rather than replayed with a
    negative since-activation input; the window rule refuses it independently.
    """
    for phase, activated in zip(
        interpretation.rebound.phases, (snapshots[0], snapshots[5]), strict=True
    ):
        driver = _exact_map(_identity_document(phase)["runtime_config"])["roaster_driver"]
        if type(driver) is not str:  # pragma: no cover - read_identity_v1 requires an exact str.
            found.add(_F.IDENTITY_DELTA_NOT_ADMITTED)
            continue
        previous: float | None = None
        for tick in phase.ticks:
            since = tick.monotonic_seconds - activated.event_monotonic_seconds
            if since < 0.0:
                continue
            decision = evaluate_tick(
                tick,
                established_session_id=typing.cast(str, activated.session_id),
                frozen_driver=driver,
                since_activation_seconds=since,
                previous_elapsed=previous,
            )
            if decision.reasons:
                found.add(_F.RETAINED_TICK_POLICY_VIOLATED)
            previous = decision.next_previous_elapsed_seconds


def _result(found: set[ColdConformanceFinding]) -> ColdConformanceResult:
    """Return the closed result listing findings uniquely in declaration order."""
    findings = tuple(member for member in ColdConformanceFinding if member in found)
    return ColdConformanceResult(
        policy_version=CONFORMANCE_POLICY_VERSION,
        outcome=(
            ColdConformanceOutcome.NOT_CONFORMANT
            if findings
            else ColdConformanceOutcome.PRE_ADVISORY_CONFORMANT
        ),
        findings=findings,
    )


def check_pre_advisory_conformance(run: object) -> ColdConformanceResult:
    """Check one retained v2 run against the pre-advisory conformance policy v1.

    The run conforms if and only if no finding is recorded.  A rejection, parser
    refusal or internal exception never conforms; ``BaseException`` that is not an
    ``Exception`` propagates.  The result carries enums and the version only.

    Args:
        run: A candidate ``ColdRetainedRunV2``, as the strict v2 reader returns it.

    Returns:
        The closed conformance result.
    """
    found: set[ColdConformanceFinding] = set()
    if not _guarded(found, _F.CARRIER_NOT_ADMITTED, lambda: _admit(run)):
        return _result({_F.CARRIER_NOT_ADMITTED})
    carrier = typing.cast(ColdRetainedRunV2, run)
    interpretation = _guarded(
        found, _F.INTERPRETATION_REFUSED, lambda: interpret_retained_run(carrier.run)
    )
    if interpretation is None:
        return _result({_F.INTERPRETATION_REFUSED})
    internal = _F.CHECKER_INTERNAL_FAILURE
    _guarded(found, internal, lambda: _check_phases(interpretation, found))
    _guarded(
        found, _F.IDENTITY_DELTA_NOT_ADMITTED, lambda: _check_identity_delta(interpretation, found)
    )
    snapshots = _guarded(
        found,
        _F.LIFECYCLE_NOT_ADMITTED,
        lambda: _admit_lifecycle_evidence(carrier, interpretation, found),
    )
    if snapshots is not None and _guarded(
        found, internal, lambda: _check_lifecycle_fields(snapshots, found)
    ):
        _guarded(found, internal, lambda: _check_matched(interpretation, snapshots, found))
        _guarded(found, internal, lambda: _replay_ticks(interpretation, snapshots, found))
    return _result(found)
