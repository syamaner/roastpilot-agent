"""Two-phase cold-characterisation orchestration (#954 slices 4g-c and 5c-ii-b).

One run identity, one evidence directory and two MCP sessions: recording-off
observation for 600 s, clean D195 finalisation, a confirmed child stop, a
respawn with the recording-on configuration, recording-on observation for
600 s, clean finalisation, a confirmed final stop and one terminal record.  The
orchestrator owns only the child it starts, admits every runtime carrier before
use, enforces run-wide write discipline the v1 grammar cannot prove, and returns
closed members and a manifest digest only.

D195 finalisation verifies that all six command dimensions are already
safe-zero, then performs lifecycle teardown and driver disconnect (on the
Hottop driver this stops the command loop and closes serial).  It is a
non-actuating lifecycle operation, not a read-only one: it cannot command
safe-zero, and unknown, unreadable or non-zero state is rejected before
teardown without any actuator, stop-cooling or emergency-stop call.  It runs
only for a completed, unaborted phase after its session is re-admitted;
skipping it skips that verification and clean disconnect, never a promised
safe-zero command.  This module adds no actuator control and no emergency stop.

Each activated phase also runs the observation-only advisory sampler through one
run-owned :class:`ColdAdvisoryRunOwner`: it is started from the activation hook,
settled synchronously as soon as the engine returns or raises, and is typed only to
the advisory-attempt sink and retained-tick ports (both served by the run's guarded
sink); it receives no MCP, actuator or control capability.  A settlement that
leaves an attempt unresolved or unrecorded, or a provider task outstanding, fails
the run (OD5): the current phase is still finalised only when otherwise eligible,
the child stop is attempted, no next phase starts, and the run ends with either the
schema-3 failed-run terminal (an unresolved or unrecorded attempt) or the v2
``FAILED`` termination (a provider task outstanding with no open attempt).  Exactly
one synchronous provider observation follows the seal step on that path; it is an
exit signal for the caller only, never a stop or delivery proof.

``ADVISORY_CONFORMANT`` requires a completed terminal record, a confirmed final
stop, no advisory failure path, a sealed digest, a reload through the V6 reader
and an admitted conformant D210 policy-4 result (which shares the unchanged advisory,
lifecycle and temperature checks with the historical policies). It is never
qualification, readiness, hardware acceptance or recording-on acceptance.

D209 activation: the operator-asserted reviewed MCP candidate is re-admitted before
any child action, each frozen phase identity must name its reported version, and
one candidate record is retained per phase at its activation instant before any
tick.  Every tick is retained with one paired tick-temperature record from the
same read; the guarded sink holds the pair pending between the two writes, so
neither seal nor finalisation can follow an unpaired tick, and a temperature
screen reason ends the phase with a retained abort and no finalisation.

The transition budget runs from the recording-off ``PHASE_ACTIVATED`` scheduled
end (which includes any overrun) to recording-on activation, and is checked at
every transition checkpoint and at activation, before the first recording-on
read.  It never applies after recording-on activation.

Residuals: per-tick software observation checks commanded heat, main fan, roast
fan and cooling.  Drum and solenoid/drop appear only in eligible D195
six-dimension finalisation evidence.  These are commanded software values, not
physical sensing or proof of physical response.  Clock progress is a
port contract with no watchdog: a stall that never resumes leaves the run
unfinished, and a resumed stall can leave sparse ticks in a completed phase, so
no continuous observation is claimed.  The D209 temperature screen (bean and
environment temperatures within 5 to 40 °C from the 60-second startup boundary,
accepted-packet progress, Celsius, raw/typed agreement, no newly counted fault) is
engineering screening, not calibration or physical-safety evidence: progress is
arrival between observations, not a watchdog; agreement is consistency, not
corroboration; ``observed`` does not imply liveness; bad-checksum frames the driver
skips without counting remain invisible; and the tick and its temperature are two
durable writes, not a transaction (a kill between them leaves an unsealed tree).
The candidate is an operator assertion that never attests installed bytes.  An
independent operator emergency stop is required.
``session_id=None`` or ``NOT_ADMITTED`` never proves that no session exists; a
process stop or a failed append never proves a safe commanded state; v1 cannot
prove append provenance beyond the discipline enforced here.  Cleanup is bounded
only by the child port's own stop contract; a stalled dependency is not
guaranteed to finish.

An optional :class:`ColdRetainedTickObserver` receives a freshly re-admitted copy of
each durably retained tick, synchronously and on the run's loop, only after the
tick's paired temperature record is also durable (so publication follows one more
durable write, and a tick whose pair failed is never published).  It is
display-only: an observer failure records ``UNEXPECTED_FAILURE`` with no retained
abort, stops further publication and fails the run; it is never a liveness,
freshness or safety signal and never authorises a phase, a finalisation or an exit.
"""

import asyncio
import enum
import math
import typing

import pydantic

