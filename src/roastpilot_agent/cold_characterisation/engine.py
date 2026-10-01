"""Single-phase, non-actuating cold observation engine (#954 slice 4f-c).

The engine admits one phase without creating anything, then observes it:
``start_cold_session``, ``mark_beans_added``, and ``get_roast_state`` at the
fixed 1.0 s interval for 1800.0 s from the admitted activation instant.  Each
read is retained as exactly one tick record before the pure policy evaluates
it.  Aborts are durable, closed, and text-free; nothing is ever deleted.

The engine never finalises, stops, respawns, seals, or closes anything, and it
reaches exactly five non-actuating MCP operations through ``ColdEngineMcp``.
Finalisation and session equality belong to the later phase hand-off.

Residuals: host-thread duration is bounded only by the host reader itself; a
read is bounded by ``call_timeout_seconds`` only when composed over the
timeout-bounded ``MCPServerProcess`` transport; the session clock is an MCP
software heartbeat.

Carried to later composition: use the same client, host and clock instances for
admission and observation, and admit, open and observe back-to-back; an
independent operator emergency stop is required; finalisation session
equality, qualification across every abort domain, and empty-tick-stream
rejection remain later obligations.
"""

import asyncio
import enum
import math
import time
import typing
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pydantic

from roastpilot_agent.cold_characterisation.engine_policy import (
    COLD_OBSERVATION_INTERVAL_SECONDS,
    COLD_PHASE_OBSERVATION_SECONDS,
    ColdTickDecision,
    evaluate_tick,
)
from roastpilot_agent.cold_characterisation.evidence_builders import (
    build_abort_record,
    build_host_record,
    build_run_header,
    build_tick_record,
)
from roastpilot_agent.cold_characterisation.evidence_schema import (
    MAX_TEXT_FIELD_BYTES,
    ColdAbortDomain,
    ColdEngineAbortReason,
    ColdEvidenceError,
    ColdEvidenceFailure,
    ColdEvidenceRecord,
    ColdHostAbortReason,
    ColdPhaseKind,
    ColdRunHeader,
    ColdTickRecord,
)
from roastpilot_agent.cold_characterisation.evidence_store import (
    ColdAdmittedRoot,
    ColdBindingState,
    ColdEvidenceStoreError,
    ColdEvidenceWriter,
    canonical_json,
    check_record_binding,
    open_run,
)
from roastpilot_agent.cold_characterisation.host import ColdHostBoundError, HostBoundSample
from roastpilot_agent.cold_characterisation.identity import ColdRunIdentity, identity_sha256
from roastpilot_agent.cold_characterisation.mcp import (
    ColdMcpError,
    ColdMcpTransportError,
    ColdMcpValidationError,
    ColdSessionIdentityError,
    ColdSessionPurposeError,
    ColdTickObservation,
)
from roastpilot_agent.mcp_client import (
    EventCommandResult,
    RuntimeConfigSnapshot,
    ServerInfo,
    StartRoastSessionResult,
)

_T = typing.TypeVar("_T")
_ADMISSION_TOKEN = object()
_RESULT_CONFIG = pydantic.ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)


class ColdEngineMcp(typing.Protocol):
    """The five non-actuating MCP operations the engine may reach; no finalisation."""

    async def get_server_info(self) -> ServerInfo:
        """Return the MCP server inventory."""
        ...

    async def get_runtime_config(self) -> RuntimeConfigSnapshot:
        """Return the MCP runtime configuration snapshot."""
        ...

    async def start_cold_session(self) -> StartRoastSessionResult:
        """Start one confirmed cold-characterisation session."""
        ...

    async def mark_beans_added(self) -> EventCommandResult:
        """Request the non-actuating inference-activation event."""
        ...

    async def get_roast_state(self, session_id: str | None = None) -> ColdTickObservation:
        """Read one strict cold tick for the established session."""
        ...


class ColdEngineClock(typing.Protocol):
    """Monotonic and UTC time source plus the only sleep the engine performs."""

    def monotonic(self) -> float:
        """Return monotonic seconds."""
        ...

    def utc_now_iso(self) -> str:
        """Return the current UTC instant as ISO 8601 text."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Suspend for ``seconds``.

        ``sleep`` returns after the requested duration on this port's monotonic
        clock. Standard asyncio scheduling may wake up to one platform
        clock-resolution early; this is an ordinary production timer-resolution
        residual, not a physical-determinism claim. No progress watchdog or
        independent wall-clock bound is provided. A non-advancing clock or a
        sleep that never returns may leave the phase stalled and uncompleted;
        this never qualifies the run.
        """
        ...


