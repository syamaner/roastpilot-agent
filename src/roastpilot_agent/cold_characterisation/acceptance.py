"""Pure interpretation of one strictly read retained cold-characterisation run.

The public entry point rebinds a retained run into a token-checked capability,
then evaluates five closed per-phase checks and derives the D191 metrics.  It
performs no file, network or process I/O, holds no actuator, transport, advisor
or controller surface, and computes no run verdict: each check reports only
what it finds, and the D191 limits are declared for rendering, never compared.

Honest limit: rebinding re-establishes the record schema, the per-run bindings,
the v1 identity parse, header agreement and container consistency.  It cannot
prove that the manifest digest is authentic, that the run is complete, or that
the retained copies are physically independent.  Deleting or truncating
records, streams or phases cannot be detected here.  Records that a caller
reorders, inserts, duplicates or fabricates within a container are not detected
either, provided each still validates and binds: container order is file order
only when the run is the strict retained-run reader's output.  Those properties
hold only when slice-6 composition supplies the strict retained-run reader's
output under an externally recorded manifest digest and verifies both retained
copies.

Every read of the caller's run and its containers happens during rebinding;
evaluation afterwards reads only the fresh snapshots and parses the capability
holds.

Qualification (Q1-Q11) is frozen v1 policy over retained values; it never
converts, folds or trims them.  It is not live-freeze parity: packaged agent,
model and manifest constants, and D188 profile applicability, belong to the
later applicability gate.  Temperatures are Celsius only; Q2 fails closed.
"""

import enum
import math
import re
import types
import typing

import pydantic

from roastpilot_agent.cold_characterisation.evidence_reader import (
    ColdRetainedHeader,
    ColdRetainedRun,
    ColdRetainedStream,
)
from roastpilot_agent.cold_characterisation.evidence_schema import (
    ColdAbortRecord,
    ColdAdvisoryRecord,
    ColdEvidenceError,
    ColdEvidenceStream,
    ColdFinalisationRecord,
    ColdHostRecord,
    ColdPhaseKind,
    ColdRunHeader,
    ColdTickAudioSample,
    ColdTickRecord,
    validate_record,
)
from roastpilot_agent.cold_characterisation.evidence_store import (
    ColdBindingState,
    ColdEvidenceStoreError,
    ColdRetainedIdentityV1,
    check_record_binding,
    parse_finalisation_envelope,
    run_id_is_valid,
)
from roastpilot_agent.cold_characterisation.mcp import (
    FinalisationFirstCrackStatus,
    FirstCrackRuntimeFinalisationEvidence,
    RecordingFinalisationEvidence,
    SessionFinalisationResult,
)

__all__ = (
    "D191_N_LIMIT",
    "D191_X_LIMIT_MS",
    "EFFECTIVE_HOP_SECONDS",
    "PRODUCTION_FATAL_STREAK",
    "QUALIFICATION_POLICY_VERSION",
    "ColdCheck",
    "ColdCheckFailure",
    "ColdCheckOutcome",
    "ColdCheckResult",
    "ColdD191Metrics",
    "ColdIdentityFacts",
    "ColdInterpretation",
    "ColdInterpretationError",
    "ColdInterpretationFailure",
    "ColdPhaseInterpretation",
    "ColdReboundPhase",
    "ColdReboundRun",
    "interpret_retained_run",
)

#: The frozen qualification policy version; it covers identity schema version 1 only.
QUALIFICATION_POLICY_VERSION: typing.Final = 1
#: Locked D191 and production limits: declared for later rendering, never compared here.
D191_N_LIMIT: typing.Final = 1
D191_X_LIMIT_MS: typing.Final = 200.0
PRODUCTION_FATAL_STREAK: typing.Final = 30
#: The D183/D188 seven-second effective hop; used only by the inference-duration check.
EFFECTIVE_HOP_SECONDS: typing.Final = 7.0

_SHA256_RUST_PATTERN: typing.Final = r"\A[0-9a-f]{64}\z"
_SOURCE_COMMIT_RUST_PATTERN: typing.Final = r"\A[0-9a-f]{40}\z"


class ColdCheck(enum.Enum):
    """The five closed per-phase checks, in result order."""

    IDENTITY_QUALIFICATION_V1 = "identity_qualification_v1"
    INFERENCE_RUNTIME = "inference_runtime"
    AUDIO_COUNTERS = "audio_counters"
    INFERENCE_DURATION = "inference_duration"
    RECORDING_ARTEFACTS = "recording_artefacts"


class ColdCheckOutcome(enum.Enum):
    """One check's outcome: ``PASS`` exactly when it recorded no failure."""

    PASS = "pass"
    FAIL = "fail"


class ColdCheckFailure(enum.Enum):
    """Closed check failures; results list them uniquely in declaration order."""

    Q_SHAPE_UNEXPECTED = "q_shape_unexpected"
    Q_MCP_VERSION = "q_mcp_version"
    Q_TEMPERATURE_UNIT = "q_temperature_unit"
    Q_RECORDING_DEVICE_NOT_SINGLE = "q_recording_device_not_single"
    Q_INFERENCE_NOT_ACTIVE = "q_inference_not_active"
    Q_CREDENTIAL_NAME = "q_credential_name"
    Q_BOOT_ID = "q_boot_id"
    Q_OPERATOR_TEXT = "q_operator_text"
    Q_DEVICE_VALUE = "q_device_value"
    Q_PROVENANCE_DIGEST = "q_provenance_digest"
    Q_SOURCE_TREE_DIRTY = "q_source_tree_dirty"
    Q_PROFILE_VALUE = "q_profile_value"
    TICK_EVIDENCE_ABSENT = "tick_evidence_absent"
    PRE_FINALISATION_EVIDENCE_ABSENT = "pre_finalisation_evidence_absent"
    FINALISATION_EVIDENCE_ABSENT = "finalisation_evidence_absent"
    FINALISATION_SESSION_AMBIGUOUS = "finalisation_session_ambiguous"
    INFERENCE_NOT_ACTIVE = "inference_not_active"
    NO_PROCESSED_WINDOW = "no_processed_window"
    FIRST_CRACK_CONFIRMED = "first_crack_confirmed"
    MICROPHONE_OR_FATAL_ERROR = "microphone_or_fatal_error"
    POST_STOP_AUDIO_RUNNING = "post_stop_audio_running"
    MEASUREMENT_SHAPE_UNEXPECTED = "measurement_shape_unexpected"
    NEGATIVE_MEASUREMENT = "negative_measurement"
    DROPPED_WINDOW = "dropped_window"
    INFERENCE_OVERRUN = "inference_overrun"
    QUEUE_GROWING = "queue_growing"
    QUEUE_NOT_DRAINED = "queue_not_drained"
    CAPTURE_RESTART = "capture_restart"
    INFERENCE_DURATION_AT_OR_ABOVE_HOP = "inference_duration_at_or_above_hop"
    RECORDING_NOT_FINALISED = "recording_not_finalised"
    RECORDING_ARTEFACT_SET_UNEXPECTED = "recording_artefact_set_unexpected"
    RECORDING_ARTEFACT_EMPTY = "recording_artefact_empty"
    RECORDING_UNEXPECTEDLY_CONFIGURED = "recording_unexpectedly_configured"


