"""Two-phase cold-characterisation orchestration (#954 slice 4g-c).

One run identity, one evidence directory and two MCP sessions: recording-off
observation for 1800 s, clean D195 finalisation, a confirmed child stop, a
respawn with the recording-on configuration, recording-on observation for
1800 s, clean finalisation, a confirmed final stop and one terminal record.  The
orchestrator owns only the child it starts, admits every runtime carrier before
use, enforces run-wide write discipline the v1 grammar cannot prove, and returns
closed members and a manifest digest only.

D195 finalisation can perform safe-zero and disconnect; it is not a
non-actuating operation.  It runs only for a completed, unaborted phase after
its session is re-admitted.  This module adds no actuator control and no
emergency stop.

``PRE_ADVISORY_CONFORMANT`` requires a completed terminal record, a confirmed
final stop, a sealed digest, a reload through ``read_retained_run_v2`` and an
admitted conformant policy-v1 result.  It is never qualification, readiness,
hardware acceptance, advisory acceptance or recording-on acceptance.

The transition budget runs from the recording-off ``PHASE_ACTIVATED`` scheduled
end (which includes any overrun) to recording-on activation, and is checked at
every transition checkpoint and at activation, before the first recording-on
read.  It never applies after recording-on activation.

Residuals: per-tick observation covers heat, roast fan and cooling; main fan,
drum and solenoid are checked only at D195 finalisation.  Clock progress is a
port contract with no watchdog: a stall that never resumes leaves the run
unfinished, and a resumed stall can leave sparse ticks in a completed phase, so
no continuous observation is claimed.  A finite temperature is not a
plausibility claim.  An independent operator emergency stop is required.
``session_id=None`` or ``NOT_ADMITTED`` never proves that no session exists; a
process stop or a failed append never proves a safe commanded state; v1 cannot
prove append provenance beyond the discipline enforced here.  Cleanup is bounded
only by the child port's own stop contract; a stalled dependency is not
guaranteed to finish.
"""

import asyncio
import enum
import math
import typing

import pydantic

from roastpilot_agent.cold_characterisation.conformance import (
    ColdConformanceFinding,
    ColdConformanceOutcome,
    ColdConformanceResult,
    check_pre_advisory_conformance,
    masked_identity_text,
)
from roastpilot_agent.cold_characterisation.engine import (
    ColdAbortClassification,
    ColdActivationHook,
    ColdAdmissionFailure,
    ColdAdmissionRefusedError,
    ColdEngineClock,
    ColdEngineHost,
    ColdEngineMcp,
    ColdEngineUnexpectedError,
    ColdEvidenceIncompleteError,
    ColdPhaseAborted,
    ColdPhaseActivationRefused,
    ColdPhaseAdmission,
    ColdPhaseCompleted,
    admit_cold_phase,
    observe_cold_phase,
    open_phase_evidence,
)
from roastpilot_agent.cold_characterisation.evidence_builders import (
    build_finalisation_record,
    build_lifecycle_record,
)
from roastpilot_agent.cold_characterisation.evidence_lifecycle import (
    COLD_TRANSITION_BUDGET_SECONDS,
    ColdLifecycleChildStart,
    ColdLifecycleChildStop,
    ColdLifecycleEvent,
    ColdLifecycleFinalisationResult,
    ColdLifecycleRecord,
    ColdRunTermination,
    ColdRunTerminationReason,
    is_admissible_monotonic,
    is_admissible_session_id,
    is_admissible_utc_instant,
    validate_lifecycle_record,
)
from roastpilot_agent.cold_characterisation.evidence_reader import read_retained_run_v2
from roastpilot_agent.cold_characterisation.evidence_schema import (
    MAX_COLLECTION_LENGTH,
    MAX_ENVELOPE_BYTES,
    MAX_INT_DIGITS,
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    ColdAbortDomain,
    ColdAbortRecord,
    ColdAdvisoryRecord,
    ColdEngineAbortReason,
    ColdEvidenceFailure,
    ColdEvidenceRecord,
    ColdFinalisationRecord,
    ColdHostAbortReason,
    ColdPhaseKind,
    ColdRunHeader,
    validate_record,
)
from roastpilot_agent.cold_characterisation.evidence_store import (
    ColdAdmittedRoot,
    ColdEvidenceWriter,
    canonical_json,
)
from roastpilot_agent.cold_characterisation.identity import (
    AgentBuildProvenance,
    ColdArtefactKind,
    ColdRunIdentity,
    EffectiveMCPProfile,
    ManagedDeviceIdentity,
    ModelManifestEntry,
)
from roastpilot_agent.cold_characterisation.mcp import (
    ColdFinalisationNotCleanError,
    ColdFinalisationSafetyError,
    DisconnectEvidence,
    DriverCommandStateEvidence,
    DriverEvidenceRead,
    FinalisationFailure,
    FinalisationFirstCrackStatus,
    FinalisationStageResult,
    FirstCrackRuntimeFinalisationEvidence,
    RecordingArtifact,
    RecordingFinalisationEvidence,
    RejectionReason,
    SamplerFinalisationEvidence,
    SessionFinalisationResult,
    finalisation_has_required_safety_evidence,
    finalisation_is_clean,
)
from roastpilot_agent.mcp_client import RuntimeConfigSnapshot, ServerInfo

_M = typing.TypeVar("_M", bound=pydantic.BaseModel)
_OFF: typing.Final = ColdPhaseKind.RECORDING_OFF
_ON: typing.Final = ColdPhaseKind.RECORDING_ON
_E: typing.TypeAlias = ColdLifecycleEvent
_R: typing.TypeAlias = ColdRunTerminationReason
_INT_BOUND: typing.Final = 10**MAX_INT_DIGITS - 1
_HEX: typing.Final = frozenset("0123456789abcdef")

# ------------------------------------------------------------------ ports


class ColdTwoPhaseMcp(ColdEngineMcp, typing.Protocol):
    """The engine's five non-actuating operations plus D195 finalisation."""

    async def finalise_session(self, session_id: str) -> SessionFinalisationResult:
        """Finalise one explicit cold session (may safe-zero and disconnect)."""
        ...


class ColdChildLifecycle(typing.Protocol):
    """One MCP child process owning two pre-bound phase configurations."""

    async def start(self) -> None:
        """Spawn the child with the selected configuration."""
        ...

    async def stop(self) -> None:
        """Stop the child."""
        ...

    def configure_phase(self, phase: ColdPhaseKind) -> None:
        """Select the pre-bound configuration for the next spawn; no MCP call."""
        ...

    @property
    def running(self) -> bool:
        """Whether a child session is attached."""
        ...

    @property
    def stop_unconfirmed(self) -> bool:
        """Whether a teardown could not be confirmed (sticky)."""
        ...