class ColdEngineHost(typing.Protocol):
    """Host-bound checks run on an owned worker thread."""

    def check_start_bounds(self, evidence_root: Path) -> None:
        """Enforce every host bound with the start disk floor."""
        ...

    def sample(self, evidence_root: Path) -> HostBoundSample:
        """Return one during-run host sample after enforcing its bounds."""
        ...


class ColdEngineSink(typing.Protocol):
    """The caller-owned evidence writer; the engine only appends."""

    def append(self, record: ColdEvidenceRecord) -> None:
        """Durably append one record."""
        ...


class ColdActivationHook(typing.Protocol):
    """Caller hook run once at the admitted activation instant, before any read."""

    def __call__(self, *, session_id: str, activated_monotonic: float, activated_utc: str) -> bool:
        """Return exactly ``True`` to observe the phase, or exactly ``False`` to refuse it."""
        ...


class MonotonicEngineClock:
    """Production clock over ``time.monotonic``, UTC ``datetime`` and ``asyncio.sleep``.

    ``sleep`` returns after the requested duration on this port's monotonic
    clock. Standard asyncio scheduling may wake up to one platform
    clock-resolution early; this is an ordinary production timer-resolution
    residual, not a physical-determinism claim. No progress watchdog or
    independent wall-clock bound is provided. A non-advancing clock or a sleep
    that never returns may leave the phase stalled and uncompleted; this never
    qualifies the run.
    """

    def monotonic(self) -> float:
        """Return ``time.monotonic()``."""
        return time.monotonic()

    def utc_now_iso(self) -> str:
        """Return the current UTC instant in ISO 8601 form."""
        return datetime.now(UTC).isoformat()

    async def sleep(self, seconds: float) -> None:
        """Suspend with ``asyncio.sleep``.

        Args:
            seconds: Seconds to sleep.
        """
        await asyncio.sleep(seconds)


class ColdAdmissionFailure(enum.Enum):
    """Closed reasons a phase is refused before, or incomplete after, evidence creation."""

    ROOT_NOT_ADMITTED = "root_not_admitted"
    PHASE_NOT_ADMITTED = "phase_not_admitted"
    IDENTITY_NOT_ADMITTED = "identity_not_admitted"
    SOURCE_TREE_DIRTY = "source_tree_dirty"
    EVIDENCE_ROOT_MISMATCH = "evidence_root_mismatch"
    TICK_INTERVAL_MISMATCH = "tick_interval_mismatch"
    RECORDING_CONFIG_MISMATCH = "recording_config_mismatch"
    MCP_READ_FAILED = "mcp_read_failed"
    MCP_IDENTITY_DRIFT = "mcp_identity_drift"
    CLOCK_INVALID = "clock_invalid"
    HEADER_NOT_BOUND = "header_not_bound"
    HOST_START_BOUND_FAILED = "host_start_bound_failed"
    ADMISSION_NOT_VALID = "admission_not_valid"
    EVIDENCE_OPEN_FAILED = "evidence_open_failed"
    HEADER_APPEND_FAILED = "header_append_failed"


class ColdAdmissionRefusedError(RuntimeError):
    """A phase was refused before any evidence was created."""

    failure: ColdAdmissionFailure

    def __init__(self, failure: ColdAdmissionFailure) -> None:
        """Create a content-free refusal.

        Args:
            failure: Closed refusal reason.
        """
        super().__init__("Cold phase admission refused.")
        self.failure = failure


class ColdEvidenceIncompleteError(RuntimeError):
    """Evidence creation began but could not be completed; nothing is deleted."""

    failure: ColdAdmissionFailure

    def __init__(self, failure: ColdAdmissionFailure) -> None:
        """Create a content-free incomplete-evidence error.

        Args:
            failure: ``EVIDENCE_OPEN_FAILED`` or ``HEADER_APPEND_FAILED``.
        """
        super().__init__("Cold phase evidence is incomplete.")
        self.failure = failure


class ColdEngineUnexpectedError(RuntimeError):
    """Fixed closed error for an unexpected engine failure; carries no input text.

    The retained ``session_id`` may be refused, untrusted or unbounded; it is for
    private hand-off only and is never written to a public log, a report or a
    new record without readmission.  ``session_id=None`` is not proof that no
    session exists.
    """

    abort_recorded: bool
    session_id: str | None

    def __init__(self, *, abort_recorded: bool, session_id: str | None) -> None:
        """Create the fixed error; neither attribute enters ``args``.

        Args:
            abort_recorded: Whether the ``UNEXPECTED_FAILURE`` abort was durably recorded.
            session_id: The established, byte-bounded session id, if any.
        """
        super().__init__("Cold engine failed unexpectedly.")
        self.abort_recorded = abort_recorded
        self.session_id = session_id