class ColdInterpretationFailure(enum.Enum):
    """Closed failures for rebinding a retained run into a capability."""

    CONTAINER_MALFORMED = "container_malformed"
    RECORD_REBIND_FAILED = "record_rebind_failed"
    HEADER_SET_MISMATCHED = "header_set_mismatched"
    NO_PHASE_PRESENT = "no_phase_present"
    CAPABILITY_INVALID = "capability_invalid"


class ColdInterpretationError(RuntimeError):
    """Closed interpretation error carrying only its failure member and a fixed message."""

    failure: ColdInterpretationFailure

    def __init__(self, failure: ColdInterpretationFailure) -> None:
        """Create a content-free interpretation failure.

        Args:
            failure: Closed refusal reason.
        """
        super().__init__("Cold interpretation failed.")
        self.failure = failure


class ColdCheckResult(pydantic.BaseModel):
    """One check's closed result; ``PASS`` exactly when ``failures`` is empty."""

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    check: ColdCheck
    outcome: ColdCheckOutcome
    failures: tuple[ColdCheckFailure, ...]

    @pydantic.model_validator(mode="after")
    def _require_consistent_failures(self) -> typing.Self:
        """Require unique failures in declaration order and a matching outcome."""
        ordered = tuple(member for member in ColdCheckFailure if member in self.failures)
        outcome_is_pass = self.outcome is ColdCheckOutcome.PASS
        if self.failures != ordered or outcome_is_pass == bool(self.failures):
            raise ValueError("check failures and outcome disagree")
        return self


class ColdD191Metrics(pydantic.BaseModel):
    """Derived D191 N and peak trailing X; never compared with a limit here."""

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    max_consecutive_overflow_count: int = pydantic.Field(ge=0)
    peak_trailing_lost_audio_ms: float = pydantic.Field(ge=0)


class ColdIdentityFacts(pydantic.BaseModel):
    """Closed facts copied from a retained identity that passed every gate Q1-Q11."""

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    mcp_version: typing.Literal["0.2.1"]
    temperature_unit: typing.Literal["celsius"]
    first_crack_mode: typing.Literal["audio"]
    model_precision: typing.Literal["int8"]
    recording_device_count: typing.Literal[1]
    source_tree_dirty: typing.Literal[False]
    source_revision: str = pydantic.Field(pattern=_SOURCE_COMMIT_RUST_PATTERN)
    artefact_kind: typing.Literal["wheel", "sdist", "editable_source"]
    artefact_sha256: typing.Annotated[str, pydantic.Field(pattern=_SHA256_RUST_PATTERN)] | None
    profile_source_sha256: str = pydantic.Field(pattern=_SHA256_RUST_PATTERN)
    profile_source_byte_length: int = pydantic.Field(ge=0)
    first_crack_onnx_threads: int = pydantic.Field(ge=1)
    first_crack_min_positive_windows: int = pydantic.Field(ge=1)
    first_crack_confirmation_window_seconds: float = pydantic.Field(gt=0)
    audio_sample_rate: int = pydantic.Field(gt=0)
    audio_window_seconds: float = pydantic.Field(gt=0)
    audio_overlap: float = pydantic.Field(ge=0.0, lt=1.0)
    audio_hop_seconds: typing.Annotated[float, pydantic.Field(gt=0)] | None
    session_ror_window_seconds: int = pydantic.Field(gt=0)
    session_ror_min_sample_seconds: int = pydantic.Field(gt=0)


class ColdPhaseInterpretation(pydantic.BaseModel):
    """One phase's five check results in ``ColdCheck`` order, metrics and facts."""

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    phase: ColdPhaseKind
    identity_sha256: str = pydantic.Field(pattern=_SHA256_RUST_PATTERN)
    results: tuple[ColdCheckResult, ...]
    d191: ColdD191Metrics | None
    identity_facts: ColdIdentityFacts | None

    @pydantic.model_validator(mode="after")
    def _require_closed_results(self) -> typing.Self:
        """Require exactly the five checks and facts exactly when qualification passed."""
        if tuple(result.check for result in self.results) != tuple(ColdCheck):
            raise ValueError("phase results are not the closed check set")
        if (self.results[0].outcome is ColdCheckOutcome.PASS) != (self.identity_facts is not None):
            raise ValueError("identity facts disagree with the qualification result")
        return self


_REBIND_TOKEN: typing.Final = object()


class _Capability:
    """Token-checked, slotted, write-once holder following the admitted-root pattern.

    Construction without the private token, re-initialisation, ordinary assignment
    and deletion all raise.  Deliberate ``object.__setattr__`` reflection is not
    ordinary mutation and is not claimed to be prevented; this is not a sandbox.
    """

    __slots__ = ()

    def _admit(self, token: object, fields: tuple[tuple[str, object], ...]) -> None:
        """Set every field once, refusing a foreign token or a second initialisation."""
        if token is not _REBIND_TOKEN or hasattr(self, fields[0][0]):
            raise ColdInterpretationError(ColdInterpretationFailure.CAPABILITY_INVALID)
        for name, value in fields:
            object.__setattr__(self, name, value)

    def __setattr__(self, name: str, value: object) -> typing.NoReturn:
        """Refuse every ordinary assignment.

        Args:
            name: The attribute name being assigned.
            value: The value being assigned.

        Raises:
            ColdInterpretationError: Always.
        """
        del name, value
        raise ColdInterpretationError(ColdInterpretationFailure.CAPABILITY_INVALID)

    def __delattr__(self, name: str) -> typing.NoReturn:
        """Refuse every ordinary deletion.

        Args:
            name: The attribute name being deleted.

        Raises:
            ColdInterpretationError: Always.
        """
        del name
        raise ColdInterpretationError(ColdInterpretationFailure.CAPABILITY_INVALID)


class ColdReboundPhase(_Capability):
    """One phase's rebound evidence: validated snapshots in file order.

    ``finalisation`` is optional: the result parsed from the envelope of the last
    finalisation record when every finalisation record shares one session, and
    ``None`` when there is no finalisation record or the sessions differ (which
    also sets ``finalisation_ambiguous``).
    """

    __slots__ = (
        "_identity",
        "aborts",
        "advisories",
        "finalisation",
        "finalisation_ambiguous",
        "finalisations",
        "header",
        "hosts",
        "phase",
        "ticks",
    )

    phase: ColdPhaseKind
    header: ColdRunHeader
    ticks: tuple[ColdTickRecord, ...]
    hosts: tuple[ColdHostRecord, ...]
    advisories: tuple[ColdAdvisoryRecord, ...]
    finalisations: tuple[ColdFinalisationRecord, ...]
    aborts: tuple[ColdAbortRecord, ...]
    finalisation: SessionFinalisationResult | None
    finalisation_ambiguous: bool
    _identity: ColdRetainedIdentityV1

    def __init__(
        self,
        *,
        token: object,
        phase: ColdPhaseKind,
        header: ColdRunHeader,
        ticks: tuple[ColdTickRecord, ...],
        hosts: tuple[ColdHostRecord, ...],
        advisories: tuple[ColdAdvisoryRecord, ...],
        finalisations: tuple[ColdFinalisationRecord, ...],
        aborts: tuple[ColdAbortRecord, ...],
        finalisation: SessionFinalisationResult | None,
        finalisation_ambiguous: bool,
        identity: ColdRetainedIdentityV1,
    ) -> None:
        """Mint one rebound phase; only this module holds the token.

        Args:
            token: Private rebinding token.
            phase: The phase.
            header: The bound phase header snapshot.
            ticks: Tick snapshots in file order.
            hosts: Host snapshots in file order.
            advisories: Advisory snapshots in file order.
            finalisations: Finalisation snapshots in file order.
            aborts: Abort snapshots in file order.
            finalisation: The result parsed from the last record's envelope, or ``None``.
            finalisation_ambiguous: Whether finalisation records name different sessions.
            identity: The v1 identity parsed during binding.

        Raises:
            ColdInterpretationError: If minted without the token, or initialised twice.
        """
        self._admit(
            token,
            (
                ("phase", phase),
                ("header", header),
                ("ticks", ticks),
                ("hosts", hosts),
                ("advisories", advisories),
                ("finalisations", finalisations),
                ("aborts", aborts),
                ("finalisation", finalisation),
                ("finalisation_ambiguous", finalisation_ambiguous),
                ("_identity", identity),
            ),
        )