class ColdPhaseIdentitySource(typing.Protocol):
    """Freezes one phase identity after a confirmed child start."""

    async def freeze(self, phase: ColdPhaseKind) -> ColdRunIdentity:
        """Return the frozen identity for ``phase``."""
        ...


# ----------------------------------------------------------------- results


class ColdTwoPhaseOutcome(enum.Enum):
    """Closed run outcome; never qualification, readiness or acceptance."""

    PRE_ADVISORY_CONFORMANT = "pre_advisory_conformant"
    NOT_CONFORMANT = "not_conformant"
    EVIDENCE_NOT_SEALED = "evidence_not_sealed"
    REFUSED_BEFORE_EVIDENCE = "refused_before_evidence"


class ColdRunStartRefusal(enum.Enum):
    """Closed reasons a run was refused before any evidence writer existed."""

    CHILD_ALREADY_RUNNING = "child_already_running"
    CHILD_STOP_UNCONFIRMED_AT_ENTRY = "child_stop_unconfirmed_at_entry"
    CHILD_START_FAILED = "child_start_failed"
    IDENTITY_NOT_FROZEN = "identity_not_frozen"
    ADMISSION_REFUSED = "admission_refused"
    EVIDENCE_OPEN_FAILED = "evidence_open_failed"
    UNEXPECTED_FAILURE = "unexpected_failure"


class ColdChildOwnership(enum.Enum):
    """What this invocation knows of the child it may have started.

    ``NOT_OWNED`` says only that this invocation never attempted a start; it
    never proves that no child exists.
    """

    NOT_OWNED = "not_owned"
    OWNED_STOP_CONFIRMED = "owned_stop_confirmed"
    OWNED_STOP_UNCONFIRMED = "owned_stop_unconfirmed"


_ENTRY_REFUSALS: typing.Final = (
    ColdRunStartRefusal.CHILD_ALREADY_RUNNING,
    ColdRunStartRefusal.CHILD_STOP_UNCONFIRMED_AT_ENTRY,
)
_STARTED_REFUSALS: typing.Final = (
    ColdRunStartRefusal.IDENTITY_NOT_FROZEN,
    ColdRunStartRefusal.ADMISSION_REFUSED,
    ColdRunStartRefusal.EVIDENCE_OPEN_FAILED,
)


def _is_member(value: object, kind: type[enum.Enum]) -> bool:
    """Whether ``value`` is, by identity, a real member of ``kind``."""
    return type(value) is kind and any(value is member for member in kind)


def _is_digest(value: object) -> bool:
    """Whether ``value`` is an exact 64-character lowercase hex ``str``."""
    return type(value) is str and len(value) == 64 and all(char in _HEX for char in value)


class ColdTwoPhaseResult(pydantic.BaseModel):
    """The closed run result: enum members, a digest and an admitted checker result."""

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    outcome: ColdTwoPhaseOutcome
    start_refusal: ColdRunStartRefusal | None
    termination_reason: ColdRunTerminationReason | None
    child_ownership: ColdChildOwnership
    manifest_sha256: str | None
    conformance: ColdConformanceResult | None

    @pydantic.field_validator("manifest_sha256", mode="before")
    @classmethod
    def _require_digest(cls, value: object) -> object:
        """Admit ``None`` or an exact lowercase SHA-256 hex string only."""
        if value is None or _is_digest(value):
            return value
        raise ValueError("manifest digest not admitted")

    @pydantic.field_validator("conformance", mode="before")
    @classmethod
    def _require_admitted_conformance(cls, value: object) -> object:
        """Replace a checker result with its admitted fresh snapshot, or refuse it."""
        if value is None:
            return None
        fresh = _admit_carrier(value, ColdConformanceResult, _CHECKER)
        if fresh is None:
            raise ValueError("conformance result not admitted")
        return fresh

    @pydantic.model_validator(mode="after")
    def _require_closed_row(self) -> typing.Self:
        """Admit exactly the four closed outcome rows."""
        if not _row_admits(self):
            raise ValueError("result fields do not form a closed row")
        return self


def _row_admits(result: ColdTwoPhaseResult) -> bool:
    """Whether the result's exact members form one closed outcome row."""
    outcome, refusal = result.outcome, result.start_refusal
    reason, owner = result.termination_reason, result.child_ownership
    digest, checked = result.manifest_sha256, result.conformance
    if not (
        _is_member(outcome, ColdTwoPhaseOutcome)
        and _is_member(owner, ColdChildOwnership)
        and (refusal is None or _is_member(refusal, ColdRunStartRefusal))
        and (reason is None or _is_member(reason, ColdRunTerminationReason))
    ):
        return False
    owned = owner is not ColdChildOwnership.NOT_OWNED
    conformant = (
        checked is not None and checked.outcome is ColdConformanceOutcome.PRE_ADVISORY_CONFORMANT
    )
    if outcome is ColdTwoPhaseOutcome.PRE_ADVISORY_CONFORMANT:
        return (
            refusal is None
            and reason is None
            and owner is ColdChildOwnership.OWNED_STOP_CONFIRMED
            and digest is not None
            and conformant
        )
    if outcome is ColdTwoPhaseOutcome.NOT_CONFORMANT:
        return refusal is None and owned and digest is not None and not conformant
    if outcome is ColdTwoPhaseOutcome.EVIDENCE_NOT_SEALED:
        return refusal is None and owned and digest is None and checked is None
    if refusal is None or reason is not None or digest is not None or checked is not None:
        return False
    if any(refusal is member for member in _ENTRY_REFUSALS):
        return not owned
    return owned or not any(refusal is member for member in _STARTED_REFUSALS)


class ColdRunSinkRefusedError(RuntimeError):
    """Fixed refusal of the guarded run sink; carries nothing from its input."""

    def __init__(self) -> None:
        """Create the fixed refusal."""
        super().__init__("Cold run evidence refused.")


# ------------------------------------------------------- carrier admission


class _Carrier(typing.NamedTuple):
    """One closed admission table: models with field names, enums with members."""

    models: tuple[tuple[type[pydantic.BaseModel], tuple[str, ...]], ...]
    enums: tuple[tuple[type[enum.Enum], tuple[enum.Enum, ...]], ...]


def _carrier(
    models: tuple[type[pydantic.BaseModel], ...], enums: tuple[type[enum.Enum], ...]
) -> _Carrier:
    """Precompute one closed table from trusted classes."""
    return _Carrier(
        tuple((model, tuple(model.model_fields)) for model in models),
        tuple((kind, tuple(kind)) for kind in enums),
    )