from roastpilot_agent.cold_characterisation.advisory_run_owner import (
    ColdAdvisoryPhaseObservation,
    ColdAdvisoryPhaseRun,
    ColdAdvisoryPhaseSettlement,
    ColdAdvisoryPhaseStart,
    ColdAdvisoryRunOwner,
    ColdAdvisoryRunTaskState,
)
from roastpilot_agent.cold_characterisation.advisory_sampler import (
    ColdAdvisoryAdvisorPort,
    ColdAdvisoryCallFact,
    ColdAdvisoryCancellationRequest,
    ColdAdvisoryEvaluatorPort,
    ColdAdvisoryProviderObservation,
    ColdAdvisoryProviderTask,
    ColdAdvisoryProviderTaskState,
    ColdAdvisorySamplerStop,
    ColdAdvisorySettlement,
    ColdAdvisorySettlementClosure,
    ColdAdvisorySpec,
)
from roastpilot_agent.cold_characterisation.conformance import masked_identity_text
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
    build_mcp_candidate_record,
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
from roastpilot_agent.cold_characterisation.evidence_reader import read_retained_run_v6
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
    ColdEvidenceError,
    ColdEvidenceFailure,
    ColdEvidenceRecord,
    ColdFinalisationRecord,
    ColdHostAbortReason,
    ColdPhaseKind,
    ColdRunHeader,
    ColdTickAudioSample,
    ColdTickDeviceEvidence,
    ColdTickRecord,
    ColdTickRoastFanEvidence,
    ColdTickRoastFanOutcome,
    ColdTickSessionEvidence,
    ColdTickSessionPhase,
    validate_record,
)
from roastpilot_agent.cold_characterisation.evidence_store import (
    ColdAdmittedRoot,
    ColdEvidenceWriter,
    canonical_json,
)
from roastpilot_agent.cold_characterisation.evidence_temperature import (
    ColdTickTemperatureRecord,
    pairs_with,
    validate_tick_temperature_record,
)
from roastpilot_agent.cold_characterisation.evidence_temperature_run import (
    ColdMcpCandidateProvenance,
    ColdMcpCandidateRecord,
    ColdTemperatureAbortRecord,
    ColdTemperatureScreenReason,
    readmit_mcp_candidate_provenance,
    validate_mcp_candidate_record,
    validate_temperature_abort_record,
)
from roastpilot_agent.cold_characterisation.evidence_terminal import (
    ColdFailedRunAdvisorySettlement,
    ColdFailedRunProviderCancellation,
    ColdFailedRunTerminalRecord,
    build_failed_run_terminal_record,
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
from roastpilot_agent.cold_characterisation.temperature_conformance import (
    ColdRevisedConformanceResult,
    ColdTemperatureConformanceFinding,
    ColdTemperatureConformanceOutcome,
    check_revised_conformance,
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


class ColdRetainedTickObserver(typing.Protocol):
    """Display-only observer of each durably retained tick (#954 U2).

    It is called synchronously on the run's own event loop, after the tick and its
    paired tick-temperature record are both durable, with an exclusive freshly
    re-admitted copy that shares nothing with the
    retained record.  It must not block, perform I/O or await, and must return
    ``None``.  Any raised ``Exception`` or non-``None`` return fails the run
    (``UNEXPECTED_FAILURE``, no retained abort) and stops further calls.  It is
    never a liveness, freshness, continuity or safety signal.
    """

    def __call__(self, tick: ColdTickRecord, /) -> None:
        """Observe one retained tick copy."""
        ...


class ColdTwoPhaseMcp(ColdEngineMcp, typing.Protocol):
    """The engine's non-actuating operations plus D195 finalisation.

    The engine operations are the identity reads, cold-session start, activation
    and tick reads; finalisation verifies already-safe-zero state, then tears
    down and disconnects (non-actuating, not read-only).
    """

    async def finalise_session(self, session_id: str) -> SessionFinalisationResult:
        """Finalise one cold session: verify already-safe-zero state, then disconnect."""
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

    ADVISORY_CONFORMANT = "advisory_conformant"
    NOT_CONFORMANT = "not_conformant"
    EVIDENCE_NOT_SEALED = "evidence_not_sealed"
    REFUSED_BEFORE_EVIDENCE = "refused_before_evidence"


class ColdTwoPhaseAdvisoryPath(enum.Enum):
    """Which OD5 advisory failure path, if any, the run took.

    ``FAILED_RUN_TERMINAL``: a stored settlement left an attempt unresolved or
    unrecorded; the run ends with the schema-3 failed-run terminal.
    ``PROVIDER_OUTSTANDING_FAILED``: a provider task was outstanding at settlement
    with no such attempt; the run ends with the v2 ``FAILED`` termination.  Neither
    proves delivery, cancellation, a stop or any physical state.
    """

    NOT_APPLICABLE = "not_applicable"
    FAILED_RUN_TERMINAL = "failed_run_terminal"
    PROVIDER_OUTSTANDING_FAILED = "provider_outstanding_failed"


class ColdTwoPhaseProviderCheck(enum.Enum):
    """The one post-seal provider observation of an OD5 path.

    Only ``PENDING_AT_CHECK`` is an exit signal.  ``NOT_PENDING_AT_CHECK`` means only
    that the most recent published provider task was observed not outstanding; it
    never claims that no task or remote call exists.  ``NOT_OBSERVABLE_AT_CHECK``
    means the observation was refused, absent, not admitted or raised.
    """

    NOT_CHECKED = "not_checked"
    PENDING_AT_CHECK = "pending_at_check"
    NOT_PENDING_AT_CHECK = "not_pending_at_check"
    NOT_OBSERVABLE_AT_CHECK = "not_observable_at_check"


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
    never proves that no child exists.  ``OWNED_STOP_CONFIRMED`` confirms only
    that the stop of the child this invocation owned was confirmed; it never
    proves that no child currently exists or that any actuator is in a safe state.
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
    """The closed run result: enum members, a digest and an admitted policy-4 checker result."""

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    outcome: ColdTwoPhaseOutcome
    start_refusal: ColdRunStartRefusal | None
    termination_reason: ColdRunTerminationReason | None
    child_ownership: ColdChildOwnership
    manifest_sha256: str | None
    conformance: ColdRevisedConformanceResult | None
    advisory_path: ColdTwoPhaseAdvisoryPath
    provider_check: ColdTwoPhaseProviderCheck

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
        fresh = _admit_carrier(value, ColdRevisedConformanceResult, _CHECKER, flat_identity=True)
        if fresh is None:
            raise ValueError("conformance result not admitted")
        return fresh

    @pydantic.model_validator(mode="after")
    def _require_closed_row(self) -> typing.Self:
        """Admit exactly the closed outcome rows."""
        if not _row_admits(self):
            raise ValueError("result fields do not form a closed row")
        return self


def _row_admits(result: ColdTwoPhaseResult) -> bool:
    """Whether the result's exact members form one closed outcome row."""
    outcome, refusal = result.outcome, result.start_refusal
    reason, owner = result.termination_reason, result.child_ownership
    digest, checked = result.manifest_sha256, result.conformance
    path, check = result.advisory_path, result.provider_check
    if not (
        _is_member(outcome, ColdTwoPhaseOutcome)
        and _is_member(owner, ColdChildOwnership)
        and _is_member(path, ColdTwoPhaseAdvisoryPath)
        and _is_member(check, ColdTwoPhaseProviderCheck)
        and (refusal is None or _is_member(refusal, ColdRunStartRefusal))
        and (reason is None or _is_member(reason, ColdRunTerminationReason))
    ):
        return False
    if path is ColdTwoPhaseAdvisoryPath.NOT_APPLICABLE:
        if check is not ColdTwoPhaseProviderCheck.NOT_CHECKED:
            return False
    elif not _advisory_path_admits(result):
        return False
    owned = owner is not ColdChildOwnership.NOT_OWNED
    conformant = (
        checked is not None
        and checked.outcome is ColdTemperatureConformanceOutcome.TEMPERATURE_SCREENED_CONFORMANT
    )
    if outcome is ColdTwoPhaseOutcome.ADVISORY_CONFORMANT:
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


def _advisory_path_admits(result: ColdTwoPhaseResult) -> bool:
    """The extra facts every row on an OD5 path carries.

    The run failed (a primary reason is set) with retained or attempted evidence,
    the one provider check was taken (AC7/D203: one of the three ``*_AT_CHECK``
    values, never ``NOT_CHECKED``), and a failed-run terminal carries no checker
    result (it is verified, never checked).
    """
    outcome = result.outcome
    if not (
        outcome is ColdTwoPhaseOutcome.NOT_CONFORMANT
        or outcome is ColdTwoPhaseOutcome.EVIDENCE_NOT_SEALED
    ):
        return False
    if (
        result.termination_reason is None
        or result.provider_check is ColdTwoPhaseProviderCheck.NOT_CHECKED
    ):
        return False
    terminal = result.advisory_path is ColdTwoPhaseAdvisoryPath.FAILED_RUN_TERMINAL
    return not (terminal and result.conformance is not None)


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
    (
        ColdAbortDomain,
        ColdHostAbortReason,
        ColdEvidenceFailure,
        ColdEngineAbortReason,
        ColdTemperatureScreenReason,
    ),
)
_CHECKER: typing.Final = _carrier(
    (ColdRevisedConformanceResult,),
    (ColdTemperatureConformanceOutcome, ColdTemperatureConformanceFinding),
)
_ADVISORY: typing.Final = _carrier(
    (
        ColdAdvisoryPhaseSettlement,
        ColdAdvisorySettlement,
        ColdAdvisoryPhaseRun,
        ColdAdvisoryPhaseObservation,
        ColdAdvisoryProviderObservation,
    ),
    (
        ColdAdvisorySettlementClosure,
        ColdAdvisoryProviderTask,
        ColdAdvisoryRunTaskState,
        ColdAdvisorySamplerStop,
        ColdAdvisoryCancellationRequest,
        ColdAdvisoryPhaseStart,
        ColdAdvisoryProviderTaskState,
        ColdAdvisoryCallFact,
    ),
)
_TICK: typing.Final = _carrier(
    (
        ColdTickRecord,
        ColdTickDeviceEvidence,
        ColdTickRoastFanEvidence,
        ColdTickSessionEvidence,
        ColdTickAudioSample,
    ),
    (ColdPhaseKind, ColdTickRoastFanOutcome, ColdTickSessionPhase),
)
_ENGINE_ROOTS: typing.Final[tuple[type[pydantic.BaseModel], ...]] = (
    ColdPhaseCompleted,
    ColdPhaseAborted,
    ColdPhaseActivationRefused,
)

# ----------------------------------------------------- OD5 advisory policy

_Closure: typing.TypeAlias = ColdAdvisorySettlementClosure
_Cancel: typing.TypeAlias = ColdAdvisoryCancellationRequest
#: The five stored closures that leave an attempt unresolved or unrecorded, each
#: mapped by identity to its schema-3 failed-run terminal member.
_TERMINAL_SETTLEMENTS: typing.Final[
    tuple[tuple[ColdAdvisorySettlementClosure, ColdFailedRunAdvisorySettlement], ...]
] = (
    (
        _Closure.RECORDED_UNRESOLVED_INVOKED,
        ColdFailedRunAdvisorySettlement.RECORDED_UNRESOLVED_INVOKED,
    ),
    (
        _Closure.RECORDED_UNRESOLVED_NOT_INVOKED,
        ColdFailedRunAdvisorySettlement.RECORDED_UNRESOLVED_NOT_INVOKED,
    ),
    (
        _Closure.NOT_RECORDED_CLOCK_INVALID,
        ColdFailedRunAdvisorySettlement.NOT_RECORDED_CLOCK_INVALID,
    ),
    (_Closure.NOT_RECORDED_SINK_REFUSED, ColdFailedRunAdvisorySettlement.NOT_RECORDED_SINK_REFUSED),
    (
        _Closure.NOT_RECORDED_COMPLETION_UNKNOWN,
        ColdFailedRunAdvisorySettlement.NOT_RECORDED_COMPLETION_UNKNOWN,
    ),
)
#: The six storable settlement-time cancellation requests, mapped by identity.
_TERMINAL_CANCELLATIONS: typing.Final[
    tuple[tuple[ColdAdvisoryCancellationRequest, ColdFailedRunProviderCancellation], ...]
] = (
    (_Cancel.NO_PROVIDER_TASK, ColdFailedRunProviderCancellation.NO_PROVIDER_TASK),
    (_Cancel.TASK_ALREADY_DONE, ColdFailedRunProviderCancellation.TASK_ALREADY_DONE),
    (_Cancel.REQUESTED, ColdFailedRunProviderCancellation.REQUESTED),
    (_Cancel.REQUEST_NOT_ACCEPTED, ColdFailedRunProviderCancellation.REQUEST_NOT_ACCEPTED),
    (_Cancel.REQUEST_RAISED, ColdFailedRunProviderCancellation.REQUEST_RAISED),
    (_Cancel.REQUEST_INTERRUPTED, ColdFailedRunProviderCancellation.REQUEST_INTERRUPTED),
)


def _terminal_settlement(
    closure: ColdAdvisorySettlementClosure,
) -> ColdFailedRunAdvisorySettlement | None:
    """The failed-run terminal member for one of the five closures, by identity."""
    return next((member for stored, member in _TERMINAL_SETTLEMENTS if closure is stored), None)


def _terminal_cancellation(
    request: ColdAdvisoryCancellationRequest,
) -> ColdFailedRunProviderCancellation | None:
    """The failed-run terminal member for one storable cancellation request, by identity."""
    return next((member for stored, member in _TERMINAL_CANCELLATIONS if request is stored), None)


def _od5_triggered(settlement: ColdAdvisorySettlement) -> bool:
    """OD5: an outstanding provider task, or one of the five closures, at settlement.

    ``NO_OPEN_ATTEMPT`` alone never establishes resolution, and a completed call
    recorded while its task is still outstanding is a contradiction that triggers.
    """
    return (
        settlement.provider_task is ColdAdvisoryProviderTask.OUTSTANDING
        or _terminal_settlement(settlement.closure) is not None
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
        size = 0
        for key in keys:
            if len(key) > MAX_ENVELOPE_BYTES:
                return None
            size += len(key.encode("utf-8"))
            if size > MAX_ENVELOPE_BYTES:
                return None
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


def _validated_flat(value: object, root: type[_M], table: _Carrier) -> _M:
    """Strictly validate one flat root from the exact declared values of the original.

    For a root whose own validators admit enum members and tuples by identity only,
    so a JSON round trip can never satisfy them: those validators judge the real
    values, already bounded and identity-checked by the snapshot walk.

    Raises:
        Exception: If the root is not in the table, its values cannot be read, or
            strict validation refuses them (the caller refuses the carrier).
    """
    names = next(names for model, names in table.models if model is root)
    values = typing.cast(tuple[object, ...], _model_values(value, names))
    return root.model_validate(dict(zip(names, values, strict=True)), strict=True)


def _admit_carrier(
    value: object, root: type[_M], table: _Carrier, *, flat_identity: bool = False
) -> _M | None:
    """Return a fresh, losslessly re-validated instance of ``root``, or ``None``.

    The snapshot is canonicalised, validated with ``model_validate_json`` in strict
    mode, and must round-trip to the identical canonical text from a fresh dump:
    no coercion or dropped input is silently accepted.  No input or error text is
    emitted.  With ``flat_identity`` (a flat root whose validators admit members and
    tuples by identity) the admitted original's declared values are validated in
    strict Python mode instead (see :func:`_validated_flat`); the same lossless round
    trip to the snapshot text is required.
    """
    try:
        snapshot = _snapshot(value, root, table)
        if snapshot is None:
            return None
        text = canonical_json(snapshot)
        fresh = (
            _validated_flat(value, root, table)
            if flat_identity
            else root.model_validate_json(text, strict=True)
        )
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


def _record_phase(record: object) -> ColdPhaseKind | None:
    """Read a record's ``phase`` slot from its exact ``__dict__`` by identity, or ``None``.

    Every key must be an exact ``str`` before the lookup; nothing is validated,
    hashed beyond the key scan, or formatted here (the writer re-admits the record).
    """
    try:
        data: object = object.__getattribute__(record, "__dict__")
    except Exception:
        return None
    if type(data) is not dict:
        return None
    raw = typing.cast(dict[object, object], data)
    if not all(type(key) is str for key in raw):
        return None
    phase = raw.get("phase")
    return typing.cast(ColdPhaseKind, phase) if _is_member(phase, ColdPhaseKind) else None


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
    """Run-private guard over the single writer; a poisoned sink may only be closed.

    It owns the tick/tick-temperature pair state: a tick is pending from just before
    its write until its paired temperature record is durable, and only then is it
    the latest retained tick and published.  A pending pair makes the sink unusable,
    so no terminal, seal or finalisation can follow an unpaired tick.  The two
    writes are not a transaction; this coordinator, not the raw writer, holds the
    pair state.
    """

    def __init__(self, writer: ColdEvidenceWriter) -> None:
        self._writer = writer
        self._guards = {_OFF: _PhaseGuard(), _ON: _PhaseGuard()}
        self._latest_tick: dict[ColdPhaseKind, ColdTickRecord] = {}
        self._pending: ColdTickRecord | None = None
        self._candidate_phases: set[ColdPhaseKind] = set()
        self.phase: ColdPhaseKind | None = None
        self.next_sequence = 0
        self.advisory_count = 0
        self.terminated = False
        self.poisoned = False
        self.seal_attempted = False
        self.sealed_digest: str | None = None
        #: Display-only hook called after each durable tick pair; ``None`` by default.
        self.after_tick: typing.Callable[[ColdTickRecord], None] | None = None

    @property
    def usable(self) -> bool:
        """Whether another append may be attempted (never while a pair is pending)."""
        return not self.poisoned and not self.terminated and self._pending is None

    def abort_retained(self, phase: ColdPhaseKind) -> bool:
        """Whether any abort was retained, or a temperature abort attempted, for ``phase``.

        A v1 abort (any of the seven domains) sets this after its write; a
        temperature abort sets it before its write, as conservative finalisation
        inhibition only, never as proof that the abort was durably retained.
        """
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
        if type(record) is ColdTickRecord:
            return not guard.window_closed and record.phase in self._candidate_phases
        return not guard.window_closed

    def append(self, record: ColdEvidenceRecord) -> None:
        """Admit, guard and durably append one v1 record (the engine's sink port).

        A tick is admitted only after its phase's durable MCP candidate record; it
        becomes pending just before its write and is neither the latest retained
        tick nor published until its paired temperature record is durable.

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
        if type(fresh) is ColdTickRecord:
            self._pending = fresh
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

    def append_advisory_attempt(self, record: object) -> None:
        """Guard and durably append one advisory-attempt record (R12, the sampler's sink).

        Admitted only while the sink is usable, for the current bound phase, before
        its observation window closed and before any finalisation record.  The
        writer re-admits, binds and orders the record itself.

        Raises:
            ColdRunSinkRefusedError: On any refusal or writer fault (then poisoned).
        """
        if not self.usable or self.phase is None:
            self._refuse()
        guard = self._guards[self.phase]
        if (
            _record_phase(record) is not self.phase
            or not guard.header
            or guard.window_closed
            or guard.finalisation_appended
        ):
            self._refuse()
        self._write(lambda: self._writer.append_advisory_attempt(typing.cast(typing.Any, record)))
        self.advisory_count += 1

    def append_tick_temperature(self, record: ColdTickTemperatureRecord) -> None:
        """Durably append the pending tick's paired temperature record, then publish it.

        Only after this write is the pending tick the phase's latest retained tick and
        handed to the display hook.  Lifecycle checks run first, then fresh
        re-admission, then the pairing guards, then the write.

        Raises:
            ColdRunSinkRefusedError: On any refusal or writer fault (then poisoned).
        """
        pending = self._pending
        if self.poisoned or self.terminated or pending is None:
            self._refuse()
        snapshot: ColdTickTemperatureRecord | None = None
        try:
            snapshot = validate_tick_temperature_record(record)
        except Exception:
            snapshot = None
        if (
            snapshot is None
            or snapshot.phase is not self.phase
            or not pairs_with(pending, snapshot)
        ):
            self._refuse()
        fresh = snapshot
        self._write(lambda: self._writer.append_tick_temperature(fresh))
        self._latest_tick[pending.phase] = pending
        self._pending = None
        if self.after_tick is not None:
            self.after_tick(pending)

    def append_mcp_candidate(self, record: ColdMcpCandidateRecord) -> None:
        """Durably append the bound phase's one MCP candidate record, before any tick.

        Lifecycle checks run first, then fresh re-admission, then the record guards
        (current phase, header bound, window open, no candidate and no retained tick
        yet), then the write; the phase is admitted for ticks only after the write.

        Raises:
            ColdRunSinkRefusedError: On any refusal or writer fault (then poisoned).
        """
        if not self.usable or self.phase is None:
            self._refuse()
        snapshot: ColdMcpCandidateRecord | None = None
        try:
            snapshot = validate_mcp_candidate_record(record)
        except Exception:
            snapshot = None
        guard = self._guards[self.phase]
        if (
            snapshot is None
            or snapshot.phase is not self.phase
            or not guard.header
            or guard.window_closed
            or snapshot.phase in self._candidate_phases
            or self._latest_tick.get(snapshot.phase) is not None
        ):
            self._refuse()
        fresh = snapshot
        self._write(lambda: self._writer.append_mcp_candidate(fresh))
        self._candidate_phases.add(fresh.phase)

    def append_temperature_abort(self, record: ColdTemperatureAbortRecord) -> None:
        """Durably append one temperature-abort record against the latest paired tick.

        Lifecycle checks run first, then fresh re-admission, then the record guards
        (current phase, header bound, window open, naming the latest paired tick).
        Finalisation is then inhibited before the write, conservatively: a failed
        write poisons the sink, so nothing can seal, and the inhibition is never
        proof of durable retention.

        Raises:
            ColdRunSinkRefusedError: On any refusal or writer fault (then poisoned).
        """
        if not self.usable or self.phase is None:
            self._refuse()
        snapshot: ColdTemperatureAbortRecord | None = None
        try:
            snapshot = validate_temperature_abort_record(record)
        except Exception:
            snapshot = None
        guard = self._guards[self.phase]
        latest = self._latest_tick.get(self.phase)
        if (
            snapshot is None
            or snapshot.phase is not self.phase
            or not guard.header
            or guard.window_closed
            or latest is None
            or snapshot.tick != latest.tick
        ):
            self._refuse()
        fresh = snapshot
        guard.abort_seen = True
        self._write(lambda: self._writer.append_temperature_abort(fresh))

    def latest_retained_tick(self) -> ColdTickRecord | None:
        """The latest durably paired tick of the current phase only (the tick port)."""
        if self.phase is None:
            return None
        return self._latest_tick.get(self.phase)

    def append_failed_run_terminal(self, record: ColdFailedRunTerminalRecord) -> None:
        """Durably append the one schema-3 failed-run terminal; the run is then terminated.

        The writer independently checks the phase, the retained counts and that no
        v2 run termination exists.

        Raises:
            ColdRunSinkRefusedError: If not usable or unbound, or on a writer refusal or
                fault (then poisoned).
        """
        if not self.usable or self.phase is None:
            self._refuse()
        self._write(lambda: self._writer.append_failed_run_terminal(record))
        self.terminated = True

    def seal(self) -> str:
        """Seal a terminated run with a bound header once and return its manifest digest.

        The writer is entered at most once per run: the attempt is recorded before
        the call, and a later call is refused without touching the writer or the
        held receipt.

        Raises:
            ColdRunSinkRefusedError: If poisoned, not terminated, unbound, already
                attempted, or sealing fails (then poisoned).
        """
        if self.poisoned or not self.terminated or self.phase is None:
            self._refuse()
        if self.seal_attempted:
            self._refuse()
        self.seal_attempted = True
        digest: object = None
        try:
            digest = self._writer.seal().manifest_sha256
        except Exception:
            digest = None
        if not _is_digest(digest):
            self._refuse()
        self.sealed_digest = typing.cast(str, digest)
        return self.sealed_digest

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
        self._cleanup_started = False

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

    async def shielded_cleanup(self) -> bool:
        """Run at most one owned cleanup stop task to completion; never raise from it.

        Repeated cancellation is absorbed until the same task is done.  The task's
        own outcome (including a non-cancellation ``BaseException`` from the stop)
        is retrieved once and absorbed, so it never replaces the exception that
        started the cleanup.

        Returns:
            Whether a cancellation of the enclosing task arrived meanwhile (the
            stop task cancelling itself is the child's outcome, not the caller's).
        """
        if self._cleanup_started or not self.needs_stop:
            return False
        self._cleanup_started = True
        current = typing.cast("asyncio.Task[object]", asyncio.current_task())
        pending = current.cancelling()
        task = asyncio.ensure_future(self.stop())
        while not task.done():
            try:
                await asyncio.shield(task)
            except BaseException:
                continue
        if not task.cancelled():
            task.exception()
        return current.cancelling() > pending


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
        advisor_factory: typing.Callable[[], ColdAdvisoryAdvisorPort],
        spec: ColdAdvisorySpec,
        configured_call_bound_seconds: float,
        configured_dwell_seconds: float,
        evaluator: ColdAdvisoryEvaluatorPort,
        mcp_candidate: object,
        tick_observer: ColdRetainedTickObserver | None = None,
    ) -> None:
        self._root = root
        self._raw_candidate = mcp_candidate
        self._candidate: ColdMcpCandidateProvenance | None = None
        self._observer = tick_observer
        self._mcp = mcp
        self._child = _ChildOwner(child)
        self._identities = identities
        self._host = host
        self._clock = clock
        self._evaluator = evaluator
        # The owner's constructor only stores its inputs; an invalid input is refused
        # in-run by its stored start result, which fails the run closed.
        self._owner = ColdAdvisoryRunOwner(
            advisor_factory=advisor_factory,
            spec=spec,
            configured_call_bound_seconds=configured_call_bound_seconds,
            configured_dwell_seconds=configured_dwell_seconds,
            clock=clock,
        )
        self._starts: dict[ColdPhaseKind, object] = {}
        self._settled: set[ColdPhaseKind] = set()
        self._path = ColdTwoPhaseAdvisoryPath.NOT_APPLICABLE
        self._path_settlement: ColdAdvisoryPhaseSettlement | None = None
        self._path_phase: ColdPhaseKind | None = None
        self._check: ColdTwoPhaseProviderCheck | None = None
        self._ledger = _ClockFloor()
        self._sink: _RunSink | None = None
        self._primary: ColdRunTerminationReason | None = None
        self._refusal = ColdRunStartRefusal.UNEXPECTED_FAILURE
        self._headers: dict[ColdPhaseKind, ColdRunHeader] = {}
        self._sessions: dict[ColdPhaseKind, str] = {}
        self._ends: dict[ColdPhaseKind, float] = {}

    # ------------------------------------------------------------ helpers

    def _after_tick(self, stored: ColdTickRecord) -> None:
        """Publish one exclusive copy of a durably retained tick to the observer.

        Display-only.  A session mismatch only stops publication (the engine
        classifies the tick).  Any refused copy or identity, observer ``Exception``
        or non-``None`` return stops publication and records ``UNEXPECTED_FAILURE``
        with no abort.  A ``BaseException`` propagates unchanged.  Nothing is logged
        or formatted.
        """
        try:
            observer = self._observer
            if observer is None:
                return
            sink = self._sink
            if sink is None or sink.phase is None or stored.phase is not sink.phase:
                self._observer = None
                self._fail(_R.UNEXPECTED_FAILURE)
                return
            session = self._sessions.get(sink.phase)
            if session is None or not _same_session(stored.session.session_id, session):
                self._observer = None
                return
            header = self._headers.get(sink.phase)
            copy = _admit_carrier(stored, ColdTickRecord, _TICK)
            if (
                copy is None
                or copy is stored
                or header is None
                or copy.phase is not sink.phase
                or copy.run_id != header.run_id
                or copy.identity_sha256 != header.identity_sha256
                or not _same_session(copy.session.session_id, session)
            ):
                self._observer = None
                self._fail(_R.UNEXPECTED_FAILURE)
                return
            result: object = observer(copy)
            if result is not None:
                self._observer = None
                self._fail(_R.UNEXPECTED_FAILURE)
                if asyncio.iscoroutine(result):
                    result.close()
        except Exception:
            self._observer = None
            self._fail(_R.UNEXPECTED_FAILURE)

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
        """Re-admit hook arguments, then record ``PHASE_ACTIVATED`` and the MCP candidate.

        The candidate record uses the already-admitted activation instant (no extra
        clock sample) and precedes every tick of the phase.
        """
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
        recorded = True
        try:
            candidate = build_mcp_candidate_record(
                header=self._headers[phase],
                candidate=typing.cast(ColdMcpCandidateProvenance, self._candidate),
                recorded_at_utc=at.utc,
                monotonic_seconds=at.monotonic,
            )
            typing.cast(_RunSink, self._sink).append_mcp_candidate(candidate)
        except (ColdRunSinkRefusedError, ColdEvidenceError):
            recorded = False
        if not recorded:
            raise _HookFailed
        return at

    def _off_hook(self, *, session_id: str, activated_monotonic: float, activated_utc: str) -> bool:
        """Recording-off activation: record it and its scheduled end, then start advisory."""
        self._activated(_OFF, session_id, activated_monotonic, activated_utc)
        self._start_advisory(_OFF)
        return True

    def _on_hook(self, *, session_id: str, activated_monotonic: float, activated_utc: str) -> bool:
        """Recording-on activation: record it and the transition, then apply the budget.

        Advisory sampling starts only when the budget holds; its start result never
        changes this hook's verdict.
        """
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
        within = at.monotonic - start <= COLD_TRANSITION_BUDGET_SECONDS
        if within:
            self._start_advisory(_ON)
        return within

    # ----------------------------------------------------------- advisory

    def _start_advisory(self, phase: ColdPhaseKind) -> None:
        """Start one phase's sampler with the exact admitted header, session and end.

        The phase counts as started before the owner is entered, so it is settled
        even if the start raised; any start other than ``STARTED`` fails the run at
        settlement.
        """
        sink = typing.cast(_RunSink, self._sink)
        self._starts[phase] = None
        started: object = None
        try:
            started = self._owner.start_phase(
                phase,
                header=self._headers[phase],
                established_session_id=self._sessions[phase],
                scheduled_end_monotonic=self._ends[phase],
                evaluator=self._evaluator,
                sink=sink,
                ticks=sink,
            )
        except Exception:
            started = None
        self._starts[phase] = started

    def _settle(self, phase: ColdPhaseKind) -> None:
        """Settle one started phase exactly once, synchronously, then apply OD5.

        An OD5 trigger fails the run and fixes the advisory path (the first trigger
        wins).  A refusal, an inadmissible settlement or a start other than
        ``STARTED`` fails the run closed without establishing a path.
        """
        if phase not in self._starts or phase in self._settled:
            return
        self._settled.add(phase)
        raw: object = None
        try:
            raw = self._owner.settle_phase(phase)
        except Exception:
            raw = None
        settlement = _admit_carrier(raw, ColdAdvisoryPhaseSettlement, _ADVISORY)
        if settlement is not None and _od5_triggered(settlement.sampler):
            self._fail(_R.UNEXPECTED_FAILURE)
            if self._path is ColdTwoPhaseAdvisoryPath.NOT_APPLICABLE:
                terminal = _terminal_settlement(settlement.sampler.closure) is not None
                self._path = (
                    ColdTwoPhaseAdvisoryPath.FAILED_RUN_TERMINAL
                    if terminal
                    else ColdTwoPhaseAdvisoryPath.PROVIDER_OUTSTANDING_FAILED
                )
                self._path_settlement = settlement
                self._path_phase = phase
        if settlement is None or self._starts[phase] is not ColdAdvisoryPhaseStart.STARTED:
            self._fail(_R.UNEXPECTED_FAILURE)

    def _settle_unsettled(self) -> None:
        """Settle every started phase that is not yet settled, in phase order."""
        for phase in (_OFF, _ON):
            self._settle(phase)

    def _advance_permitted(self) -> bool:
        """Fail closed unless the owner's own recording-off facts admit recording-on.

        Requires a stored settlement with no OD5 trigger, a run fact other than
        ``UNCONFIRMED`` (at settlement and now) and a provider observation that is
        present and not ``OUTSTANDING``.
        """
        permitted = False
        try:
            raw: object = self._owner.observe_phase(_OFF)
            observed = _admit_carrier(raw, ColdAdvisoryPhaseObservation, _ADVISORY)
            unconfirmed = ColdAdvisoryRunTaskState.UNCONFIRMED
            permitted = (
                observed is not None
                and observed.settlement is not None
                and not _od5_triggered(observed.settlement.sampler)
                and observed.settlement.run_at_settlement.state is not unconfirmed
                and observed.run.state is not unconfirmed
                and observed.provider is not None
                and observed.provider.task is not ColdAdvisoryProviderTaskState.OUTSTANDING
            )
        except Exception:
            permitted = False
        if not permitted:
            self._fail(_R.UNEXPECTED_FAILURE)
        return permitted

    def _check_provider(self) -> ColdTwoPhaseProviderCheck:
        """Take the one synchronous post-seal provider observation of an OD5 path.

        Latched: a taken value is returned unchanged and never recomputed.  It never
        appends, mutates or seals.  ``PENDING_AT_CHECK`` requires an admitted
        observation whose provider task is ``OUTSTANDING``.
        """
        if self._check is not None:
            return self._check
        if self._path is ColdTwoPhaseAdvisoryPath.NOT_APPLICABLE:
            return ColdTwoPhaseProviderCheck.NOT_CHECKED
        checked = ColdTwoPhaseProviderCheck.NOT_OBSERVABLE_AT_CHECK
        try:
            raw: object = self._owner.observe_phase(typing.cast(ColdPhaseKind, self._path_phase))
            observed = _admit_carrier(raw, ColdAdvisoryPhaseObservation, _ADVISORY)
            if observed is not None and observed.provider is not None:
                outstanding = observed.provider.task is ColdAdvisoryProviderTaskState.OUTSTANDING
                checked = (
                    ColdTwoPhaseProviderCheck.PENDING_AT_CHECK
                    if outstanding
                    else ColdTwoPhaseProviderCheck.NOT_PENDING_AT_CHECK
                )
        except Exception:
            checked = ColdTwoPhaseProviderCheck.NOT_OBSERVABLE_AT_CHECK
        self._check = checked
        return checked

    def _seal_then_check(self, sink: "_RunSink") -> str | None:
        """The seal step, then the provider check as the very next statement.

        A precondition refusal (poisoned, unterminated or unbound sink) raises before
        any writer call; nothing here claims that a refused seal entered the writer.
        """
        digest: str | None = None
        try:
            digest = sink.seal()
        except ColdRunSinkRefusedError:
            digest = None
        self._check_provider()
        return digest

    # -------------------------------------------------------------- phases

    async def _freeze(self, phase: ColdPhaseKind) -> ColdRunIdentity | None:
        """Freeze and admit one phase identity naming the candidate's version, or ``None``."""
        try:
            raw: object = await self._identities.freeze(phase)
        except Exception:
            return None
        identity = _admit_carrier(raw, ColdRunIdentity, _IDENTITY)
        candidate = typing.cast(ColdMcpCandidateProvenance, self._candidate)
        if (
            identity is not None
            and identity.coffee_roaster_mcp_version != candidate.reported_version
        ):
            return None
        return identity

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
        # Settle before classifying, so before any phase-end lifecycle append.
        self._settle(phase)
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
        raised = False
        try:
            returned: object = await self._mcp.finalise_session(session)
        except Exception as error:
            raised = True
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
        # A result carried by a finalisation error is retained but never clean,
        # whatever its payload says: the error origin itself is a failure.
        clean = (
            not raised
            and outcome.session_purpose == "cold_characterisation"
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
            advisory_path=ColdTwoPhaseAdvisoryPath.NOT_APPLICABLE,
            provider_check=ColdTwoPhaseProviderCheck.NOT_CHECKED,
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
        self._candidate = readmit_mcp_candidate_provenance(self._raw_candidate)
        if self._candidate is None:
            return self._refused(ColdRunStartRefusal.UNEXPECTED_FAILURE)
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
        if self._observer is not None:
            self._sink.after_tick = self._after_tick
        if await self._run_off(admission) and self._advance_permitted():
            await self._run_on()
        return await self._teardown()

    async def _teardown(self) -> ColdTwoPhaseResult:
        """Cleanup stop, ``CHILD_STOPPED``, terminal, seal, reload and check.

        The terminal is the schema-3 failed-run terminal on the
        ``FAILED_RUN_TERMINAL`` path, instead of (never alongside) the v2 one.
        """
        sink = self._sink
        if self._child.needs_stop:
            if sink is None:
                await self._child.stop()
            else:
                await self._stop()
        if sink is None:
            return self._refused(self._refusal)
        if self._path is ColdTwoPhaseAdvisoryPath.FAILED_RUN_TERMINAL:
            self._append_failed_run_terminal(sink)
            return self._end(sink)
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

    def _append_failed_run_terminal(self, sink: _RunSink) -> None:
        """Append the one failed-run terminal from the stored settlement and actual counts.

        A missing mapping, a builder refusal or a sink refusal writes nothing, so the
        run stays unterminated and is never sealed; nothing is fabricated.
        """
        # The FAILED_RUN_TERMINAL path is only ever set together with its settlement,
        # and only after a phase header was bound.
        settlement = typing.cast(ColdAdvisoryPhaseSettlement, self._path_settlement)
        closure = _terminal_settlement(settlement.sampler.closure)
        cancellation = _terminal_cancellation(settlement.provider_cancellation)
        if closure is None or cancellation is None:
            return
        try:
            record = build_failed_run_terminal_record(
                self._headers[typing.cast(ColdPhaseKind, sink.phase)],
                advisory_settlement=closure,
                provider_cancellation=cancellation,
                lifecycle_records_retained=sink.next_sequence,
                advisory_attempt_records_retained=sink.advisory_count,
            )
            sink.append_failed_run_terminal(record)
        except Exception:
            return

    def _result(
        self,
        outcome: ColdTwoPhaseOutcome,
        digest: str | None = None,
        conformance: ColdRevisedConformanceResult | None = None,
    ) -> ColdTwoPhaseResult:
        """Build one evidence-bearing result row from admitted closed fields."""
        return ColdTwoPhaseResult(
            outcome=outcome,
            start_refusal=None,
            termination_reason=self._primary,
            child_ownership=self._child.ownership,
            manifest_sha256=digest,
            conformance=conformance,
            advisory_path=self._path,
            provider_check=(
                ColdTwoPhaseProviderCheck.NOT_CHECKED if self._check is None else self._check
            ),
        )

    def _end(self, sink: _RunSink) -> ColdTwoPhaseResult:
        """Seal a terminated run, reload it by its digest and check it.

        On an OD5 path the seal is called once whatever the sink state (a poisoned,
        unterminated or unbound sink is refused before any writer call) and the one
        provider check follows it immediately.
        """
        if self._path is not ColdTwoPhaseAdvisoryPath.NOT_APPLICABLE:
            sealed = self._seal_then_check(sink)
            if sealed is None:
                sink.close()
                return self._result(ColdTwoPhaseOutcome.EVIDENCE_NOT_SEALED)
            return self._reload(sealed)
        if sink.poisoned or not sink.terminated:
            sink.close()
            return self._result(ColdTwoPhaseOutcome.EVIDENCE_NOT_SEALED)
        try:
            digest = sink.seal()
        except ColdRunSinkRefusedError:
            sink.close()
            return self._result(ColdTwoPhaseOutcome.EVIDENCE_NOT_SEALED)
        return self._reload(digest)

    def _verify_failed_run_terminal(self, digest: str) -> bool:
        """Reload a failed-run terminal through the V6 reader; verification only."""
        try:
            read_retained_run_v6(
                self._root.path,
                run_id=self._headers[_OFF].run_id,
                expected_manifest_sha256=digest,
            )
        except Exception:
            return False
        return True

    def _reload(self, digest: str) -> ColdTwoPhaseResult:
        """Reload a sealed run by its digest and check it; a failed-run terminal never qualifies."""
        if self._path is ColdTwoPhaseAdvisoryPath.FAILED_RUN_TERMINAL:
            self._verify_failed_run_terminal(digest)
            return self._result(ColdTwoPhaseOutcome.NOT_CONFORMANT, digest)
        try:
            retained = read_retained_run_v6(
                self._root.path,
                run_id=self._headers[_OFF].run_id,
                expected_manifest_sha256=digest,
            )
            checked: object = check_revised_conformance(retained)
        except Exception:
            checked = None
        conformance = _admit_carrier(
            checked, ColdRevisedConformanceResult, _CHECKER, flat_identity=True
        )
        conformant = (
            conformance is not None
            and conformance.outcome
            is ColdTemperatureConformanceOutcome.TEMPERATURE_SCREENED_CONFORMANT
        )
        if (
            conformant
            and self._primary is None
            and self._child.ownership is ColdChildOwnership.OWNED_STOP_CONFIRMED
            and self._path is ColdTwoPhaseAdvisoryPath.NOT_APPLICABLE
        ):
            return ColdTwoPhaseResult(
                outcome=ColdTwoPhaseOutcome.ADVISORY_CONFORMANT,
                start_refusal=None,
                termination_reason=None,
                child_ownership=ColdChildOwnership.OWNED_STOP_CONFIRMED,
                manifest_sha256=digest,
                conformance=conformance,
                advisory_path=ColdTwoPhaseAdvisoryPath.NOT_APPLICABLE,
                provider_check=ColdTwoPhaseProviderCheck.NOT_CHECKED,
            )
        return self._result(
            ColdTwoPhaseOutcome.NOT_CONFORMANT, digest, None if conformant else conformance
        )

    async def _contained(self) -> ColdTwoPhaseResult:
        """Contain an ordinary failure without sealing, re-reading or re-checking.

        The sink is closed and only the permitted owned cleanup stop runs; nothing
        is finalised or stopped again and no text escapes.  A held seal receipt is
        never lost: it is returned as ``NOT_CONFORMANT`` with that digest.

        A provider check already taken is projected, never repeated.  When an OD5
        path is established but the check was not taken (and no seal was attempted),
        the cleanup attempts have finished here, so the one guarded seal call is made
        (refused on the closed sink before any writer call) and the check follows it.

        Raises:
            asyncio.CancelledError: If the caller was cancelled during the cleanup
                stop; no result is returned and nothing further runs.
        """
        sink = self._sink
        if sink is not None:
            sink.close()
        if await self._child.shielded_cleanup():
            raise asyncio.CancelledError
        if sink is None:
            return ColdTwoPhaseResult(
                outcome=ColdTwoPhaseOutcome.REFUSED_BEFORE_EVIDENCE,
                start_refusal=ColdRunStartRefusal.UNEXPECTED_FAILURE,
                termination_reason=None,
                child_ownership=self._child.ownership,
                manifest_sha256=None,
                conformance=None,
                advisory_path=ColdTwoPhaseAdvisoryPath.NOT_APPLICABLE,
                provider_check=ColdTwoPhaseProviderCheck.NOT_CHECKED,
            )
        if (
            self._check is None
            and self._path is not ColdTwoPhaseAdvisoryPath.NOT_APPLICABLE
            and not sink.seal_attempted
        ):
            self._seal_then_check(sink)
        if sink.sealed_digest is not None:
            return self._result(ColdTwoPhaseOutcome.NOT_CONFORMANT, sink.sealed_digest)
        return self._result(ColdTwoPhaseOutcome.EVIDENCE_NOT_SEALED)

    async def _recover(self) -> ColdTwoPhaseResult:
        """Recover once from an ordinary exception, never recursively.

        Only a usable sink that has neither a terminal nor a seal attempt gets the
        fallback teardown (one ``FAILED`` terminal, then at most one seal).  Every
        other state, including a held receipt, a poisoned sink or any existing
        terminal, is contained without sealing.
        """
        self._settle_unsettled()
        sink = self._sink
        if sink is not None and sink.usable and not sink.seal_attempted:
            try:
                return await self._teardown()
            except Exception:
                self._fail(_R.UNEXPECTED_FAILURE)
        return await self._contained()

    async def execute(self) -> ColdTwoPhaseResult:
        """Run once; contain ordinary failures, and re-raise any other ``BaseException``.

        An ordinary exception is recovered once (see :meth:`_recover`).
        Cancellation and every other ``BaseException`` close the sink, settle each
        started unsettled phase (absorbing anything that raises), run the shielded
        cleanup and re-raise the original exception object unchanged: no terminal,
        seal, finalisation or provider check follows.
        """
        try:
            try:
                return await self._sequence()
            except Exception:
                self._fail(_R.UNEXPECTED_FAILURE)
            self._refusal = ColdRunStartRefusal.UNEXPECTED_FAILURE
            return await self._recover()
        except BaseException:
            if self._sink is not None:
                self._sink.close()
            try:
                self._settle_unsettled()
            except BaseException:
                self._fail(_R.UNEXPECTED_FAILURE)
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
    advisor_factory: typing.Callable[[], ColdAdvisoryAdvisorPort],
    spec: ColdAdvisorySpec,
    configured_call_bound_seconds: float,
    configured_dwell_seconds: float,
    evaluator: ColdAdvisoryEvaluatorPort,
    mcp_candidate: object,
    tick_observer: ColdRetainedTickObserver | None = None,
) -> ColdTwoPhaseResult:
    """Run one hardware-free-testable two-phase cold characterisation.

    The same ``mcp``, ``child``, ``host`` and ``clock`` instances are used for both
    phases.  The child is started only from a confirmed idle state and never
    respawned after any failure or uncertainty; the evidence directory is created
    once, for the recording-off phase only.  The advisor is observation-only: it
    receives typed context and returns typed data, never an MCP or control surface.

    Args:
        root: The admitted evidence root.
        mcp: The cold MCP port (non-actuating operations plus D195 finalisation).
        child: The consumer-owned child lifecycle port.
        identities: The phase identity source, called after each confirmed start.
        host: The host-bound port.
        clock: The engine clock.
        advisor_factory: Called at most once for one fresh advisor.
        spec: The explicit per-run advisory spec (Celsius).
        configured_call_bound_seconds: The sampler's configured per-call bound.
        configured_dwell_seconds: The sampler's configured post-completion dwell.
        evaluator: The typed safety-evaluation port for returned requests.
        mcp_candidate: The operator-asserted reviewed MCP candidate provenance; it is
            re-admitted before any child action (a refusal is an unowned
            ``UNEXPECTED_FAILURE``), and each frozen phase identity must name its
            reported version.  It never attests installed bytes.
        tick_observer: Optional display-only observer of each retained tick copy;
            its failure fails the run with no abort (see
            :class:`ColdRetainedTickObserver`).  ``None`` changes nothing.

    Returns:
        The closed result; ``ADVISORY_CONFORMANT`` is never qualification.

    Raises:
        asyncio.CancelledError: After owned cleanup, with no terminal and no seal.
    """
    run = _TwoPhaseRun(
        root=root,
        mcp=mcp,
        child=child,
        identities=identities,
        host=host,
        clock=clock,
        advisor_factory=advisor_factory,
        spec=spec,
        configured_call_bound_seconds=configured_call_bound_seconds,
        configured_dwell_seconds=configured_dwell_seconds,
        evaluator=evaluator,
        mcp_candidate=mcp_candidate,
        tick_observer=tick_observer,
    )
    return await run.execute()