class ColdReboundRun(_Capability):
    """A rebound run: its run id, carried manifest digest and present phases in order."""

    __slots__ = ("manifest_sha256", "phases", "run_id")

    run_id: str
    manifest_sha256: str
    phases: tuple[ColdReboundPhase, ...]

    def __init__(
        self,
        *,
        token: object,
        run_id: str,
        manifest_sha256: str,
        phases: tuple[ColdReboundPhase, ...],
    ) -> None:
        """Mint one rebound run; only this module holds the token.

        Args:
            token: Private rebinding token.
            run_id: The bound run identifier.
            manifest_sha256: The carried (not re-verified) manifest digest.
            phases: Present phases in ``ColdPhaseKind`` order.

        Raises:
            ColdInterpretationError: If minted without the token, or initialised twice.
        """
        self._admit(
            token,
            (("run_id", run_id), ("manifest_sha256", manifest_sha256), ("phases", phases)),
        )


class ColdInterpretation(_Capability):
    """The rebound run and its per-phase interpretations, in the same order."""

    __slots__ = ("phases", "rebound")

    rebound: ColdReboundRun
    phases: tuple[ColdPhaseInterpretation, ...]

    def __init__(
        self,
        *,
        token: object,
        rebound: ColdReboundRun,
        phases: tuple[ColdPhaseInterpretation, ...],
    ) -> None:
        """Mint one interpretation; only this module holds the token.

        Args:
            token: Private rebinding token.
            rebound: The rebound run.
            phases: One interpretation per rebound phase, in the same order.

        Raises:
            ColdInterpretationError: If minted without the token, or initialised twice.
        """
        self._admit(token, (("rebound", rebound), ("phases", phases)))


# ------------------------------------------------------------------ rebinding

_T = typing.TypeVar("_T")
_R = typing.TypeVar("_R")
_MANIFEST_DIGEST_PATTERN: typing.Final = re.compile(r"\A[0-9a-f]{64}\Z")
_STREAM_RECORD_CLASS: typing.Final = types.MappingProxyType(
    {
        ColdEvidenceStream.HEADER: ColdRunHeader,
        ColdEvidenceStream.TICK: ColdTickRecord,
        ColdEvidenceStream.HOST: ColdHostRecord,
        ColdEvidenceStream.ADVISORY: ColdAdvisoryRecord,
        ColdEvidenceStream.FINALISATION: ColdFinalisationRecord,
        ColdEvidenceStream.ABORT: ColdAbortRecord,
    }
)
_REBIND_ERRORS: typing.Final = (
    ColdEvidenceError,
    ColdEvidenceStoreError,
    AttributeError,
    TypeError,
    ValueError,
    RecursionError,
)

_Record: typing.TypeAlias = (
    ColdRunHeader
    | ColdTickRecord
    | ColdHostRecord
    | ColdAdvisoryRecord
    | ColdFinalisationRecord
    | ColdAbortRecord
)
_BoundIdentities: typing.TypeAlias = tuple[tuple[ColdRunHeader, ColdRetainedIdentityV1], ...]


class _Containers(typing.NamedTuple):
    """Step-1 values taken from the run; steps 2 and 3 still read the containers held."""

    run_id: str
    manifest_sha256: str
    headers: tuple[ColdRetainedHeader, ...]
    layout: dict[tuple[ColdPhaseKind, ColdEvidenceStream], ColdRetainedStream]


class _Bound(typing.NamedTuple):
    """Step-2 binding state and per-phase, per-stream snapshots."""

    state: ColdBindingState
    snapshots: dict[ColdPhaseKind, dict[ColdEvidenceStream, tuple[_Record, ...]]]


def _step(call: typing.Callable[[], _T | None], failure: ColdInterpretationFailure) -> _T:
    """Run one rebinding step; a mapped error or a refusal becomes one closed failure."""
    try:
        value = call()
    except _REBIND_ERRORS:
        value = None
    if value is None:
        raise ColdInterpretationError(failure)
    return value


def _check_containers(run: ColdRetainedRun) -> _Containers | None:
    """Step 1: exact container types, identifiers, uniqueness and one header per phase.

    Each record's exact class and phase must match its container here, so a
    relabelled record is a malformed container; step 2 re-checks every snapshot.
    """
    if type(run) is not ColdRetainedRun:
        return None
    run_id: object = run.run_id
    digest: object = run.manifest_sha256
    headers: object = run.headers
    streams: object = run.streams
    if type(run_id) is not str or not run_id_is_valid(run_id):
        return None
    if type(digest) is not str or _MANIFEST_DIGEST_PATTERN.fullmatch(digest) is None:
        return None
    if type(headers) is not tuple or type(streams) is not tuple:
        return None
    header_items = typing.cast(tuple[object, ...], headers)
    if any(type(item) is not ColdRetainedHeader for item in header_items):
        return None
    layout: dict[tuple[ColdPhaseKind, ColdEvidenceStream], ColdRetainedStream] = {}
    for item in typing.cast(tuple[object, ...], streams):
        if type(item) is not ColdRetainedStream:
            return None
        phase: object = item.phase
        stream: object = item.stream
        records: object = item.records
        if type(phase) is not ColdPhaseKind or type(stream) is not ColdEvidenceStream:
            return None
        if (phase, stream) in layout or type(records) is not tuple or not records:
            return None
        expected = _STREAM_RECORD_CLASS[stream]
        if any(type(record) is not expected or record.phase is not phase for record in records):
            return None
        layout[(phase, stream)] = item
    for present, _stream in layout:
        header = layout.get((present, ColdEvidenceStream.HEADER))
        if header is None or len(header.records) != 1:
            return None
    return _Containers(
        run_id, digest, typing.cast(tuple[ColdRetainedHeader, ...], header_items), layout
    )