_IDENTITY: typing.Final = _carrier(
    (
        ColdRunIdentity,
        RuntimeConfigSnapshot,
        ServerInfo,
        ManagedDeviceIdentity,
        AgentBuildProvenance,
        EffectiveMCPProfile,
        ModelManifestEntry,
    ),
    (ColdArtefactKind,),
)
_FINALISATION: typing.Final = _carrier(
    (
        SessionFinalisationResult,
        FinalisationStageResult,
        FinalisationFailure,
        DriverEvidenceRead,
        DriverCommandStateEvidence,
        SamplerFinalisationEvidence,
        FinalisationFirstCrackStatus,
        FirstCrackRuntimeFinalisationEvidence,
        RecordingFinalisationEvidence,
        RecordingArtifact,
        DisconnectEvidence,
    ),
    (RejectionReason,),
)
_ENGINE: typing.Final = _carrier(
    (ColdPhaseCompleted, ColdPhaseAborted, ColdPhaseActivationRefused, ColdAbortClassification),
    (ColdAbortDomain, ColdHostAbortReason, ColdEvidenceFailure, ColdEngineAbortReason),
)
_CHECKER: typing.Final = _carrier(
    (ColdConformanceResult,), (ColdConformanceOutcome, ColdConformanceFinding)
)
_ENGINE_ROOTS: typing.Final[tuple[type[pydantic.BaseModel], ...]] = (
    ColdPhaseCompleted,
    ColdPhaseAborted,
    ColdPhaseActivationRefused,
)

_Parent: typing.TypeAlias = dict[str, object] | list[object]
_Children: typing.TypeAlias = list[tuple[str | None, object]]


def _exact_values(data: object, names: tuple[str, ...]) -> tuple[object, ...] | None:
    """Return exactly the named values of an exact ``dict`` with exact ``str`` keys."""
    if type(data) is not dict:
        return None
    raw = typing.cast(dict[object, object], data)
    if len(raw) != len(names) or not all(type(key) is str for key in raw):
        return None
    if not all(name in raw for name in names):
        return None
    return tuple(raw[name] for name in names)


def _model_values(node: object, names: tuple[str, ...]) -> tuple[object, ...] | None:
    """Read one exact model's declared values; both metadata slots must be ``None``."""
    extra: object = object.__getattribute__(node, "__pydantic_extra__")
    private: object = object.__getattribute__(node, "__pydantic_private__")
    if extra is not None or private is not None:
        return None
    return _exact_values(object.__getattribute__(node, "__dict__"), names)


def _scalar(value: object) -> int | None:
    """Return the charged size of one exact admitted JSON scalar, or ``None``."""
    kind = type(value)
    if value is None or kind is bool:
        return 5
    if kind is int:
        return MAX_INT_DIGITS + 1 if -_INT_BOUND <= typing.cast(int, value) <= _INT_BOUND else None
    if kind is float:
        return 32 if math.isfinite(typing.cast(float, value)) else None
    if kind is str:
        text = typing.cast(str, value)
        return len(text.encode("utf-8")) if len(text) <= MAX_ENVELOPE_BYTES else None
    return None


def _copy_node(
    current: object, table: _Carrier, visited: set[int]
) -> tuple[object, _Children, int] | None:
    """Copy one node into a fresh primitive: ``(copy, children, size)``, or ``None``."""
    size = _scalar(current)
    if size is not None:
        return current, [], size
    for kind, members in table.enums:
        if type(current) is kind:
            member = next((item for item in members if item is current), None)
            return None if member is None else (member.value, [], 32)
    names = next((names for model, names in table.models if type(current) is model), None)
    kind = type(current)
    if names is None and not (kind is dict or kind is list or kind is tuple):
        return None
    sized = typing.cast(typing.Sized, current)
    if names is not None or len(sized) > 0:
        if id(current) in visited:
            return None
        visited.add(id(current))
    if names is not None:
        values = _model_values(current, names)
        if values is None:
            return None
        return {}, list(zip(names, values, strict=True)), 0
    if len(sized) > MAX_COLLECTION_LENGTH:
        return None
    if kind is dict:
        mapping = typing.cast(dict[object, object], current)
        if not all(type(key) is str for key in mapping):
            return None
        keys = typing.cast(list[str], list(mapping))
        size = sum(len(key.encode("utf-8")) for key in keys)
        return {}, [(key, mapping[key]) for key in keys], size
    return [], [(None, item) for item in typing.cast(tuple[object, ...], current)], 0


def _snapshot(value: object, root: type[pydantic.BaseModel], table: _Carrier) -> object | None:
    """Build a new JSON-native snapshot of declared admitted fields, or ``None``.

    The walk is iterative and bounded by the settled depth, node, collection and
    envelope-byte limits.  Every model and non-empty container may be visited at
    most once; a repeated visit refuses immediately (empty containers cannot form
    a cycle).  Classes and enum members are matched by identity scans; nothing
    caller-defined is hashed, compared, dumped or copied.
    """
    if type(value) is not root:
        return None
    holder: list[object] = []
    stack: list[tuple[object, int, _Parent, str | None]] = [(value, 0, holder, None)]
    visited: set[int] = set()
    nodes = size = 0
    while stack:
        current, depth, parent, slot = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            return None
        copied = _copy_node(current, table, visited)
        if copied is None:
            return None
        node, children, charge = copied
        size += charge
        if size > MAX_ENVELOPE_BYTES:
            return None
        if slot is None:
            typing.cast(list[object], parent).append(node)
        else:
            typing.cast(dict[str, object], parent)[slot] = node
        fresh = typing.cast(_Parent, node)
        stack.extend((child, depth + 1, fresh, key) for key, child in reversed(children))
    return holder[0]


def _admit_carrier(value: object, root: type[_M], table: _Carrier) -> _M | None:
    """Return a fresh, losslessly re-validated instance of ``root``, or ``None``.

    The snapshot is canonicalised, validated with ``model_validate_json`` in strict
    mode, and must round-trip to the identical canonical text from a fresh dump:
    no coercion or dropped input is silently accepted.  No input or error text is
    emitted.
    """
    try:
        snapshot = _snapshot(value, root, table)
        if snapshot is None:
            return None
        text = canonical_json(snapshot)
        fresh = root.model_validate_json(text, strict=True)
        lossless = canonical_json(fresh.model_dump(mode="json")) == text
    except Exception:
        return None
    return fresh if lossless else None


def _error_values(error: object, names: tuple[str, ...]) -> tuple[object, ...] | None:
    """Read the named attributes of an exact error from its exact ``__dict__`` only."""
    try:
        return _exact_values(object.__getattribute__(error, "__dict__"), names)
    except Exception:
        return None