class ColdAbortClassification(pydantic.BaseModel):
    """One closed abort classification in the HOST, EVIDENCE or ENGINE domain."""

    model_config = _RESULT_CONFIG

    domain: ColdAbortDomain
    reason: ColdHostAbortReason | ColdEvidenceFailure | ColdEngineAbortReason

    @pydantic.model_validator(mode="after")
    def _require_pair(self) -> typing.Self:
        """Admit exactly the three domain/reason-class pairs the engine produces."""
        pairs: dict[ColdAbortDomain, type[enum.Enum]] = {
            ColdAbortDomain.HOST: ColdHostAbortReason,
            ColdAbortDomain.EVIDENCE: ColdEvidenceFailure,
            ColdAbortDomain.ENGINE: ColdEngineAbortReason,
        }
        if type(self.reason) is not pairs.get(self.domain):
            raise ValueError("abort domain and reason are not a closed pair")
        return self


class ColdPhaseCompleted(pydantic.BaseModel):
    """The phase observation window ended without an abort.

    Completed means the 1800 s observation window elapsed without an abort. It is
    not qualification: tick_count may be 0, and 4g G25 must reject an empty tick
    stream and perform finalisation-equality checks.
    """

    model_config = _RESULT_CONFIG

    session_id: str = pydantic.Field(min_length=1, max_length=MAX_TEXT_FIELD_BYTES)
    observation_end_monotonic: float = pydantic.Field(ge=0)
    observation_end_utc: str = pydantic.Field(min_length=1, max_length=MAX_TEXT_FIELD_BYTES)
    tick_count: int = pydantic.Field(ge=0)


class ColdPhaseActivationRefused(pydantic.BaseModel):
    """The activation hook refused the phase after activation and before any read.

    No abort record is retained for this result; the caller records the refusal.
    """

    model_config = _RESULT_CONFIG

    session_id: str = pydantic.Field(min_length=1, max_length=MAX_TEXT_FIELD_BYTES)


class ColdPhaseAborted(pydantic.BaseModel):
    """The phase aborted with closed classifications; ``abort_recorded`` is honest.

    The retained ``session_id`` may be refused, untrusted or unbounded; it is for
    private hand-off only and is never written to a public log, a report or a
    new record without readmission.  ``session_id=None`` is not proof that no
    session exists after a timeout or cancellation, and a failed tick append is
    not proof that the commanded state was safe.
    """

    model_config = _RESULT_CONFIG

    aborts: tuple[ColdAbortClassification, ...] = pydantic.Field(min_length=1)
    session_id: str | None
    abort_recorded: bool


class ColdPhaseAdmission:
    """Token-checked capability for one admitted phase; creates nothing itself.

    Assignment, deletion and token-less construction raise.  Deliberate
    ``object.__new__`` or ``object.__setattr__`` reflection is not prevented; the
    observer re-validates every slot on entry.  This is not a sandbox.
    """

    __slots__ = ("frozen_driver", "header", "identity", "phase", "root")

    identity: ColdRunIdentity
    phase: ColdPhaseKind
    header: ColdRunHeader
    root: ColdAdmittedRoot
    frozen_driver: str

    def __init__(
        self,
        *,
        identity: ColdRunIdentity,
        phase: ColdPhaseKind,
        header: ColdRunHeader,
        root: ColdAdmittedRoot,
        frozen_driver: str,
        token: object,
    ) -> None:
        """Mint the capability; only :func:`admit_cold_phase` holds the token.

        Args:
            identity: The revalidated phase identity.
            phase: The admitted phase.
            header: The bound, not yet appended, phase header.
            root: The admitted evidence root.
            frozen_driver: The roaster driver frozen from the identity.
            token: Private admission token.

        Raises:
            ColdAdmissionRefusedError: If constructed outside admission or re-initialised.
        """
        if token is not _ADMISSION_TOKEN or hasattr(self, "identity"):
            raise ColdAdmissionRefusedError(ColdAdmissionFailure.ADMISSION_NOT_VALID)
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "header", header)
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "frozen_driver", frozen_driver)

    def __setattr__(self, name: str, value: object) -> typing.NoReturn:
        """Refuse every ordinary assignment.

        Args:
            name: Attribute name.
            value: Assigned value.

        Raises:
            ColdAdmissionRefusedError: Always.
        """
        del name, value
        raise ColdAdmissionRefusedError(ColdAdmissionFailure.ADMISSION_NOT_VALID)

    def __delattr__(self, name: str) -> typing.NoReturn:
        """Refuse every ordinary deletion.

        Args:
            name: Attribute name.

        Raises:
            ColdAdmissionRefusedError: Always.
        """
        del name
        raise ColdAdmissionRefusedError(ColdAdmissionFailure.ADMISSION_NOT_VALID)


class _Instant(typing.NamedTuple):
    """One admitted clock sample."""

    monotonic: float
    utc: str