def _bind_records(containers: _Containers) -> _Bound | None:
    """Step 2: validate and bind every container record once, in reader order."""
    state = ColdBindingState(containers.run_id)
    snapshots: dict[ColdPhaseKind, dict[ColdEvidenceStream, tuple[_Record, ...]]] = {}
    for phase in ColdPhaseKind:
        for stream in ColdEvidenceStream:
            item = containers.layout.get((phase, stream))
            if item is None:
                continue
            bound: list[_Record] = []
            for record in item.records:
                snapshot = validate_record(record)
                relabelled = (
                    type(snapshot) is not _STREAM_RECORD_CLASS[stream]
                    or snapshot.phase is not phase
                )
                if relabelled:
                    return None
                check_record_binding(state, snapshot, writer_root=None)
                bound.append(snapshot)
            snapshots.setdefault(phase, {})[stream] = tuple(bound)
    return _Bound(state, snapshots)


def _same(left: object, right: object) -> bool:
    """Exact-type structural equality; the trusted bound ``left`` value bounds the walk."""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        mine = typing.cast(dict[object, object], left)
        theirs = typing.cast(dict[object, object], right)
        return len(mine) == len(theirs) and all(
            key in theirs and _same(value, theirs[key]) for key, value in mine.items()
        )
    if type(left) in (list, tuple):
        mine_items = typing.cast(tuple[object, ...], left)
        their_items = typing.cast(tuple[object, ...], right)
        return len(mine_items) == len(their_items) and all(
            _same(a, b) for a, b in zip(mine_items, their_items, strict=True)
        )
    return left == right


def _identity_value(identity: ColdRetainedIdentityV1) -> tuple[object, ...]:
    """Return every retained v1 identity field, for exact comparison only."""
    return (
        identity.run_id,
        identity.pi_evidence_root,
        identity.known,
        identity.runtime_config_extras,
        identity.server_info_extras,
    )


def _agreed_headers(
    headers: tuple[ColdRetainedHeader, ...], state: ColdBindingState
) -> _BoundIdentities | None:
    """Step 3: the container headers equal the bound pairs in count, order and value."""
    bound = state.headers
    if len(headers) != len(bound):
        return None
    for item, (header, identity) in zip(headers, bound, strict=True):
        exact = type(item.header) is ColdRunHeader and type(item.identity) is ColdRetainedIdentityV1
        if not exact:
            return None
        if not _same(header.model_dump(), validate_record(item.header).model_dump()):
            return None
        if not _same(_identity_value(identity), _identity_value(item.identity)):
            return None
    return bound


def _only(records: tuple[_Record, ...], kind: type[_R]) -> tuple[_R, ...]:
    """Return one stream's snapshots, whose exact class binding already verified."""
    return tuple(record for record in records if isinstance(record, kind))


def _mint_phase(
    header: ColdRunHeader,
    identity: ColdRetainedIdentityV1,
    streams: dict[ColdEvidenceStream, tuple[_Record, ...]],
) -> ColdReboundPhase:
    """Steps 4-5: select the finalisation, then mint one phase capability."""
    finalisations = _only(streams.get(ColdEvidenceStream.FINALISATION, ()), ColdFinalisationRecord)
    sessions = {record.session_id for record in finalisations}
    return ColdReboundPhase(
        token=_REBIND_TOKEN,
        phase=header.phase,
        header=header,
        ticks=_only(streams.get(ColdEvidenceStream.TICK, ()), ColdTickRecord),
        hosts=_only(streams.get(ColdEvidenceStream.HOST, ()), ColdHostRecord),
        advisories=_only(streams.get(ColdEvidenceStream.ADVISORY, ()), ColdAdvisoryRecord),
        finalisations=finalisations,
        aborts=_only(streams.get(ColdEvidenceStream.ABORT, ()), ColdAbortRecord),
        finalisation=(
            parse_finalisation_envelope(finalisations[-1].envelope) if len(sessions) == 1 else None
        ),
        finalisation_ambiguous=len(sessions) > 1,
        identity=identity,
    )


def _rebind(run: ColdRetainedRun) -> ColdReboundRun:
    """Rebind a retained run; every read of the caller's run and containers happens here."""
    containers = _step(
        lambda: _check_containers(run), ColdInterpretationFailure.CONTAINER_MALFORMED
    )
    bound = _step(lambda: _bind_records(containers), ColdInterpretationFailure.RECORD_REBIND_FAILED)
    identities = _step(
        lambda: _agreed_headers(containers.headers, bound.state),
        ColdInterpretationFailure.HEADER_SET_MISMATCHED,
    )
    if not identities:
        raise ColdInterpretationError(ColdInterpretationFailure.NO_PHASE_PRESENT)
    phases = _step(
        lambda: tuple(
            _mint_phase(header, identity, bound.snapshots[header.phase])
            for header, identity in identities
        ),
        ColdInterpretationFailure.RECORD_REBIND_FAILED,
    )
    return ColdReboundRun(
        token=_REBIND_TOKEN,
        run_id=containers.run_id,
        manifest_sha256=containers.manifest_sha256,
        phases=phases,
    )


# ------------------------------------------------------- Q1-Q11 qualification

_ABSENT: typing.Final = object()
_HEX40_PATTERN: typing.Final = re.compile(r"\A[0-9a-f]{40}\Z")
_HEX64_PATTERN: typing.Final = re.compile(r"\A[0-9a-f]{64}\Z")

# Frozen v1 literals (Q1-Q11).  Tests pin each to its live counterpart; this module
# never imports the live identity module or any packaged constant.
_REQUIRED_MCP_VERSION: typing.Final = "0.2.1"
_REQUIRED_MODEL_PRECISION: typing.Final = "int8"
_ALLOWED_CELSIUS_TOKENS: typing.Final = frozenset({"celsius"})
_ALLOWED_INFERENCE_MODES: typing.Final = frozenset({"audio"})
_ALLOWED_CREDENTIAL_ENV_NAMES: typing.Final = frozenset({"OPENROUTER_API_KEY"})
_ARTEFACT_KINDS: typing.Final = frozenset({"wheel", "sdist", "editable_source"})
_PACKAGED_ARTEFACT_KINDS: typing.Final = frozenset({"wheel", "sdist"})
_BOOT_ID_PATTERN: typing.Final = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
_PRINTABLE_TEXT_PATTERN: typing.Final = re.compile(r"\A[\x20-\x7e\n]*\Z")
_CREDENTIAL_SHAPE_PATTERN: typing.Final = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)
_REVISION_SECRET_SHAPE_PATTERN: typing.Final = re.compile(
    r"\A(?:"
    r"(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_-]{16,}"
    r"|glpat-[A-Za-z0-9_-]{16,}"
    r"|(?:xoxb-|xoxp-|xoxa-|xoxr-|xoxs-)[A-Za-z0-9_-]{16,}"
    r"|AKIA[A-Z0-9]{16}"
    r"|eyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{8,}"
    r")\Z"
)
_REVISION_TEXT_PATTERN: typing.Final = re.compile(r"\A[A-Za-z0-9._-]{1,128}\Z")
_MAX_REVISION_LENGTH: typing.Final = 128
_MAX_DEVICE_TEXT_LENGTH: typing.Final = 512

# Q7 copy of the live operator-text screen: length, printable, credential shape, token score.
_MAX_OPERATOR_TEXT_LENGTH: typing.Final = 2000
_HIGH_ENTROPY_TOKEN_PATTERN: typing.Final = re.compile(r"[A-Za-z0-9_-]{24,}")
_ENTROPY_LIMIT: typing.Final = 3.5