def _admission_failure(error: object) -> ColdAdmissionFailure | None:
    """Return the admitted closed member of an exact admission/evidence error, or ``None``."""
    if (
        type(error) is not ColdAdmissionRefusedError
        and type(error) is not ColdEvidenceIncompleteError
    ):
        return None
    values = _error_values(error, ("failure",))
    if values is None or not _is_member(values[0], ColdAdmissionFailure):
        return None
    return typing.cast(ColdAdmissionFailure, values[0])


def _unexpected_session(error: object) -> str | None:
    """Return the admitted session retained by an exact unexpected engine error."""
    if type(error) is not ColdEngineUnexpectedError:
        return None
    values = _error_values(error, ("abort_recorded", "session_id"))
    if values is None or type(values[0]) is not bool or not is_admissible_session_id(values[1]):
        return None
    return typing.cast(str, values[1])


def _finalisation_error_result(error: object) -> SessionFinalisationResult | None:
    """Return the admitted result carried by an exact D195 finalisation error."""
    if (
        type(error) is not ColdFinalisationNotCleanError
        and type(error) is not ColdFinalisationSafetyError
    ):
        return None
    values = _error_values(error, ("result",))
    if values is None:
        return None
    return _admit_carrier(values[0], SessionFinalisationResult, _FINALISATION)


def _same_session(left: object, right: object) -> bool:
    """Whether both values are admitted exact session strings and equal (no normalisation)."""
    return is_admissible_session_id(left) and is_admissible_session_id(right) and left == right


# ------------------------------------------------------------- clock ledger


class _Instant(typing.NamedTuple):
    """One admitted clock instant."""

    monotonic: float
    utc: str


class _ClockFloor:
    """Run-wide monotonic floor across orchestration boundaries; ties are admitted."""

    def __init__(self) -> None:
        self.floor: float | None = None

    def observe_fact(self, monotonic: object, utc: object) -> _Instant | None:
        """Admit one instant at or after the floor and advance it; else ``None``."""
        if not (is_admissible_monotonic(monotonic) and is_admissible_utc_instant(utc)):
            return None
        instant = _Instant(typing.cast(float, monotonic), typing.cast(str, utc))
        if self.floor is not None and instant.monotonic < self.floor:
            return None
        self.floor = instant.monotonic
        return instant

    def sample(self, clock: ColdEngineClock) -> _Instant | None:
        """Sample the clock once (no resampling); ``None`` when refused."""
        try:
            monotonic: object = clock.monotonic()
            utc: object = clock.utc_now_iso()
        except Exception:
            return None
        return self.observe_fact(monotonic, utc)


# -------------------------------------------------------- guarded run sink


class _PhaseGuard:
    """Write-discipline facts retained for one phase."""

    def __init__(self) -> None:
        self.header = False
        self.child_started = False
        self.window_closed = False
        self.elapsed = False
        self.aborted_not_finalised = False
        self.finalisation_appended = False
        self.finalisation_returned = False
        self.abort_seen = False


class _RunSink:
    """Run-private guard over the single writer; a poisoned sink may only be closed."""

    def __init__(self, writer: ColdEvidenceWriter) -> None:
        self._writer = writer
        self._guards = {_OFF: _PhaseGuard(), _ON: _PhaseGuard()}
        self.phase: ColdPhaseKind | None = None
        self.next_sequence = 0
        self.terminated = False
        self.poisoned = False

    @property
    def usable(self) -> bool:
        """Whether another append may be attempted."""
        return not self.poisoned and not self.terminated

    def header_bound(self, phase: ColdPhaseKind) -> bool:
        """Whether ``phase`` has a bound header."""
        return self._guards[phase].header

    def abort_retained(self, phase: ColdPhaseKind) -> bool:
        """Whether any v1 abort (any of the seven domains) was retained for ``phase``."""
        return self._guards[phase].abort_seen

    def finalisation_eligible(self, phase: ColdPhaseKind) -> bool:
        """Whether D195 finalisation of ``phase`` may be attempted now."""
        guard = self._guards[phase]
        return (
            self.usable
            and self.phase is phase
            and guard.header
            and guard.elapsed
            and not guard.abort_seen
            and not guard.aborted_not_finalised
            and not guard.finalisation_appended
            and not guard.finalisation_returned
        )

    def _refuse(self) -> typing.NoReturn:
        """Poison the guard and raise the fixed refusal."""
        self.poisoned = True
        raise ColdRunSinkRefusedError

    def _write(self, action: typing.Callable[[], object]) -> None:
        """Run one writer call; any fault poisons and raises the fixed refusal."""
        failed = False
        try:
            action()
        except Exception:
            failed = True
        if failed:
            self._refuse()

    def _admits(self, record: ColdEvidenceRecord) -> bool:
        """Apply R4-R8 to one fresh v1 snapshot."""
        if type(record) is ColdRunHeader:
            if self.phase is None:
                return record.phase is _OFF
            off = self._guards[_OFF]
            return (
                self.phase is _OFF
                and record.phase is _ON
                and off.child_started
                and not self._guards[_ON].header
            )
        if self.phase is None or record.phase is not self.phase:
            return False
        guard = self._guards[self.phase]
        if type(record) is ColdAdvisoryRecord:
            return False
        if type(record) is ColdFinalisationRecord:
            return guard.elapsed and not (
                guard.finalisation_appended
                or guard.finalisation_returned
                or guard.aborted_not_finalised
                or guard.abort_seen
            )
        return not guard.window_closed

    def append(self, record: ColdEvidenceRecord) -> None:
        """Admit, guard and durably append one v1 record (the engine's sink port).

        Raises:
            ColdRunSinkRefusedError: On any refusal or writer fault (then poisoned).
        """
        if not self.usable:
            self._refuse()
        snapshot: ColdEvidenceRecord | None = None
        try:
            snapshot = validate_record(record)
        except Exception:
            snapshot = None
        if snapshot is None or not self._admits(snapshot):
            self._refuse()
        fresh = snapshot
        self._write(lambda: self._writer.append(fresh))
        if type(fresh) is ColdRunHeader:
            self.phase = fresh.phase
            self._guards[fresh.phase].header = True
        elif type(fresh) is ColdAbortRecord:
            self._guards[fresh.phase].abort_seen = True
        elif type(fresh) is ColdFinalisationRecord:
            self._guards[fresh.phase].finalisation_appended = True

    def _admits_lifecycle(self, record: ColdLifecycleRecord) -> bool:
        """Apply R3, R5 and R9-R11 to one fresh lifecycle snapshot."""
        if self.phase is None or record.phase is not self.phase:
            return False
        guard = self._guards[self.phase]
        if record.event is _E.FINALISATION_RETURNED:
            result = record.finalisation_result
            recorded = (
                result is ColdLifecycleFinalisationResult.CLEAN_RECORDED
                or result is ColdLifecycleFinalisationResult.NOT_CLEAN_RECORDED
            )
            return guard.finalisation_appended is recorded
        if record.event is _E.PHASE_ABORTED_NOT_FINALISED:
            return not guard.finalisation_appended
        if record.event is _E.OBSERVATION_WINDOW_ELAPSED:
            return not guard.abort_seen
        return True

    def append_lifecycle(self, record: ColdLifecycleRecord) -> None:
        """Admit, guard and durably append one lifecycle record.

        Raises:
            ColdRunSinkRefusedError: On any refusal or writer fault (then poisoned).
        """
        if not self.usable:
            self._refuse()
        snapshot: ColdLifecycleRecord | None = None
        try:
            snapshot = validate_lifecycle_record(record)
        except Exception:
            snapshot = None
        if snapshot is None or not self._admits_lifecycle(snapshot):
            self._refuse()
        fresh = snapshot
        self._write(lambda: self._writer.append_lifecycle(fresh))
        self.next_sequence += 1
        guard = self._guards[fresh.phase]
        if fresh.event is _E.OBSERVATION_WINDOW_ELAPSED:
            guard.window_closed = guard.elapsed = True
        elif fresh.event is _E.PHASE_ABORTED_NOT_FINALISED:
            guard.window_closed = guard.aborted_not_finalised = True
        elif fresh.event is _E.FINALISATION_RETURNED:
            guard.finalisation_returned = True
        elif fresh.event is _E.CHILD_STARTED:
            guard.child_started = True
        elif fresh.event is _E.RUN_TERMINATED:
            self.terminated = True

    def seal(self) -> str:
        """Seal a terminated run with a bound header and return its manifest digest.

        Raises:
            ColdRunSinkRefusedError: If poisoned, not terminated, unbound or sealing
                fails (then poisoned).
        """
        if self.poisoned or not self.terminated or self.phase is None:
            self._refuse()
        digest: object = None
        try:
            digest = self._writer.seal().manifest_sha256
        except Exception:
            digest = None
        if not _is_digest(digest):
            self._refuse()
        return typing.cast(str, digest)

    def close(self) -> None:
        """Poison and release the writer; a close fault is absorbed."""
        self.poisoned = True
        try:
            self._writer.close()
        except Exception:
            return