class _Admitted(typing.NamedTuple):
    """Slots of a re-validated admission, read once."""

    identity: ColdRunIdentity
    phase: ColdPhaseKind
    header: ColdRunHeader
    root: ColdAdmittedRoot
    root_path: str
    frozen_driver: str


def _instant_is_admissible(monotonic: object, utc: object, previous: _Instant | None) -> bool:
    """Whether one clock sample is finite, ordered, bounded, and an exact-UTC instant."""
    if type(monotonic) is not float or not math.isfinite(monotonic) or monotonic < 0.0:
        return False
    if previous is not None and monotonic < previous.monotonic:
        return False
    if type(utc) is not str:
        return False
    try:
        size = len(utc.encode("utf-8"))
        offset = datetime.fromisoformat(utc).utcoffset()
    except ValueError:
        return False
    return 1 <= size <= MAX_TEXT_FIELD_BYTES and offset == timedelta(0)


def _meets_floor(instant: _Instant, floor: object) -> bool:
    """Whether an admitted instant is at or after an optional exact finite floor."""
    if floor is None:
        return True
    if type(floor) is not float or not math.isfinite(floor) or floor < 0.0:
        return False
    return instant.monotonic >= floor


def _admit_instant(clock: ColdEngineClock, previous: _Instant | None) -> _Instant | None:
    """Sample the clock once; return the admitted pair or ``None``."""
    try:
        monotonic = clock.monotonic()
        utc = clock.utc_now_iso()
    except Exception:
        return None
    if not _instant_is_admissible(monotonic, utc, previous):
        return None
    return _Instant(monotonic, utc)


def _discard(future: "asyncio.Future[typing.Any]") -> None:
    """Observe and discard a completed future's outcome so none goes unreported."""
    if future.done() and not future.cancelled():
        future.exception()


async def _owned_thread(fn: Callable[[Path], _T], arg: Path) -> _T:
    """Run ``fn(arg)`` on one owned, shielded worker thread.

    On cancellation the thread is awaited to completion, its result or exception
    is retrieved once and discarded, and one ``CancelledError`` is re-raised.
    Cancellation always wins, including when the thread finished before the
    cancellation was delivered.  The explicit retrieval is defensive ownership,
    not a fix for a demonstrated leak: a standard ``asyncio.shield`` already
    observes the inner outcome on CPython 3.11.
    """
    task = asyncio.ensure_future(asyncio.to_thread(fn, arg))
    guard = asyncio.shield(task)
    try:
        return await guard
    except asyncio.CancelledError:
        _discard(guard)
    retrieved = False
    while not task.done():
        guard = asyncio.shield(task)
        try:
            await guard
            retrieved = True
        except asyncio.CancelledError:
            _discard(guard)
        except Exception:
            retrieved = True
    if not retrieved:
        _discard(task)
    raise asyncio.CancelledError


def _admitted_root_path(root: object) -> str | None:
    """Read all three root slots; return the exact-``str`` path or ``None``."""
    if type(root) is not ColdAdmittedRoot:
        return None
    try:
        path: object = root.path
        _ = (root.realpath, root.lineage)
    except AttributeError:
        return None
    return path if type(path) is str else None


def _revalidated_identity(identity: ColdRunIdentity) -> ColdRunIdentity | None:
    """Re-parse the identity from canonical JSON and require a lossless round trip."""
    if type(identity) is not ColdRunIdentity:
        return None
    try:
        text = canonical_json(identity.model_dump(mode="json"))
        again = ColdRunIdentity.model_validate_json(text, strict=True)
        lossless = canonical_json(again.model_dump(mode="json")) == text and again == identity
    except Exception:
        return None
    return again if lossless else None


def _recording_matches(identity: ColdRunIdentity, phase: ColdPhaseKind) -> bool:
    """Whether both recording flags are exactly the phase's polarity; ``None`` refuses."""
    device = identity.device_config
    expected = phase is ColdPhaseKind.RECORDING_ON
    return device.recording_enabled is expected and device.recording_autocapture is expected


async def _read_mcp_identity(
    mcp: ColdEngineMcp,
) -> tuple[ServerInfo, RuntimeConfigSnapshot] | None:
    """Read the server inventory and runtime config; ``None`` on any failure."""
    try:
        return await mcp.get_server_info(), await mcp.get_runtime_config()
    except Exception:
        return None


def _bound_header(
    identity: ColdRunIdentity, phase: ColdPhaseKind, instant: _Instant, root_path: str
) -> ColdRunHeader | None:
    """Build the phase header and prove it binds to the run and root, or ``None``."""
    try:
        header = build_run_header(
            identity=identity,
            phase=phase,
            recorded_at_utc=instant.utc,
            monotonic_seconds=instant.monotonic,
        )
        check_record_binding(ColdBindingState(identity.run_id), header, writer_root=root_path)
    except Exception:
        return None
    return header