def _operator_text_is_safe(text: str) -> bool:
    """Q7: bounded printable text with no credential shape and no high-entropy token."""
    if len(text) > _MAX_OPERATOR_TEXT_LENGTH or _PRINTABLE_TEXT_PATTERN.fullmatch(text) is None:
        return False
    if _CREDENTIAL_SHAPE_PATTERN.search(text) is not None:
        return False
    return all(
        _shannon_entropy(token) < _ENTROPY_LIMIT
        for token in _HIGH_ENTROPY_TOKEN_PATTERN.findall(text)
    )


def _shannon_entropy(token: str) -> float:
    """Return the character entropy of one bounded token (Q7 only)."""
    length = len(token)
    return -sum(
        (count / length) * math.log2(count / length) for count in map(token.count, set(token))
    )


def _device_text_is_safe(text: str) -> bool:
    """Q8: bounded printable device text with no credential shape; no token score."""
    return (
        len(text) <= _MAX_DEVICE_TEXT_LENGTH
        and _PRINTABLE_TEXT_PATTERN.fullmatch(text) is not None
        and _CREDENTIAL_SHAPE_PATTERN.search(text) is None
    )


def _revision_is_admissible(revision: str) -> bool:
    """Q11: the length bound first, then the grammar and both credential shapes."""
    if len(revision) > _MAX_REVISION_LENGTH:
        return False
    return (
        _REVISION_TEXT_PATTERN.fullmatch(revision) is not None
        and _CREDENTIAL_SHAPE_PATTERN.search(revision) is None
        and _REVISION_SECRET_SHAPE_PATTERN.fullmatch(revision) is None
    )


_Check: typing.TypeAlias = typing.Callable[[object], bool]
_Rule: typing.TypeAlias = typing.Callable[[typing.Any], bool]
_F: typing.TypeAlias = ColdCheckFailure
_TOP: typing.Final = ""
_RUNTIME: typing.Final = "runtime_config"
_DEVICE: typing.Final = "device_config"
_PROVENANCE: typing.Final = "build_provenance"
_PROFILE: typing.Final = "effective_mcp_profile"


def _exact(kind: type) -> _Check:
    """Admit only values whose exact type is ``kind``; a ``bool`` is never an ``int``."""
    return lambda value: type(value) is kind


def _matches(pattern: re.Pattern[str]) -> _Check:
    """Admit only exact strings that fully match ``pattern``."""
    return lambda value: type(value) is str and pattern.fullmatch(value) is not None


def _nullable(check: _Check) -> _Check:
    """Admit ``None`` or a value passing ``check``."""
    return lambda value: value is None or check(value)


def _is_finite_float(value: object) -> bool:
    """Whether a value is exactly a finite ``float``; an ``int`` is never a ``float`` here."""
    return type(value) is float and math.isfinite(value)


def _is_str_list(value: object) -> bool:
    """Whether a value is exactly a ``list`` of exact strings."""
    return type(value) is list and all(
        type(item) is str for item in typing.cast(list[object], value)
    )


def _is_artefact_kind(value: object) -> bool:
    """Whether a value is one of the three closed v1 artefact kinds."""
    return type(value) is str and value in _ARTEFACT_KINDS


def _is_single_entry(devices: list[str] | None) -> bool:
    """Q3: exactly one recording device."""
    return devices is not None and len(devices) == 1


def _device_text_or_none(text: str | None) -> bool:
    """Q8 over one nullable device string."""
    return text is None or _device_text_is_safe(text)


def _device_texts_or_none(devices: list[str] | None) -> bool:
    """Q8 over every recording-device entry."""
    return devices is None or all(_device_text_is_safe(text) for text in devices)


def _none_or_positive(value: float | None) -> bool:
    """A nullable bound that must be strictly positive when present."""
    return value is None or value > 0


def _none_or_unit(value: float | None) -> bool:
    """A nullable bound that must lie in ``[0, 1]`` when present."""
    return value is None or 0.0 <= value <= 1.0


#: Exact-type accessors for every retained v1 value the gates read.
_Q_SHAPE: typing.Final[types.MappingProxyType[tuple[str, str], _Check]] = types.MappingProxyType(
    {
        (_TOP, "coffee_roaster_mcp_version"): _exact(str),
        (_TOP, "boot_id"): _exact(str),
        (_TOP, "credential_env_var_name"): _exact(str),
        (_TOP, "stimulus_block"): _exact(str),
        (_TOP, "operator_host_notes"): _exact(str),
        (_TOP, "operator_psu_notes"): _exact(str),
        (_TOP, "operator_cooling_notes"): _exact(str),
        (_TOP, "controller_tick_seconds"): _is_finite_float,
        (_RUNTIME, "temperature_unit"): _exact(str),
        (_RUNTIME, "first_crack_mode"): _exact(str),
        (_RUNTIME, "model_precision"): _exact(str),
        (_RUNTIME, "command_interval_seconds"): _is_finite_float,
        (_RUNTIME, "sample_interval_seconds"): _is_finite_float,
        (_RUNTIME, "auto_t0_drop_threshold_c"): _is_finite_float,
        (_DEVICE, "recording_devices"): _nullable(_is_str_list),
        (_DEVICE, "serial_port"): _nullable(_exact(str)),
        (_DEVICE, "roaster_driver"): _nullable(_exact(str)),
        (_DEVICE, "audio_input_device"): _nullable(_exact(str)),
        (_DEVICE, "mcp_yaml_source_path"): _nullable(_exact(str)),
        (_DEVICE, "ambient_device"): _nullable(_exact(str)),
        (_DEVICE, "fc_confidence_threshold"): _nullable(_is_finite_float),
        (_DEVICE, "auto_t0_drop_threshold_c"): _nullable(_is_finite_float),
        (_DEVICE, "ambient_poll_interval_seconds"): _nullable(_is_finite_float),
        (_PROVENANCE, "source_revision"): _matches(_HEX40_PATTERN),
        (_PROVENANCE, "source_tree_dirty"): _exact(bool),
        (_PROVENANCE, "artefact_kind"): _is_artefact_kind,
        (_PROVENANCE, "artefact_sha256"): _nullable(_matches(_HEX64_PATTERN)),
        (_PROFILE, "source_sha256"): _matches(_HEX64_PATTERN),
        (_PROFILE, "source_byte_length"): _exact(int),
        (_PROFILE, "first_crack_onnx_threads"): _exact(int),
        (_PROFILE, "first_crack_min_positive_windows"): _exact(int),
        (_PROFILE, "first_crack_confirmation_window_seconds"): _is_finite_float,
        (_PROFILE, "first_crack_revision"): _exact(str),
        (_PROFILE, "audio_sample_rate"): _exact(int),
        (_PROFILE, "audio_window_seconds"): _is_finite_float,
        (_PROFILE, "audio_overlap"): _is_finite_float,
        (_PROFILE, "audio_hop_seconds"): _nullable(_is_finite_float),
        (_PROFILE, "session_ror_window_seconds"): _exact(int),
        (_PROFILE, "session_ror_min_sample_seconds"): _exact(int),
    }
)