# -------------------------------------------------------- child ownership


class _ChildState(enum.Enum):
    """Private closed child-ownership state."""

    UNOWNED = "unowned"
    STARTING = "starting"
    RUNNING_CONFIRMED = "running_confirmed"
    STOP_IN_FLIGHT = "stop_in_flight"
    STOPPED_CONFIRMED = "stopped_confirmed"
    STOP_UNCONFIRMED = "stop_unconfirmed"
    STOP_INTERRUPTED = "stop_interrupted"


class _ChildOwner:
    """Fail-closed state machine over the consumer-owned child port."""

    def __init__(self, port: ColdChildLifecycle) -> None:
        self._port = port
        self.state = _ChildState.UNOWNED
        self._interrupted_cleanup_attempted = False

    def flags(self) -> tuple[object, object] | None:
        """Read ``running`` and ``stop_unconfirmed`` once each; ``None`` if either raises."""
        try:
            running: object = self._port.running
            unconfirmed: object = self._port.stop_unconfirmed
        except Exception:
            return None
        return running, unconfirmed

    def _observed(self, running: bool) -> bool:
        """Whether ``running`` is exactly ``running`` and ``stop_unconfirmed`` exactly False."""
        flags = self.flags()
        return flags is not None and flags[0] is running and flags[1] is False

    @property
    def ownership(self) -> ColdChildOwnership:
        """The public ownership claim; nothing upgrades an uncertain stop."""
        if self.state is _ChildState.UNOWNED:
            return ColdChildOwnership.NOT_OWNED
        if self.state is _ChildState.STOPPED_CONFIRMED:
            return ColdChildOwnership.OWNED_STOP_CONFIRMED
        return ColdChildOwnership.OWNED_STOP_UNCONFIRMED

    @property
    def needs_stop(self) -> bool:
        """Whether one owned stop is still required."""
        if self.state is _ChildState.STOP_INTERRUPTED:
            return not self._interrupted_cleanup_attempted
        return self.state is _ChildState.STARTING or self.state is _ChildState.RUNNING_CONFIRMED

    def _may_start(self) -> bool:
        """Configure/start only before any start or after a confirmed stop."""
        return self.state is _ChildState.UNOWNED or self.state is _ChildState.STOPPED_CONFIRMED

    def configure(self, phase: ColdPhaseKind) -> bool:
        """Select a pre-bound configuration; ``False`` if refused or it raises."""
        if not self._may_start():
            return False
        try:
            self._port.configure_phase(phase)
        except Exception:
            return False
        return True

    async def start(self) -> bool:
        """Start once and require exact ``running`` True and ``stop_unconfirmed`` False."""
        if not self._may_start() or not self._observed(False):
            return False
        self.state = _ChildState.STARTING
        try:
            await self._port.start()
        except Exception:
            return False
        if not self._observed(True):
            return False
        self.state = _ChildState.RUNNING_CONFIRMED
        return True

    async def stop(self) -> None:
        """Stop at most once; a returned or raised stop is never retried."""
        if self.state is _ChildState.STOP_INTERRUPTED:
            if not self._interrupted_cleanup_attempted:
                self._interrupted_cleanup_attempted = True
                await self._attempt_stop()
            return
        if not self.needs_stop:
            return
        self.state = _ChildState.STOP_IN_FLIGHT
        try:
            await self._port.stop()
        except asyncio.CancelledError:
            self.state = _ChildState.STOP_INTERRUPTED
            raise
        except Exception:
            self.state = _ChildState.STOP_UNCONFIRMED
            return
        confirmed = self._observed(False)
        self.state = _ChildState.STOPPED_CONFIRMED if confirmed else _ChildState.STOP_UNCONFIRMED

    async def _attempt_stop(self) -> bool:
        """One cleanup stop after an interrupted stop; it never upgrades ownership."""
        try:
            await self._port.stop()
        except Exception:
            return False
        return True

    async def shielded_cleanup(self) -> None:
        """Run at most one owned cleanup stop to completion, absorbing cancellation."""
        if not self.needs_stop:
            return
        task = asyncio.ensure_future(self.stop())
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.exception()


# ------------------------------------------------------------ orchestrator


class _HookFailed(Exception):
    """Internal signal: an activation hook could not record its facts."""