def _validated_admission(admission: ColdPhaseAdmission) -> _Admitted | None:
    """Read every admission slot once and re-check it; ``None`` on any failure."""
    try:
        if type(admission) is not ColdPhaseAdmission:
            return None
        identity = admission.identity
        phase = admission.phase
        header = admission.header
        root = admission.root
        frozen_driver = admission.frozen_driver
        root_path = _admitted_root_path(root)
        valid = (
            type(identity) is ColdRunIdentity
            and type(phase) is ColdPhaseKind
            and type(header) is ColdRunHeader
            and root_path is not None
            and type(frozen_driver) is str
            and header.phase is phase
            and header.run_id == identity.run_id
            and header.identity_sha256 == identity_sha256(identity)
            and frozen_driver == identity.runtime_config.roaster_driver
            and root_path == identity.pi_evidence_root
            and _instant_is_admissible(header.monotonic_seconds, header.recorded_at_utc, None)
        )
    except Exception:
        return None
    if not valid or root_path is None:
        return None
    return _Admitted(identity, phase, header, root, root_path, frozen_driver)


async def admit_cold_phase(
    *,
    identity: ColdRunIdentity,
    phase: ColdPhaseKind,
    root: ColdAdmittedRoot,
    mcp: ColdEngineMcp,
    host: ColdEngineHost,
    clock: ColdEngineClock,
    not_before_monotonic: float | None = None,
) -> ColdPhaseAdmission:
    """Admit one phase in order, creating nothing.

    Args:
        identity: The frozen phase identity; it is revalidated losslessly.
        phase: The phase to admit.
        root: The admitted evidence root.
        mcp: The five-operation MCP port (only the two identity reads are used).
        host: The host-bound port (only the start check is used).
        clock: The engine clock.
        not_before_monotonic: Optional exact finite floor the admission instant
            must not precede; ``None`` applies no floor.

    Returns:
        The admission capability.

    Raises:
        ColdAdmissionRefusedError: With the first failed step's closed member.
        asyncio.CancelledError: After reaping the host start check.
    """
    if type(phase) is not ColdPhaseKind:
        raise ColdAdmissionRefusedError(ColdAdmissionFailure.PHASE_NOT_ADMITTED)
    root_path = _admitted_root_path(root)
    if root_path is None:
        raise ColdAdmissionRefusedError(ColdAdmissionFailure.ROOT_NOT_ADMITTED)
    again = _revalidated_identity(identity)
    if again is None:
        raise ColdAdmissionRefusedError(ColdAdmissionFailure.IDENTITY_NOT_ADMITTED)
    if again.build_provenance.source_tree_dirty is not False:
        raise ColdAdmissionRefusedError(ColdAdmissionFailure.SOURCE_TREE_DIRTY)
    if again.pi_evidence_root != root_path:
        raise ColdAdmissionRefusedError(ColdAdmissionFailure.EVIDENCE_ROOT_MISMATCH)
    if again.controller_tick_seconds != COLD_OBSERVATION_INTERVAL_SECONDS:
        raise ColdAdmissionRefusedError(ColdAdmissionFailure.TICK_INTERVAL_MISMATCH)
    if not _recording_matches(again, phase):
        raise ColdAdmissionRefusedError(ColdAdmissionFailure.RECORDING_CONFIG_MISMATCH)
    reads = await _read_mcp_identity(mcp)
    if reads is None:
        raise ColdAdmissionRefusedError(ColdAdmissionFailure.MCP_READ_FAILED)
    if reads != (again.server_info, again.runtime_config):
        raise ColdAdmissionRefusedError(ColdAdmissionFailure.MCP_IDENTITY_DRIFT)
    instant = _admit_instant(clock, None)
    if instant is None or not _meets_floor(instant, not_before_monotonic):
        raise ColdAdmissionRefusedError(ColdAdmissionFailure.CLOCK_INVALID)
    header = _bound_header(again, phase, instant, root_path)
    if header is None:
        raise ColdAdmissionRefusedError(ColdAdmissionFailure.HEADER_NOT_BOUND)
    host_failed = False
    try:
        await _owned_thread(host.check_start_bounds, Path(root_path))
    except Exception:
        host_failed = True
    if host_failed:
        raise ColdAdmissionRefusedError(ColdAdmissionFailure.HOST_START_BOUND_FAILED)
    return ColdPhaseAdmission(
        identity=again,
        phase=phase,
        header=header,
        root=root,
        frozen_driver=again.runtime_config.roaster_driver,
        token=_ADMISSION_TOKEN,
    )