#: One row per single-value gate (Q1-Q8, Q10, Q11); Q9 pairs two values and runs separately.
_Q_RULES: typing.Final[tuple[tuple[str, str, ColdCheckFailure, _Rule], ...]] = (
    (_TOP, "coffee_roaster_mcp_version", _F.Q_MCP_VERSION, lambda v: v == _REQUIRED_MCP_VERSION),
    (_RUNTIME, "temperature_unit", _F.Q_TEMPERATURE_UNIT, lambda v: v in _ALLOWED_CELSIUS_TOKENS),
    (_DEVICE, "recording_devices", _F.Q_RECORDING_DEVICE_NOT_SINGLE, _is_single_entry),
    (
        _RUNTIME,
        "first_crack_mode",
        _F.Q_INFERENCE_NOT_ACTIVE,
        lambda v: v in _ALLOWED_INFERENCE_MODES,
    ),
    (
        _RUNTIME,
        "model_precision",
        _F.Q_INFERENCE_NOT_ACTIVE,
        lambda v: v == _REQUIRED_MODEL_PRECISION,
    ),
    (
        _TOP,
        "credential_env_var_name",
        _F.Q_CREDENTIAL_NAME,
        lambda v: v in _ALLOWED_CREDENTIAL_ENV_NAMES,
    ),
    (_TOP, "boot_id", _F.Q_BOOT_ID, lambda v: _BOOT_ID_PATTERN.fullmatch(v) is not None),
    (_TOP, "stimulus_block", _F.Q_OPERATOR_TEXT, _operator_text_is_safe),
    (_TOP, "operator_host_notes", _F.Q_OPERATOR_TEXT, _operator_text_is_safe),
    (_TOP, "operator_psu_notes", _F.Q_OPERATOR_TEXT, _operator_text_is_safe),
    (_TOP, "operator_cooling_notes", _F.Q_OPERATOR_TEXT, _operator_text_is_safe),
    (_DEVICE, "serial_port", _F.Q_DEVICE_VALUE, _device_text_or_none),
    (_DEVICE, "roaster_driver", _F.Q_DEVICE_VALUE, _device_text_or_none),
    (_DEVICE, "audio_input_device", _F.Q_DEVICE_VALUE, _device_text_or_none),
    (_DEVICE, "mcp_yaml_source_path", _F.Q_DEVICE_VALUE, _device_text_or_none),
    (_DEVICE, "ambient_device", _F.Q_DEVICE_VALUE, _device_text_or_none),
    (_DEVICE, "recording_devices", _F.Q_DEVICE_VALUE, _device_texts_or_none),
    (_DEVICE, "fc_confidence_threshold", _F.Q_DEVICE_VALUE, _none_or_unit),
    (_DEVICE, "auto_t0_drop_threshold_c", _F.Q_DEVICE_VALUE, _none_or_positive),
    (_DEVICE, "ambient_poll_interval_seconds", _F.Q_DEVICE_VALUE, _none_or_positive),
    (_PROVENANCE, "source_tree_dirty", _F.Q_SOURCE_TREE_DIRTY, lambda v: v is False),
    (_PROFILE, "source_byte_length", _F.Q_PROFILE_VALUE, lambda v: v >= 0),
    (_PROFILE, "first_crack_onnx_threads", _F.Q_PROFILE_VALUE, lambda v: v >= 1),
    (_PROFILE, "first_crack_min_positive_windows", _F.Q_PROFILE_VALUE, lambda v: v >= 1),
    (_PROFILE, "first_crack_confirmation_window_seconds", _F.Q_PROFILE_VALUE, lambda v: v > 0),
    (_PROFILE, "first_crack_revision", _F.Q_PROFILE_VALUE, _revision_is_admissible),
    (_PROFILE, "audio_sample_rate", _F.Q_PROFILE_VALUE, lambda v: v > 0),
    (_PROFILE, "audio_window_seconds", _F.Q_PROFILE_VALUE, lambda v: v > 0),
    (_PROFILE, "audio_overlap", _F.Q_PROFILE_VALUE, lambda v: 0.0 <= v < 1.0),
    (_PROFILE, "audio_hop_seconds", _F.Q_PROFILE_VALUE, _none_or_positive),
    (_PROFILE, "session_ror_window_seconds", _F.Q_PROFILE_VALUE, lambda v: v > 0),
    (_PROFILE, "session_ror_min_sample_seconds", _F.Q_PROFILE_VALUE, lambda v: v > 0),
)
_PROFILE_COMPARABLES: typing.Final = (
    "first_crack_onnx_threads",
    "first_crack_min_positive_windows",
    "first_crack_confirmation_window_seconds",
    "audio_sample_rate",
    "audio_window_seconds",
    "audio_overlap",
    "audio_hop_seconds",
    "session_ror_window_seconds",
    "session_ror_min_sample_seconds",
)


def _entry(container: object, key: str) -> object:
    """Return ``container[key]`` from an exact ``dict``, else the absent marker."""
    if type(container) is not dict:
        return _ABSENT
    return typing.cast(dict[str, object], container).get(key, _ABSENT)


def _result(check: ColdCheck, found: set[ColdCheckFailure]) -> ColdCheckResult:
    """Return one closed result listing failures uniquely in declaration order."""
    failures = tuple(member for member in ColdCheckFailure if member in found)
    return ColdCheckResult(
        check=check,
        outcome=ColdCheckOutcome.FAIL if failures else ColdCheckOutcome.PASS,
        failures=failures,
    )


def _identity_facts(view: dict[tuple[str, str], typing.Any]) -> ColdIdentityFacts:
    """Copy qualified retained values into the closed facts model."""
    return ColdIdentityFacts(
        mcp_version=view[(_TOP, "coffee_roaster_mcp_version")],
        temperature_unit=view[(_RUNTIME, "temperature_unit")],
        first_crack_mode=view[(_RUNTIME, "first_crack_mode")],
        model_precision=view[(_RUNTIME, "model_precision")],
        recording_device_count=typing.cast(
            typing.Literal[1], len(view[(_DEVICE, "recording_devices")])
        ),
        source_tree_dirty=view[(_PROVENANCE, "source_tree_dirty")],
        source_revision=view[(_PROVENANCE, "source_revision")],
        artefact_kind=view[(_PROVENANCE, "artefact_kind")],
        artefact_sha256=view[(_PROVENANCE, "artefact_sha256")],
        profile_source_sha256=view[(_PROFILE, "source_sha256")],
        profile_source_byte_length=view[(_PROFILE, "source_byte_length")],
        **{name: view[(_PROFILE, name)] for name in _PROFILE_COMPARABLES},
    )


