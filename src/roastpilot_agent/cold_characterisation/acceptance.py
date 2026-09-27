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
records, streams or phases cannot be detected here.  Those properties hold only
when slice-6 composition supplies the strict retained-run reader's output under
an externally recorded manifest digest and verifies both retained copies.

Qualification (Q1-Q11) is frozen v1 policy over retained values; it never
converts, folds or trims them.  It is not live-freeze parity: packaged agent,
model and manifest constants, and D188 profile applicability, belong to the
later applicability gate.  Temperatures are Celsius only; Q2 fails closed.
"""

import enum
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
from roastpilot_agent.cold_characterisation.mcp import SessionFinalisationResult

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

    ``finalisation`` is the envelope of the last finalisation record when every
    finalisation record shares one session; differing sessions leave it ``None``
    and set ``finalisation_ambiguous``.
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
            finalisation: The selected finalisation result, if unambiguous.
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
    """Step-1 values, read from the supplied run exactly once."""

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
                if relabelled:  # pragma: no cover - validate_record keeps step 1's class and phase.
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
    """Rebind a retained run; ``run`` is read in step 1 only, never afterwards."""
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