def open_phase_evidence(admission: ColdPhaseAdmission) -> ColdEvidenceWriter:
    """Create the run directory for one admitted phase; the engine's only creation.

    The caller owns the returned writer; the engine never seals or closes it.

    Args:
        admission: The admission capability.

    Returns:
        The new run writer.

    Raises:
        ColdAdmissionRefusedError: ``ADMISSION_NOT_VALID`` for a forged admission.
        ColdEvidenceIncompleteError: ``EVIDENCE_OPEN_FAILED`` if creation fails.
    """
    admitted = _validated_admission(admission)
    if admitted is None:
        raise ColdAdmissionRefusedError(ColdAdmissionFailure.ADMISSION_NOT_VALID)
    writer: ColdEvidenceWriter | None = None
    try:
        writer = open_run(admitted.root, admitted.identity.run_id)
    except Exception:
        writer = None
    if writer is None:
        raise ColdEvidenceIncompleteError(ColdAdmissionFailure.EVIDENCE_OPEN_FAILED)
    return writer


class _Classified(Exception):
    """Internal signal: classified aborts to record, then return."""

    def __init__(self, *aborts: ColdAbortClassification) -> None:
        super().__init__()
        self.aborts = aborts


class _SinkFailed(Exception):
    """Internal signal: an append failed; no further append is attempted."""

    def __init__(self, abort: ColdAbortClassification | None) -> None:
        super().__init__()
        self.abort = abort


class _ActivationRefused(Exception):
    """Internal signal: the activation hook returned exactly ``False``."""

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id


class _HookResultNotAdmitted(Exception):
    """Internal signal: the activation hook returned a non-``bool``."""


def _engine(reason: ColdEngineAbortReason) -> ColdAbortClassification:
    """Return one ENGINE-domain classification."""
    return ColdAbortClassification(domain=ColdAbortDomain.ENGINE, reason=reason)