def _qualify_identity_v1(
    identity: ColdRetainedIdentityV1,
) -> tuple[ColdCheckResult, ColdIdentityFacts | None]:
    """Apply the frozen v1 gates Q1-Q11 to one retained identity; never raises.

    Every gate is evaluated and every failure collected.  A value whose key is
    missing or whose exact type is wrong records ``Q_SHAPE_UNEXPECTED`` and is not
    evaluated further.  The tolerant-origin extras are never read.
    """
    known: object = getattr(identity, "known", None)
    found: set[ColdCheckFailure] = set()
    view: dict[tuple[str, str], typing.Any] = {}
    for path, check in _Q_SHAPE.items():
        section, key = path
        value = _entry(_entry(known, section) if section else known, key)
        if value is _ABSENT or not check(value):
            found.add(ColdCheckFailure.Q_SHAPE_UNEXPECTED)
        else:
            view[path] = value
    for section, key, failure, rule in _Q_RULES:
        if (section, key) in view and not rule(view[(section, key)]):
            found.add(failure)
    kind = view.get((_PROVENANCE, "artefact_kind"), _ABSENT)
    digest = view.get((_PROVENANCE, "artefact_sha256"), _ABSENT)
    if (
        kind is not _ABSENT
        and digest is not _ABSENT
        and (kind in _PACKAGED_ARTEFACT_KINDS) == (digest is None)
    ):
        found.add(ColdCheckFailure.Q_PROVENANCE_DIGEST)
    facts = None if found else _identity_facts(view)
    return _result(ColdCheck.IDENTITY_QUALIFICATION_V1, found), facts


# ------------------------------------------------------------- G16, G17, G18

_Sample: typing.TypeAlias = ColdTickAudioSample | FinalisationFirstCrackStatus
_LIVE_STATUS_FAILURES: typing.Final[types.MappingProxyType[str, ColdCheckFailure | None]] = (
    types.MappingProxyType(
        {
            "pending": None,
            "detected": ColdCheckFailure.FIRST_CRACK_CONFIRMED,
            "faulted": ColdCheckFailure.MICROPHONE_OR_FATAL_ERROR,
            "unavailable": ColdCheckFailure.MICROPHONE_OR_FATAL_ERROR,
            "disabled": ColdCheckFailure.INFERENCE_NOT_ACTIVE,
            "manual": ColdCheckFailure.INFERENCE_NOT_ACTIVE,
        }
    )
)
_INT_READINGS: typing.Final = (
    "queued_window_count",
    "emitted_window_count",
    "dropped_window_count",
    "processed_window_count",
    "total_overflow_count",
    "max_consecutive_overflow_count",
    "inference_overrun_count",
)
_FLOAT_READINGS: typing.Final = ("estimated_lost_audio_ms_last_minute", "max_inference_duration_ms")
_NON_DECREASING: typing.Final = (
    "total_overflow_count",
    "emitted_window_count",
    "processed_window_count",
    "max_inference_duration_ms",
    "max_consecutive_overflow_count",
)
_RECORDING_ON_ROLES: typing.Final = frozenset(
    {"primary_wav", "recording_sidecar", "annotation_session_sidecar"}
)


class _Series(typing.NamedTuple):
    """The phase-symmetric series ``L`` and ``S`` plus their presence failures."""

    live: tuple[_Sample, ...]
    series: tuple[_Sample, ...]
    pre: FinalisationFirstCrackStatus | None
    runtime: FirstCrackRuntimeFinalisationEvidence | None
    absent: frozenset[ColdCheckFailure]


def _series_of(rebound: ColdReboundPhase) -> _Series:
    """Return ``L`` (ticks, then pre-finalisation) and ``S`` (``L``, then final)."""
    absent: set[ColdCheckFailure] = set()
    ticks = tuple(record.audio for record in rebound.ticks)
    if not ticks:
        absent.add(ColdCheckFailure.TICK_EVIDENCE_ABSENT)
    result = rebound.finalisation
    pre: FinalisationFirstCrackStatus | None = None
    runtime: FirstCrackRuntimeFinalisationEvidence | None = None
    if rebound.finalisation_ambiguous:
        absent.add(ColdCheckFailure.FINALISATION_SESSION_AMBIGUOUS)
    elif result is None:
        absent.add(ColdCheckFailure.FINALISATION_EVIDENCE_ABSENT)
    else:
        pre, runtime = result.pre_finalisation_first_crack_status, result.first_crack_runtime
        if runtime is None:
            absent.add(ColdCheckFailure.FINALISATION_EVIDENCE_ABSENT)
        if pre is None:
            absent.add(ColdCheckFailure.PRE_FINALISATION_EVIDENCE_ABSENT)
    live: tuple[_Sample, ...] = ticks + ((pre,) if pre is not None else ())
    final: tuple[_Sample, ...] = (runtime.final_status,) if runtime is not None else ()
    return _Series(live, live + final, pre, runtime, frozenset(absent))


def _has_detection(sample: _Sample) -> bool:
    """Whether a sample carries any first-crack detection field."""
    return sample.detected_at_utc is not None or sample.detected_monotonic_seconds is not None


def _evaluate_inference_runtime(rebound: ColdReboundPhase) -> ColdCheckResult:
    """G16 ``INFERENCE_RUNTIME`` over ``L``, the runtime outcome and the final status.

    It requires active flags on every observed sample and at least one processed
    window before finalisation, in every phase and with no phase argument.  It is
    not a proof of sustained inference: a capture that stalls after one processed
    window is not detected here.
    """
    evidence = _series_of(rebound)
    found = set(evidence.absent)
    for sample in evidence.live:
        if sample.mode != "audio" or sample.audio_running is not True:
            found.add(ColdCheckFailure.INFERENCE_NOT_ACTIVE)
        status_failure = _LIVE_STATUS_FAILURES.get(
            sample.status, ColdCheckFailure.INFERENCE_NOT_ACTIVE
        )
        if status_failure is not None:
            found.add(status_failure)
        if _has_detection(sample):
            found.add(ColdCheckFailure.FIRST_CRACK_CONFIRMED)
        if sample.reason is not None:
            found.add(ColdCheckFailure.MICROPHONE_OR_FATAL_ERROR)
    if evidence.pre is not None and evidence.pre.processed_window_count < 1:
        found.add(ColdCheckFailure.NO_PROCESSED_WINDOW)
    if evidence.runtime is not None:
        final = evidence.runtime.final_status
        if evidence.runtime.outcome == "not_active" or final.mode != "audio":
            found.add(ColdCheckFailure.INFERENCE_NOT_ACTIVE)
        if final.status == "detected" or _has_detection(final):
            found.add(ColdCheckFailure.FIRST_CRACK_CONFIRMED)
        if final.audio_running is not False:
            found.add(ColdCheckFailure.POST_STOP_AUDIO_RUNNING)
    return _result(ColdCheck.INFERENCE_RUNTIME, found)


def _reading(sample: _Sample, name: str, found: set[ColdCheckFailure]) -> float | None:
    """Return one admitted counter reading, or record why it is unavailable."""
    value: object = getattr(sample, name, None)
    if name in _FLOAT_READINGS:
        admitted = type(value) is float and math.isfinite(value)
    else:
        admitted = type(value) is int
    if not admitted:
        found.add(ColdCheckFailure.MEASUREMENT_SHAPE_UNEXPECTED)
        return None
    number = typing.cast(float, value)
    if number < 0:
        found.add(ColdCheckFailure.NEGATIVE_MEASUREMENT)
        return None
    return number


def _is_nonzero(value: float | None) -> bool:
    """Whether an admitted reading is present and not zero."""
    return value is not None and value != 0


def _decreased(earlier: float | None, later: float | None) -> bool:
    """Whether two admitted consecutive readings decrease."""
    return earlier is not None and later is not None and later < earlier