class _TwoPhaseRun:
    """State of one two-phase run; never shared between runs."""

    def __init__(
        self,
        *,
        root: ColdAdmittedRoot,
        mcp: ColdTwoPhaseMcp,
        child: ColdChildLifecycle,
        identities: ColdPhaseIdentitySource,
        host: ColdEngineHost,
        clock: ColdEngineClock,
    ) -> None:
        self._root = root
        self._mcp = mcp
        self._child = _ChildOwner(child)
        self._identities = identities
        self._host = host
        self._clock = clock
        self._ledger = _ClockFloor()
        self._sink: _RunSink | None = None
        self._primary: ColdRunTerminationReason | None = None
        self._refusal = ColdRunStartRefusal.UNEXPECTED_FAILURE
        self._headers: dict[ColdPhaseKind, ColdRunHeader] = {}
        self._sessions: dict[ColdPhaseKind, str] = {}
        self._ends: dict[ColdPhaseKind, float] = {}

    # ------------------------------------------------------------ helpers

    def _fail(self, reason: ColdRunTerminationReason) -> None:
        """Record the first failure; later failures never overwrite it."""
        if self._primary is None:
            self._primary = reason

    def _sample(self) -> _Instant | None:
        """Take one admitted ledger sample, or record ``CLOCK_INVALID``."""
        instant = self._ledger.sample(self._clock)
        if instant is None:
            self._fail(_R.CLOCK_INVALID)
        return instant

    def _lifecycle(
        self, event: ColdLifecycleEvent, at: _Instant, **fields: typing.Any
    ) -> ColdLifecycleRecord | None:
        """Append one lifecycle record at a fresh recording instant after ``at``."""
        sink = self._sink
        if sink is None or not sink.usable or sink.phase is None:
            return None
        recorded = self._sample()
        if recorded is None:
            return None
        record = build_lifecycle_record(
            header=self._headers[sink.phase],
            sequence=sink.next_sequence,
            event=event,
            event_utc=at.utc,
            event_monotonic_seconds=at.monotonic,
            recorded_at_utc=recorded.utc,
            monotonic_seconds=recorded.monotonic,
            **fields,
        )
        try:
            sink.append_lifecycle(record)
        except ColdRunSinkRefusedError:
            return None
        return record

    def _checkpoint(self) -> bool:
        """Sample once and require the OFF-end-to-now interval within the budget."""
        at = self._sample()
        if at is None:
            return False
        if at.monotonic - self._ends[_OFF] > COLD_TRANSITION_BUDGET_SECONDS:
            self._fail(_R.TRANSITION_BUDGET_EXCEEDED)
            return False
        return True

    # -------------------------------------------------------------- hooks

    def _activated(
        self, phase: ColdPhaseKind, session: object, mono: object, utc: object
    ) -> _Instant:
        """Re-admit hook arguments, then record ``PHASE_ACTIVATED`` for ``phase``."""
        if not (
            is_admissible_session_id(session)
            and is_admissible_monotonic(mono)
            and is_admissible_utc_instant(utc)
        ):
            raise _HookFailed
        at = self._ledger.observe_fact(mono, utc)
        if at is None:
            self._fail(_R.CLOCK_INVALID)
            raise _HookFailed
        self._sessions[phase] = typing.cast(str, session)
        record = self._lifecycle(_E.PHASE_ACTIVATED, at, session_id=session)
        if record is None:
            raise _HookFailed
        self._ends[phase] = typing.cast(float, record.scheduled_end_monotonic)
        return at

    def _off_hook(self, *, session_id: str, activated_monotonic: float, activated_utc: str) -> bool:
        """Recording-off activation: record the activation and its scheduled end."""
        self._activated(_OFF, session_id, activated_monotonic, activated_utc)
        return True

    def _on_hook(self, *, session_id: str, activated_monotonic: float, activated_utc: str) -> bool:
        """Recording-on activation: record it and the transition, then apply the budget."""
        at = self._activated(_ON, session_id, activated_monotonic, activated_utc)
        start = self._ends[_OFF]
        measured = self._lifecycle(
            _E.TRANSITION_MEASURED,
            at,
            session_id=session_id,
            previous_phase_session_id=self._sessions[_OFF],
            transition_start_monotonic=start,
        )
        if measured is None:
            raise _HookFailed
        return at.monotonic - start <= COLD_TRANSITION_BUDGET_SECONDS

    # -------------------------------------------------------------- phases

    async def _freeze(self, phase: ColdPhaseKind) -> ColdRunIdentity | None:
        """Freeze and admit one phase identity, or ``None``."""
        try:
            raw: object = await self._identities.freeze(phase)
        except Exception:
            return None
        return _admit_carrier(raw, ColdRunIdentity, _IDENTITY)

    async def _admit(
        self, identity: ColdRunIdentity, phase: ColdPhaseKind
    ) -> ColdPhaseAdmission | ColdAdmissionFailure | None:
        """Admit one phase at or after the ledger floor; keep its fresh header."""
        try:
            admission = await admit_cold_phase(
                identity=identity,
                phase=phase,
                root=self._root,
                mcp=self._mcp,
                host=self._host,
                clock=self._clock,
                not_before_monotonic=self._ledger.floor,
            )
        except Exception as error:
            return _admission_failure(error)
        self._headers[phase] = typing.cast(ColdRunHeader, validate_record(admission.header))
        return admission

    async def _observe(
        self,
        phase: ColdPhaseKind,
        admission: ColdPhaseAdmission,
        hook: ColdActivationHook,
    ) -> str | None:
        """Observe one phase; return its session only when it may be finalised."""
        sink = typing.cast(_RunSink, self._sink)
        raw: object = None
        reported: str | None = None
        try:
            raw = await observe_cold_phase(
                admission=admission,
                sink=sink,
                mcp=self._mcp,
                host=self._host,
                clock=self._clock,
                activation_hook=hook,
            )
        except Exception as error:
            reported = _unexpected_session(error)
        fresh = next(
            (
                result
                for root in _ENGINE_ROOTS
                if (result := _admit_carrier(raw, root, _ENGINE)) is not None
            ),
            None,
        )
        if type(fresh) is ColdPhaseCompleted and not sink.abort_retained(phase):
            return self._completed(phase, fresh)
        if type(fresh) is ColdPhaseAborted:
            session = fresh.session_id if is_admissible_session_id(fresh.session_id) else None
            return self._not_finalised(phase, session, _R.PHASE_ABORTED, deadline=False)
        if type(fresh) is ColdPhaseCompleted:
            return self._not_finalised(phase, None, _R.PHASE_ABORTED, deadline=False)
        if type(fresh) is ColdPhaseActivationRefused and phase is _ON:
            return self._not_finalised(phase, None, _R.TRANSITION_BUDGET_EXCEEDED, deadline=True)
        return self._not_finalised(phase, reported, _R.PHASE_FAILED_UNEXPECTEDLY, deadline=False)

    def _not_finalised(
        self, phase: ColdPhaseKind, reported: str | None, reason: _R, *, deadline: bool
    ) -> None:
        """Record the primary and ``PHASE_ABORTED_NOT_FINALISED``; never finalise."""
        self._fail(reason)
        session = self._sessions.get(phase, reported)
        at = self._sample()
        if at is not None:
            self._lifecycle(
                _E.PHASE_ABORTED_NOT_FINALISED,
                at,
                session_id=session,
                activation_deadline_exceeded=deadline,
            )
        return None

    def _completed(self, phase: ColdPhaseKind, result: ColdPhaseCompleted) -> str | None:
        """Admit a completed window, record it elapsed, and return its session."""
        session = self._sessions.get(phase)
        if session is None or not _same_session(session, result.session_id):
            return self._not_finalised(phase, None, _R.PHASE_FAILED_UNEXPECTEDLY, deadline=False)
        at = self._ledger.observe_fact(result.observation_end_monotonic, result.observation_end_utc)
        if at is None:
            return self._not_finalised(phase, None, _R.CLOCK_INVALID, deadline=False)
        if result.tick_count == 0:
            self._fail(_R.PHASE_FAILED_UNEXPECTEDLY)
        elapsed = self._lifecycle(
            _E.OBSERVATION_WINDOW_ELAPSED,
            at,
            session_id=session,
            scheduled_end_monotonic=self._ends[phase],
            tick_count=result.tick_count,
        )
        return None if elapsed is None else session

    async def _finalise(self, phase: ColdPhaseKind, session: str) -> bool:
        """Finalise one eligible phase once; ``True`` only for a recorded clean result."""
        sink = typing.cast(_RunSink, self._sink)
        if not sink.finalisation_eligible(phase):
            return False
        outcome: SessionFinalisationResult | None = None
        try:
            returned: object = await self._mcp.finalise_session(session)
        except Exception as error:
            outcome = _finalisation_error_result(error)
        else:
            outcome = _admit_carrier(returned, SessionFinalisationResult, _FINALISATION)
        at = self._sample()
        if at is None:
            return False
        Result = ColdLifecycleFinalisationResult
        if outcome is None or not _same_session(session, outcome.session_id):
            self._fail(_R.FINALISATION_FAILED)
            self._lifecycle(
                _E.FINALISATION_RETURNED,
                at,
                session_id=session,
                finalisation_result=Result.FAILED_WITHOUT_RESULT,
            )
            return False
        retained = False
        try:
            record = build_finalisation_record(
                header=self._headers[phase],
                result=outcome,
                recorded_at_utc=at.utc,
                monotonic_seconds=at.monotonic,
            )
            sink.append(record)
            retained = True
        except Exception:
            retained = False
        if not retained:
            self._fail(_R.FINALISATION_RECORD_NOT_RETAINED)
            self._lifecycle(
                _E.FINALISATION_RETURNED,
                at,
                session_id=session,
                finalisation_result=Result.RECORD_NOT_RETAINED,
            )
            return False
        clean = (
            outcome.session_purpose == "cold_characterisation"
            and finalisation_is_clean(outcome)
            and finalisation_has_required_safety_evidence(outcome)
        )
        if not clean:
            self._fail(_R.FINALISATION_NOT_CLEAN)
        returned_record = self._lifecycle(
            _E.FINALISATION_RETURNED,
            at,
            session_id=session,
            finalisation_result=Result.CLEAN_RECORDED if clean else Result.NOT_CLEAN_RECORDED,
        )
        return clean and returned_record is not None

    async def _stop(self) -> bool:
        """Stop the owned child once and record ``CHILD_STOPPED``; ``True`` if confirmed."""
        await self._child.stop()
        confirmed = self._child.state is _ChildState.STOPPED_CONFIRMED
        if not confirmed:
            self._fail(_R.CHILD_STOP_UNCONFIRMED)
        at = self._sample()
        if at is None:
            return False
        Stop = ColdLifecycleChildStop
        stopped = self._lifecycle(
            _E.CHILD_STOPPED, at, child_stop=Stop.CONFIRMED if confirmed else Stop.UNCONFIRMED
        )
        return confirmed and stopped is not None

    async def _respawn(self) -> bool:
        """Configure and start the recording-on child after a confirmed stop."""
        if not self._child.configure(_ON):
            self._fail(_R.RESPAWN_FAILED)
            return False
        started = await self._child.start()
        if not started:
            self._fail(_R.RESPAWN_FAILED)
        at = self._sample()
        if at is None:
            return False
        Start = ColdLifecycleChildStart
        recorded = self._lifecycle(
            _E.CHILD_STARTED, at, child_start=Start.STARTED if started else Start.FAILED
        )
        return started and recorded is not None and self._checkpoint()

    def _delta_admitted(self) -> bool:
        """Whether both fresh headers' identities differ only in the five masked leaves."""
        off, on = self._headers[_OFF], self._headers[_ON]
        try:
            masked_off = masked_identity_text(off.identity, False)
            masked_on = masked_identity_text(on.identity, True)
        except Exception:
            return False
        return masked_on is not None and masked_on == masked_off and on.run_id == off.run_id

    async def _admit_on(self) -> ColdPhaseAdmission | None:
        """Freeze, admit and delta-check the recording-on phase within the budget."""
        identity = await self._freeze(_ON)
        if identity is None:
            self._fail(_R.RECORDING_ON_IDENTITY_REFUSED)
            return None
        if not self._checkpoint():
            return None
        admitted = await self._admit(identity, _ON)
        if type(admitted) is not ColdPhaseAdmission:
            if admitted is ColdAdmissionFailure.MCP_READ_FAILED:
                self._fail(_R.RECONNECT_FAILED)
            elif admitted is ColdAdmissionFailure.CLOCK_INVALID:
                self._fail(_R.CLOCK_INVALID)
            else:
                self._fail(_R.PHASE_ADMISSION_REFUSED)
            return None
        if not self._delta_admitted():
            self._fail(_R.PHASE_IDENTITY_DELTA_NOT_ADMITTED)
            return None
        return admitted if self._checkpoint() else None

    async def _run_off(self, admission: ColdPhaseAdmission) -> bool:
        """Observe, finalise and stop the recording-off phase."""
        session = await self._observe(_OFF, admission, self._off_hook)
        if session is None or not self._checkpoint():
            return False
        if not await self._finalise(_OFF, session) or self._primary is not None:
            return False
        return self._checkpoint() and await self._stop() and self._checkpoint()

    async def _run_on(self) -> None:
        """Respawn, admit, observe, finalise and finally stop the recording-on phase."""
        if not await self._respawn():
            return
        admission = await self._admit_on()
        if admission is None:
            return
        session = await self._observe(_ON, admission, self._on_hook)
        if session is not None and await self._finalise(_ON, session):
            await self._stop()

    # ----------------------------------------------------------- sequence

    def _refused(self, refusal: ColdRunStartRefusal) -> ColdTwoPhaseResult:
        """Return the refusal row with the current ownership."""
        return ColdTwoPhaseResult(
            outcome=ColdTwoPhaseOutcome.REFUSED_BEFORE_EVIDENCE,
            start_refusal=refusal,
            termination_reason=None,
            child_ownership=self._child.ownership,
            manifest_sha256=None,
            conformance=None,
        )

    def _entry_refusal(self) -> ColdRunStartRefusal | None:
        """Refuse a running, uncertain or unreadable child before creating anything."""
        flags = self._child.flags()
        if flags is None or type(flags[0]) is not bool or type(flags[1]) is not bool:
            return ColdRunStartRefusal.UNEXPECTED_FAILURE
        if flags[0]:
            return ColdRunStartRefusal.CHILD_ALREADY_RUNNING
        if flags[1]:
            return ColdRunStartRefusal.CHILD_STOP_UNCONFIRMED_AT_ENTRY
        return None

    async def _sequence(self) -> ColdTwoPhaseResult:
        """Run the whole sequence; every failure ends in teardown."""
        refusal = self._entry_refusal()
        if refusal is not None:
            return self._refused(refusal)
        if not self._child.configure(_OFF):
            return self._refused(ColdRunStartRefusal.CHILD_START_FAILED)
        self._refusal = ColdRunStartRefusal.CHILD_START_FAILED
        if not await self._child.start():
            return await self._teardown()
        self._refusal = ColdRunStartRefusal.IDENTITY_NOT_FROZEN
        identity = await self._freeze(_OFF)
        if identity is None:
            return await self._teardown()
        self._refusal = ColdRunStartRefusal.ADMISSION_REFUSED
        admission = await self._admit(identity, _OFF)
        if type(admission) is not ColdPhaseAdmission:
            return await self._teardown()
        self._refusal = ColdRunStartRefusal.EVIDENCE_OPEN_FAILED
        try:
            writer = open_phase_evidence(admission)
        except Exception:
            return await self._teardown()
        self._sink = _RunSink(writer)
        if await self._run_off(admission):
            await self._run_on()
        return await self._teardown()

    async def _teardown(self) -> ColdTwoPhaseResult:
        """Cleanup stop, ``CHILD_STOPPED``, terminal, seal, reload and check."""
        sink = self._sink
        if self._child.needs_stop:
            if sink is None:
                await self._child.stop()
            else:
                await self._stop()
        if sink is None:
            return self._refused(self._refusal)
        at = self._sample()
        if at is not None:
            primary = self._primary
            Termination = ColdRunTermination
            self._lifecycle(
                _E.RUN_TERMINATED,
                at,
                termination=Termination.COMPLETED if primary is None else Termination.FAILED,
                termination_reason=primary,
            )
        return self._end(sink)

    def _result(
        self,
        outcome: ColdTwoPhaseOutcome,
        digest: str | None = None,
        conformance: ColdConformanceResult | None = None,
    ) -> ColdTwoPhaseResult:
        """Build one evidence-bearing result row from admitted closed fields."""
        return ColdTwoPhaseResult(
            outcome=outcome,
            start_refusal=None,
            termination_reason=self._primary,
            child_ownership=self._child.ownership,
            manifest_sha256=digest,
            conformance=conformance,
        )

    def _end(self, sink: _RunSink) -> ColdTwoPhaseResult:
        """Seal a terminated run, reload it by its digest and check it."""
        if sink.poisoned or not sink.terminated:
            sink.close()
            return self._result(ColdTwoPhaseOutcome.EVIDENCE_NOT_SEALED)
        try:
            digest = sink.seal()
        except ColdRunSinkRefusedError:
            sink.close()
            return self._result(ColdTwoPhaseOutcome.EVIDENCE_NOT_SEALED)
        try:
            retained = read_retained_run_v2(
                self._root.path,
                run_id=self._headers[_OFF].run_id,
                expected_manifest_sha256=digest,
            )
            checked: object = check_pre_advisory_conformance(retained)
        except Exception:
            checked = None
        conformance = _admit_carrier(checked, ColdConformanceResult, _CHECKER)
        conformant = (
            conformance is not None
            and conformance.outcome is ColdConformanceOutcome.PRE_ADVISORY_CONFORMANT
        )
        if (
            conformant
            and self._primary is None
            and self._child.ownership is ColdChildOwnership.OWNED_STOP_CONFIRMED
        ):
            return ColdTwoPhaseResult(
                outcome=ColdTwoPhaseOutcome.PRE_ADVISORY_CONFORMANT,
                start_refusal=None,
                termination_reason=None,
                child_ownership=ColdChildOwnership.OWNED_STOP_CONFIRMED,
                manifest_sha256=digest,
                conformance=conformance,
            )
        return self._result(
            ColdTwoPhaseOutcome.NOT_CONFORMANT, digest, None if conformant else conformance
        )

    async def execute(self) -> ColdTwoPhaseResult:
        """Run once; on cancellation or another ``BaseException`` clean up and re-raise."""
        try:
            try:
                return await self._sequence()
            except Exception:
                self._fail(_R.UNEXPECTED_FAILURE)
            self._refusal = ColdRunStartRefusal.UNEXPECTED_FAILURE
            return await self._teardown()
        except BaseException:
            if self._sink is not None:
                self._sink.close()
            await self._child.shielded_cleanup()
            raise


async def run_two_phase_characterisation(
    *,
    root: ColdAdmittedRoot,
    mcp: ColdTwoPhaseMcp,
    child: ColdChildLifecycle,
    identities: ColdPhaseIdentitySource,
    host: ColdEngineHost,
    clock: ColdEngineClock,
) -> ColdTwoPhaseResult:
    """Run one hardware-free-testable two-phase cold characterisation.

    The same ``mcp``, ``child``, ``host`` and ``clock`` instances are used for both
    phases.  The child is started only from a confirmed idle state and never
    respawned after any failure or uncertainty; the evidence directory is created
    once, for the recording-off phase only.

    Args:
        root: The admitted evidence root.
        mcp: The cold MCP port (five non-actuating reads plus D195 finalisation).
        child: The consumer-owned child lifecycle port.
        identities: The phase identity source, called after each confirmed start.
        host: The host-bound port.
        clock: The engine clock.

    Returns:
        The closed result; ``PRE_ADVISORY_CONFORMANT`` is never qualification.

    Raises:
        asyncio.CancelledError: After owned cleanup, with no terminal and no seal.
    """
    run = _TwoPhaseRun(
        root=root, mcp=mcp, child=child, identities=identities, host=host, clock=clock
    )
    return await run.execute()