class _PhaseRun:
    """Mutable state of one observed phase."""

    def __init__(
        self,
        admitted: _Admitted,
        sink: ColdEngineSink,
        mcp: ColdEngineMcp,
        host: ColdEngineHost,
        clock: ColdEngineClock,
        hook: ColdActivationHook | None = None,
    ) -> None:
        self._admitted = admitted
        self._sink = sink
        self._mcp = mcp
        self._host = host
        self._clock = clock
        self._hook = hook
        self._sink_usable = True
        self._last_valid = _Instant(
            admitted.header.monotonic_seconds, admitted.header.recorded_at_utc
        )
        self._session_id: str | None = None

    def _append(self, record: ColdEvidenceRecord) -> None:
        """Append one tick or host record; any failure disables the sink."""
        failure: ColdEvidenceFailure | None = None
        typed = False
        try:
            self._sink.append(record)
        except ColdEvidenceError as error:
            self._sink_usable = False
            typed, failure = True, error.failure
        except ColdEvidenceStoreError:
            self._sink_usable = False
            typed = True
        except Exception:
            self._sink_usable = False
        if self._sink_usable:
            return
        if not typed:
            raise _SinkFailed(None)
        if failure is None:
            raise _SinkFailed(_engine(ColdEngineAbortReason.UNEXPECTED_FAILURE))
        raise _SinkFailed(ColdAbortClassification(domain=ColdAbortDomain.EVIDENCE, reason=failure))

    def _record_aborts(self, aborts: tuple[ColdAbortClassification, ...]) -> bool:
        """Record aborts in order with the last valid instant; stop at the first failure."""
        if not self._sink_usable:
            return False
        instant = self._last_valid
        for abort in aborts:
            try:
                record = build_abort_record(
                    header=self._admitted.header,
                    domain=abort.domain,
                    reason=abort.reason,
                    recorded_at_utc=instant.utc,
                    monotonic_seconds=instant.monotonic,
                )
            except Exception:
                return False
            try:
                self._sink.append(record)
            except Exception:
                self._sink_usable = False
                return False
        return True

    def _sample(self) -> _Instant:
        """Admit one clock sample at a sampled position, or classify ``CLOCK_INVALID``."""
        instant = _admit_instant(self._clock, self._last_valid)
        if instant is None:
            raise _Classified(_engine(ColdEngineAbortReason.CLOCK_INVALID))
        self._last_valid = instant
        return instant

    async def _sleep(self, seconds: float) -> None:
        """Sleep through the clock port; a raising sleep is ``CLOCK_INVALID``."""
        failed = False
        try:
            await self._clock.sleep(seconds)
        except Exception:
            failed = True
        if failed:
            raise _Classified(_engine(ColdEngineAbortReason.CLOCK_INVALID))

    async def _start(self) -> str:
        """Start the cold session and bound its id before activation.

        A returned exact ``str`` id is retained for the result even when it is
        refused, so a later phase hand-off can still name the started session.
        """
        session_id: object = None
        try:
            session_id = (await self._mcp.start_cold_session()).session.session_id
        except ColdMcpError:
            session_id = None
        size = 0
        if type(session_id) is str:
            self._session_id = session_id
            try:
                size = len(session_id.encode("utf-8"))
            except UnicodeEncodeError:
                size = 0
        if type(session_id) is not str or not 1 <= size <= MAX_TEXT_FIELD_BYTES:
            raise _Classified(_engine(ColdEngineAbortReason.SESSION_START_FAILED))
        return session_id

    async def _activate(self) -> _Instant:
        """Request activation, then admit the activation instant (P1)."""
        failed = False
        try:
            await self._mcp.mark_beans_added()
        except ColdMcpError:
            failed = True
        if failed:
            raise _Classified(_engine(ColdEngineAbortReason.ACTIVATION_FAILED))
        return self._sample()

    async def _read(self, session_id: str) -> ColdTickObservation:
        """Read one tick with no retry; map closed MCP failures by type."""
        reason: ColdEngineAbortReason
        try:
            return await self._mcp.get_roast_state(session_id=session_id)
        except (ColdSessionIdentityError, ColdSessionPurposeError):
            reason = ColdEngineAbortReason.MCP_SESSION_IDENTITY_CHANGED
        except ColdMcpTransportError:
            reason = ColdEngineAbortReason.MCP_TRANSPORT_FAILED
        except ColdMcpValidationError:
            reason = ColdEngineAbortReason.MCP_RESPONSE_NOT_ADMITTED
        raise _Classified(_engine(reason))

    def _build_tick(
        self, tick: int, done: _Instant, observation: ColdTickObservation
    ) -> ColdTickRecord:
        """Build one tick record; a builder refusal is an EVIDENCE abort."""
        failure: ColdEvidenceFailure
        try:
            return build_tick_record(
                header=self._admitted.header,
                tick=tick,
                recorded_at_utc=done.utc,
                monotonic_seconds=done.monotonic,
                observation=observation,
            )
        except ColdEvidenceError as error:
            failure = error.failure
        raise _Classified(ColdAbortClassification(domain=ColdAbortDomain.EVIDENCE, reason=failure))

    async def _host_record(self) -> _Instant:
        """Sample the host on an owned thread, then retain one host record.

        Returns:
            The admitted post-sample instant (P5).
        """
        value: str
        try:
            sample = await _owned_thread(self._host.sample, Path(self._admitted.root_path))
        except ColdHostBoundError as error:
            value = error.failure.value
        else:
            after = self._sample()
            failure: ColdEvidenceFailure
            try:
                record = build_host_record(
                    header=self._admitted.header,
                    sample=sample,
                    recorded_at_utc=after.utc,
                    monotonic_seconds=after.monotonic,
                )
            except ColdEvidenceError as error:
                failure = error.failure
            else:
                self._append(record)
                return after
            raise _Classified(
                ColdAbortClassification(domain=ColdAbortDomain.EVIDENCE, reason=failure)
            )
        raise _Classified(
            ColdAbortClassification(domain=ColdAbortDomain.HOST, reason=ColdHostAbortReason(value))
        )

    async def observe(self) -> "ColdPhaseCompleted | ColdPhaseAborted | ColdPhaseActivationRefused":
        """Observe the phase and convert every failure into a closed result or error."""
        aborts: tuple[ColdAbortClassification, ...] = ()
        sink_abort: ColdAbortClassification | None = None
        refused: str | None = None
        sink_failed = cancelled = False
        try:
            return await self._loop()
        except _Classified as signal:
            aborts = signal.aborts
        except _SinkFailed as signal:
            sink_failed, sink_abort = True, signal.abort
        except _ActivationRefused as signal:
            refused = signal.session_id
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            pass
        if refused is not None:
            return ColdPhaseActivationRefused(session_id=refused)
        if aborts:
            recorded = self._record_aborts(aborts)
            return ColdPhaseAborted(
                aborts=aborts, session_id=self._session_id, abort_recorded=recorded
            )
        if cancelled:
            self._record_aborts((_engine(ColdEngineAbortReason.CANCELLED),))
            raise asyncio.CancelledError
        if sink_failed:
            if sink_abort is not None:
                return ColdPhaseAborted(
                    aborts=(sink_abort,), session_id=self._session_id, abort_recorded=False
                )
            raise ColdEngineUnexpectedError(abort_recorded=False, session_id=self._session_id)
        recorded = self._record_aborts((_engine(ColdEngineAbortReason.UNEXPECTED_FAILURE),))
        raise ColdEngineUnexpectedError(abort_recorded=recorded, session_id=self._session_id)

    async def _loop(self) -> ColdPhaseCompleted:
        """Run the fixed-interval observation loop until the deadline or an abort."""
        session_id = await self._start()
        activation = await self._activate()
        if self._hook is not None:
            verdict: object = self._hook(
                session_id=session_id,
                activated_monotonic=activation.monotonic,
                activated_utc=activation.utc,
            )
            if type(verdict) is not bool:
                raise _HookResultNotAdmitted
            if verdict is False:
                raise _ActivationRefused(session_id)
        end = activation.monotonic + COLD_PHASE_OBSERVATION_SECONDS
        next_start = activation.monotonic
        previous: float | None = None
        tick = 0
        while True:
            now = self._sample()
            if now.monotonic >= end:
                return self._completed(session_id, now, tick)
            if now.monotonic < next_start:
                await self._sleep(next_start - now.monotonic)
                now = self._sample()
                if now.monotonic >= end:
                    return self._completed(session_id, now, tick)
            read_start = now
            observation = await self._read(session_id)
            done = self._sample()
            record = self._build_tick(tick, done, observation)
            self._append(record)
            decision: ColdTickDecision = evaluate_tick(
                record,
                established_session_id=session_id,
                frozen_driver=self._admitted.frozen_driver,
                since_activation_seconds=done.monotonic - activation.monotonic,
                previous_elapsed=previous,
            )
            if decision.reasons:
                raise _Classified(*(_engine(reason) for reason in decision.reasons))
            after = await self._host_record()
            previous = decision.next_previous_elapsed_seconds
            tick += 1
            next_start = max(
                read_start.monotonic + COLD_OBSERVATION_INTERVAL_SECONDS, after.monotonic
            )

    @staticmethod
    def _completed(session_id: str, now: _Instant, tick: int) -> ColdPhaseCompleted:
        """Return the elapsed-window result; never a qualification."""
        return ColdPhaseCompleted(
            session_id=session_id,
            observation_end_monotonic=now.monotonic,
            observation_end_utc=now.utc,
            tick_count=tick,
        )