def _queue_grew(values: tuple[float | None, ...]) -> bool:
    """Whether ``queued`` exceeds its running high-water mark on consecutive samples."""
    high: float | None = None
    streak = 0
    for value in values:
        if value is None:
            continue
        streak = streak + 1 if high is not None and value > high else 0
        if streak > 1:
            return True
        high = value if high is None or value > high else high
    return False


def _evaluate_audio_counters(rebound: ColdReboundPhase) -> ColdCheckResult:
    """G16 ``AUDIO_COUNTERS`` over ``S``; the queue-growth rule reads ``L`` only."""
    evidence = _series_of(rebound)
    found = set(evidence.absent)
    rows = [
        {name: _reading(sample, name, found) for name in (*_INT_READINGS, *_FLOAT_READINGS)}
        for sample in evidence.series
    ]
    for row in rows:
        if _is_nonzero(row["dropped_window_count"]):
            found.add(ColdCheckFailure.DROPPED_WINDOW)
        if _is_nonzero(row["inference_overrun_count"]):
            found.add(ColdCheckFailure.INFERENCE_OVERRUN)
    for earlier, later in zip(rows, rows[1:], strict=False):
        if any(_decreased(earlier[name], later[name]) for name in _NON_DECREASING):
            found.add(ColdCheckFailure.CAPTURE_RESTART)
    if _queue_grew(tuple(row["queued_window_count"] for row in rows[: len(evidence.live)])):
        found.add(ColdCheckFailure.QUEUE_GROWING)
    if evidence.runtime is not None and _is_nonzero(rows[-1]["queued_window_count"]):
        found.add(ColdCheckFailure.QUEUE_NOT_DRAINED)
    return _result(ColdCheck.AUDIO_COUNTERS, found)


def _evaluate_inference_duration(rebound: ColdReboundPhase) -> ColdCheckResult:
    """G17: the largest ``max_inference_duration_ms`` over ``S`` is below the hop."""
    evidence = _series_of(rebound)
    found = set(evidence.absent)
    readings = [_reading(sample, "max_inference_duration_ms", found) for sample in evidence.series]
    admitted = [value for value in readings if value is not None]
    if admitted and not max(admitted) < EFFECTIVE_HOP_SECONDS * 1000.0:
        found.add(ColdCheckFailure.INFERENCE_DURATION_AT_OR_ABOVE_HOP)
    return _result(ColdCheck.INFERENCE_DURATION, found)


def _recording_on_failures(recording: RecordingFinalisationEvidence) -> set[ColdCheckFailure]:
    """G18 for recording-on: finalised, the exact role trio, every artefact non-empty."""
    found: set[ColdCheckFailure] = set()
    if (
        recording.expected is not True
        or recording.outcome != "finalised"
        or recording.reason is not None
    ):
        found.add(ColdCheckFailure.RECORDING_NOT_FINALISED)
    roles = [artefact.role for artefact in recording.artifacts]
    if len(roles) != len(_RECORDING_ON_ROLES) or frozenset(roles) != _RECORDING_ON_ROLES:
        found.add(ColdCheckFailure.RECORDING_ARTEFACT_SET_UNEXPECTED)
    if any(
        artefact.exists is not True
        or type(artefact.size_bytes) is not int
        or artefact.size_bytes <= 0
        for artefact in recording.artifacts
    ):
        found.add(ColdCheckFailure.RECORDING_ARTEFACT_EMPTY)
    return found


def _evaluate_recording_artefacts(rebound: ColdReboundPhase) -> ColdCheckResult:
    """G18: the only phase-aware rule; artefact paths and filenames are never read."""
    found: set[ColdCheckFailure] = set()
    result = rebound.finalisation
    recording = None if result is None else result.recording
    if rebound.finalisation_ambiguous:
        found.add(ColdCheckFailure.FINALISATION_SESSION_AMBIGUOUS)
    elif recording is None:
        found.add(ColdCheckFailure.FINALISATION_EVIDENCE_ABSENT)
    elif rebound.phase is ColdPhaseKind.RECORDING_ON:
        found |= _recording_on_failures(recording)
    elif (
        recording.expected is not False
        or recording.outcome != "not_configured"
        or recording.artifacts
    ):
        found.add(ColdCheckFailure.RECORDING_UNEXPECTEDLY_CONFIGURED)
    return _result(ColdCheck.RECORDING_ARTEFACTS, found)


def _column(series: tuple[_Sample, ...], name: str) -> tuple[float, ...] | None:
    """Return one admitted reading per sample, or ``None`` if any is unavailable."""
    found: set[ColdCheckFailure] = set()
    values = tuple(_reading(sample, name, found) for sample in series)
    return None if found else typing.cast(tuple[float, ...], values)


def _derive_d191(rebound: ColdReboundPhase) -> ColdD191Metrics | None:
    """Derive N from the final snapshot and X as the peak over ``S``; never compared.

    The metrics are unavailable (``None``, never zeros) without ticks, without either
    snapshot, when the finalisation is ambiguous, when any value read is malformed or
    negative, or when an overflow counter decreases (a restart voids the basis).
    """
    evidence = _series_of(rebound)
    if evidence.absent or evidence.runtime is None:
        return None
    lost = _column(evidence.series, "estimated_lost_audio_ms_last_minute")
    streaks = _column(evidence.series, "max_consecutive_overflow_count")
    totals = _column(evidence.series, "total_overflow_count")
    if lost is None or streaks is None or totals is None:
        return None
    for column in (streaks, totals):
        if any(_decreased(a, b) for a, b in zip(column, column[1:], strict=False)):
            return None
    return ColdD191Metrics(
        max_consecutive_overflow_count=evidence.runtime.final_status.max_consecutive_overflow_count,
        peak_trailing_lost_audio_ms=max(lost),
    )


def _interpret_phase(rebound: ColdReboundPhase) -> ColdPhaseInterpretation:
    """Evaluate the five checks and the D191 derivation for one rebound phase."""
    qualification, facts = _qualify_identity_v1(
        rebound._identity  # pyright: ignore[reportPrivateUsage]
    )
    return ColdPhaseInterpretation(
        phase=rebound.phase,
        identity_sha256=rebound.header.identity_sha256,
        results=(
            qualification,
            _evaluate_inference_runtime(rebound),
            _evaluate_audio_counters(rebound),
            _evaluate_inference_duration(rebound),
            _evaluate_recording_artefacts(rebound),
        ),
        d191=_derive_d191(rebound),
        identity_facts=facts,
    )


def interpret_retained_run(run: ColdRetainedRun) -> ColdInterpretation:
    """Rebind one retained run, then interpret every present phase.

    Every read of the caller's run and its containers happens during rebinding;
    evaluation afterwards reads only the fresh, validated snapshots and parses held
    by the capability.
    The manifest digest is carried through, not re-verified, and no completeness,
    provenance or storage-independence property is established here.

    Args:
        run: A retained run, as the strict retained-run reader returns it.

    Returns:
        The rebound run and one interpretation per present phase, in phase order.

    Raises:
        ColdInterpretationError: If any container, binding or header check fails.
    """
    rebound = _rebind(run)
    return ColdInterpretation(
        token=_REBIND_TOKEN,
        rebound=rebound,
        phases=tuple(_interpret_phase(phase) for phase in rebound.phases),
    )