@typing.overload
async def observe_cold_phase(
    *,
    admission: ColdPhaseAdmission,
    sink: ColdEngineSink,
    mcp: ColdEngineMcp,
    host: ColdEngineHost,
    clock: ColdEngineClock,
    activation_hook: None = None,
) -> ColdPhaseCompleted | ColdPhaseAborted: ...


@typing.overload
async def observe_cold_phase(
    *,
    admission: ColdPhaseAdmission,
    sink: ColdEngineSink,
    mcp: ColdEngineMcp,
    host: ColdEngineHost,
    clock: ColdEngineClock,
    activation_hook: ColdActivationHook,
) -> ColdPhaseCompleted | ColdPhaseAborted | ColdPhaseActivationRefused: ...


async def observe_cold_phase(
    *,
    admission: ColdPhaseAdmission,
    sink: ColdEngineSink,
    mcp: ColdEngineMcp,
    host: ColdEngineHost,
    clock: ColdEngineClock,
    activation_hook: ColdActivationHook | None = None,
) -> ColdPhaseCompleted | ColdPhaseAborted | ColdPhaseActivationRefused:
    """Append the phase header, then observe one phase to its deadline or an abort.

    Args:
        admission: The admission capability; every slot is re-validated first.
        sink: The caller-owned writer for this phase's run.
        mcp: The five-operation MCP port.
        host: The host-bound port.
        clock: The engine clock.
        activation_hook: Optional hook called exactly once immediately after the
            admitted activation instant and before the first sample or read.
            Exactly ``False`` returns ``ColdPhaseActivationRefused`` with no abort
            record; a non-``bool`` or raising hook is an unexpected failure.

    Returns:
        ``ColdPhaseCompleted`` when the window elapsed without an abort (not
        qualification), ``ColdPhaseAborted`` with closed classifications, or
        ``ColdPhaseActivationRefused`` when the activation hook refused.

    Raises:
        ColdAdmissionRefusedError: ``ADMISSION_NOT_VALID`` before any append.
        ColdEvidenceIncompleteError: ``HEADER_APPEND_FAILED``.
        ColdEngineUnexpectedError: For an unexpected or unknown sink failure.
        asyncio.CancelledError: After reaping host work and recording ``CANCELLED`` once.
    """
    admitted = _validated_admission(admission)
    if admitted is None:
        raise ColdAdmissionRefusedError(ColdAdmissionFailure.ADMISSION_NOT_VALID)
    appended = True
    try:
        sink.append(admitted.header)
    except Exception:
        appended = False
    if not appended:
        raise ColdEvidenceIncompleteError(ColdAdmissionFailure.HEADER_APPEND_FAILED)
    return await _PhaseRun(admitted, sink, mcp, host, clock, activation_hook).observe()
