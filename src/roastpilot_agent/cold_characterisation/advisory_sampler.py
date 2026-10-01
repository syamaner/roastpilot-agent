"""Standalone observation-only advisory sampler for cold characterisation (#954 5c-i).

The sampler drives an injected production advisor port inside one phase's fixed
D200 advisory window and records every attempt through the 5a builders: an intent
line is appended durably before any provider task exists, and at most one
resolution line closes it.  It is observation-only (D201): it has no MCP, control,
controller, lifecycle, finalisation, stop, kill, join or process-exit capability,
and an ``ALLOW`` verdict or ``should_drop`` is retained as data only.  Nothing
wires it into a runtime.

Timing is aimed at, never guaranteed.  The first intent is made once the observed
clock reaches the window open; each later due instant is the observed completion
plus the configured dwell.  The configured per-call bound is not a timeout, grace
or watchdog: once the observed clock reaches the deadline the sampler requests one
cancellation, records the abandonment and stops, which never proves that the
provider request stopped.  At most one provider task is outstanding at a time.

Port contract: ports must not call back into the sampler (reentry is handled fail
closed and yields a provisional, unstored settlement fact), and ``clock.sleep``
must end promptly when cancelled.  A clock that violates the latter can stall
:meth:`ColdAdvisorySampler.run` with no time bound; no watchdog is provided and no
qualification or observation is claimed in that case.  ``KeyboardInterrupt`` and
``SystemExit`` are not isolated.  No exception, result or argument text is
retained or formatted, but the advisor itself may format provider errors, so no
global redaction claim is made.
"""

import asyncio
import enum
import math
import typing

import pydantic

from roastpilot_agent.advisor import (
    AdvisorContext,
    AdvisorDescriptor,
    AdvisorMalformedOutputError,
    AdvisorProviderError,
    AdvisorUnsafeOutputError,
    AdvisorUsage,
    RoastDecision,
)
from roastpilot_agent.cold_characterisation.advisory_window import (
    MIN_POST_COMPLETION_DWELL_SECONDS,
    advisory_window_bounds,
)
from roastpilot_agent.cold_characterisation.evidence_advisory import (
    ColdAdvisoryAttemptRecord,
    ColdAdvisoryResolution,
    ColdAdvisoryUsageReading,
    build_advisory_intent_record,
    build_advisory_resolution_record,
)
from roastpilot_agent.cold_characterisation.evidence_lifecycle import (
    is_admissible_monotonic,
    is_admissible_session_id,
    is_admissible_utc_instant,
)
from roastpilot_agent.cold_characterisation.evidence_schema import (
    MAX_TEXT_FIELD_BYTES,
    ColdRunHeader,
    ColdSafetyEvaluation,
    ColdSafetyVerdict,
    ColdTickRecord,
    validate_record,
)
from roastpilot_agent.models import RoastPhase
from roastpilot_agent.safety import SafetyEvaluation, SafetyVerdict

__all__ = (
    "ColdAdvisoryAdvisorPort",
    "ColdAdvisoryClockPort",
    "ColdAdvisoryEvaluatorPort",
    "ColdAdvisoryProviderTask",
    "ColdAdvisorySampler",
    "ColdAdvisorySamplerRefusedError",
    "ColdAdvisorySamplerRun",
    "ColdAdvisorySamplerStop",
    "ColdAdvisorySettlement",
    "ColdAdvisorySettlementClosure",
    "ColdAdvisorySinkPort",
    "ColdAdvisorySpec",
    "ColdAdvisoryTickPort",
)


class ColdAdvisoryAdvisorPort(typing.Protocol):
    """The production advisor surface the sampler reads; ``PydanticAIAdvisor`` fits it."""

    @property
    def last_usage(self) -> AdvisorUsage | None:
        """The most recent production-normalised usage reading, or ``None``."""
        ...

    def descriptor_for(self, phase: RoastPhase) -> AdvisorDescriptor:
        """Return the phase-resolved advisor trace identity for ``phase``."""
        ...

    async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
        """Return one typed advisory recommendation for ``context``."""
        ...


class ColdAdvisoryEvaluatorPort(typing.Protocol):
    """The typed safety evaluation the sampler requests; ``SafetyPolicy`` fits it."""

    def evaluate_command(
        self,
        *,
        requested_heat: int,
        requested_fan: int,
        seconds_since_last_command: None,
        bounds: None = None,
    ) -> SafetyEvaluation:
        """Evaluate the returned request alone, with no prior command and no bounds."""
        ...


class ColdAdvisorySinkPort(typing.Protocol):
    """The durable advisory-attempt append; ``ColdEvidenceWriter`` fits it."""

    def append_advisory_attempt(self, record: ColdAdvisoryAttemptRecord) -> None:
        """Durably append one advisory-attempt record, or raise."""
        ...


class ColdAdvisoryTickPort(typing.Protocol):
    """The latest admitted retained tick of the phase being observed."""

    def latest_retained_tick(self) -> ColdTickRecord | None:
        """Return the latest retained tick, or ``None``."""
        ...


class ColdAdvisoryClockPort(typing.Protocol):
    """The engine's own clock; ``sleep`` must end promptly when cancelled."""

    def monotonic(self) -> float:
        """Return the monotonic instant in seconds."""
        ...

    def utc_now_iso(self) -> str:
        """Return the current UTC instant in ISO 8601 form."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Suspend for ``seconds`` on this clock."""
        ...


class ColdAdvisorySamplerStop(enum.Enum):
    """Closed reasons :meth:`ColdAdvisorySampler.run` stopped; no member means success."""

    WINDOW_EXHAUSTED = "window_exhausted"
    WINDOW_CLOSED_BEFORE_START = "window_closed_before_start"
    SETTLED = "settled"
    CONTEXT_UNAVAILABLE = "context_unavailable"
    CLOCK_INVALID = "clock_invalid"
    INTENT_NOT_APPENDED = "intent_not_appended"
    RESOLUTION_NOT_APPENDED = "resolution_not_appended"
    NOT_INVOKED = "not_invoked"
    PROVIDER_TASK_ENDED_WITHOUT_OUTCOME = "provider_task_ended_without_outcome"
    STOPPED_AFTER_ABANDONMENT = "stopped_after_abandonment"


class ColdAdvisorySettlementClosure(enum.Enum):
    """What phase-end settlement recorded; ``NOT_RECORDED_REENTRANT`` is never stored."""

    NO_OPEN_ATTEMPT = "no_open_attempt"
    RECORDED_COMPLETED_CALL = "recorded_completed_call"
    RECORDED_UNRESOLVED_INVOKED = "recorded_unresolved_invoked"
    RECORDED_UNRESOLVED_NOT_INVOKED = "recorded_unresolved_not_invoked"
    NOT_RECORDED_CLOCK_INVALID = "not_recorded_clock_invalid"
    NOT_RECORDED_SINK_REFUSED = "not_recorded_sink_refused"
    NOT_RECORDED_COMPLETION_UNKNOWN = "not_recorded_completion_unknown"
    NOT_RECORDED_REENTRANT = "not_recorded_reentrant"


class ColdAdvisoryProviderTask(enum.Enum):
    """The provider task's state at the settlement instant; never updated afterwards."""

    NONE = "none"
    COMPLETED = "completed"
    ENDED_WITHOUT_OUTCOME = "ended_without_outcome"
    OUTSTANDING = "outstanding"


_RESULT_CONFIG = pydantic.ConfigDict(frozen=True, strict=True, extra="forbid")


class ColdAdvisorySamplerRun(pydantic.BaseModel):
    """The closed result of one sampler run: why it stopped and how many attempts closed."""

    model_config = _RESULT_CONFIG

    stop: ColdAdvisorySamplerStop
    attempts_resolved: int


class ColdAdvisorySettlement(pydantic.BaseModel):
    """The closed settlement fact handed to the later integration and composition slices.

    ``OUTSTANDING`` and ``ENDED_WITHOUT_OUTCOME`` are the only interface for OD5 and
    OD7/D201, which this module does not implement.
    """

    model_config = _RESULT_CONFIG

    closure: ColdAdvisorySettlementClosure
    provider_task: ColdAdvisoryProviderTask
    attempts_resolved: int


def _is_label(value: object) -> bool:
    """The exact 5a label grammar: an exact, bounded, encodable, non-blank ``str``."""
    if type(value) is not str or len(value) > MAX_TEXT_FIELD_BYTES or not value.strip():
        return False
    try:
        return len(value.encode("utf-8")) <= MAX_TEXT_FIELD_BYTES
    except UnicodeEncodeError:
        return False


class ColdAdvisorySpec(pydantic.BaseModel):
    """The explicit per-run advisory spec (OD1): no defaults, no range, finiteness only.

    Temperatures are Celsius.  The guidance minimum and maximum are each optional
    and are never compared with each other or with any plausibility range.
    """

    model_config = pydantic.ConfigDict(
        frozen=True, strict=True, extra="forbid", allow_inf_nan=False
    )

    profile_name: str
    target_drop_temp_c: float
    charge_guidance_min_c: float | None
    charge_guidance_max_c: float | None

    @pydantic.field_validator("profile_name", mode="before")
    @classmethod
    def _require_label(cls, value: object) -> object:
        """Apply the exact 5a label grammar."""
        if _is_label(value):
            return value
        raise ValueError("profile name must be a bounded non-blank str")

    @pydantic.field_validator(
        "target_drop_temp_c", "charge_guidance_min_c", "charge_guidance_max_c", mode="before"
    )
    @classmethod
    def _require_finite(cls, value: object) -> object:
        """Refuse anything but an exact finite ``float`` (or null where optional)."""
        if value is None or (type(value) is float and math.isfinite(value)):
            return value
        raise ValueError("temperature must be an exact finite float")


class ColdAdvisorySamplerRefusedError(RuntimeError):
    """A content-free refusal to construct a sampler or to run it twice."""

    def __init__(self) -> None:
        """Create the fixed refusal."""
        super().__init__("Cold advisory sampler refused.")


class _Refused(Exception):
    """Private construction refusal, always replaced by the public error."""


class _Owner(enum.Enum):
    """Private owner of one clock sample or consumption; never public."""

    RUN = "run"
    PROVIDER = "provider"
    SETTLEMENT = "settlement"


class _Refusal(enum.Enum):
    """Why a provider task ended without invoking the advisor."""

    SETTLED_BEFORE_DISPATCH = "settled_before_dispatch"
    PORT_ACCESS_FAILED = "port_access_failed"
    CLOCK_INVALID = "clock_invalid"
    AFTER_WINDOW = "after_window"
    DEADLINE_NOT_FINITE = "deadline_not_finite"


class _Gated(enum.Enum):
    """The private closed-gate sample result, distinct from an invalid sample."""

    CLOSED = "closed"


class _ConsumeResult(enum.Enum):
    """Private result of consuming one completed call."""

    APPENDED = "appended"
    STOPPED_GATE = "stopped_gate"
    CLOCK_INVALID = "clock_invalid"
    SINK_REFUSED = "sink_refused"


_T = typing.TypeVar("_T")
_P = typing.ParamSpec("_P")
_Instant: typing.TypeAlias = tuple[float, str]
_ABSENT: typing.Final = object()
_SPEC_NAMES = (
    "profile_name",
    "target_drop_temp_c",
    "charge_guidance_min_c",
    "charge_guidance_max_c",
)
_DESCRIPTOR_NAMES = ("provider", "model", "prompt_version")
_DECISION_NAMES = ("target_heat", "target_fan", "should_drop", "confidence", "rationale")
_USAGE_NAMES = ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens")
_EVALUATION_NAMES = (
    "rule",
    "verdict",
    "input_heat",
    "input_fan",
    "adjusted_heat",
    "adjusted_fan",
    "reason",
)
_VERDICTS: typing.Final[tuple[tuple[SafetyVerdict, ColdSafetyVerdict], ...]] = (
    (SafetyVerdict.ALLOW, ColdSafetyVerdict.ALLOW),
    (SafetyVerdict.CLAMP, ColdSafetyVerdict.CLAMP),
    (SafetyVerdict.REJECT, ColdSafetyVerdict.REJECT),
    (SafetyVerdict.RECOVERY, ColdSafetyVerdict.RECOVERY),
    (SafetyVerdict.FAULT, ColdSafetyVerdict.FAULT),
    (SafetyVerdict.EMERGENCY_STOP, ColdSafetyVerdict.EMERGENCY_STOP),
)
_CLASSIFICATION: typing.Final[tuple[tuple[type[Exception], ColdAdvisoryResolution], ...]] = (
    (AdvisorProviderError, ColdAdvisoryResolution.RETURNED_PROVIDER_ERROR),
    (AdvisorMalformedOutputError, ColdAdvisoryResolution.RETURNED_MALFORMED_OUTPUT),
    (AdvisorUnsafeOutputError, ColdAdvisoryResolution.RETURNED_UNSAFE_OUTPUT),
)


class _Gate:
    """The shared settlement gate and synchronous port-call depth; it holds no port."""

    __slots__ = ("closed", "depth")

    def __init__(self) -> None:
        """Create an open gate with no port call in progress."""
        self.closed = False
        self.depth = 0


def _counted(gate: _Gate, call: typing.Callable[[], _T]) -> _T:
    """Run one synchronous port access with the reentry depth counted."""
    gate.depth += 1
    try:
        return call()
    finally:
        gate.depth -= 1


class _Floor:
    """One monotonic floor shared by the run, the provider task and settlement."""

    __slots__ = ("_clock", "_gate", "_last")

    def __init__(self, clock: ColdAdvisoryClockPort, gate: _Gate) -> None:
        """Bind the clock and gate; no clock access happens here."""
        self._clock = clock
        self._gate = gate
        self._last = 0.0

    def sample(self, owner: _Owner) -> _Instant | _Gated | None:
        """Sample one admitted, non-regressing instant.

        Run and provider owners check the gate before the monotonic access, between
        the two accesses and after both, and get ``CLOSED`` with no further access;
        settlement skips only that gate rejection.  ``None`` means an invalid sample.
        """
        gate = self._gate
        guarded = owner is not _Owner.SETTLEMENT
        if guarded and gate.closed:
            return _Gated.CLOSED
        mono: object = None
        utc: object = None
        try:
            mono = _counted(gate, lambda: self._clock.monotonic())
            if guarded and gate.closed:
                return _Gated.CLOSED
            utc = _counted(gate, lambda: self._clock.utc_now_iso())
        except Exception:
            mono = None
        if guarded and gate.closed:
            return _Gated.CLOSED
        if not (is_admissible_monotonic(mono) and is_admissible_utc_instant(utc)):
            return None
        admitted = typing.cast(float, mono)
        if admitted < self._last:
            return None
        self._last = admitted
        return (admitted, typing.cast(str, utc))


class _Outcome(typing.NamedTuple):
    """One completed call's admitted values; no raw decision, usage or exception."""

    kind: ColdAdvisoryResolution
    resolved: _Instant | None
    heat: int | None
    fan: int | None
    should_drop: bool | None
    confidence: float | None
    rationale: str | None
    usage: ColdAdvisoryUsageReading | None


class _CallCell:
    """Private per-attempt state shared with exactly one provider task."""

    __slots__ = (
        "consumed",
        "deadline",
        "discarded_after_settlement",
        "invocation",
        "invoked",
        "outcome",
        "ready",
        "refusal",
    )

    def __init__(self) -> None:
        """Create the state of one not-yet-invoked attempt."""
        self.invoked = False
        self.invocation: _Instant | None = None
        self.deadline: float | None = None
        self.refusal: _Refusal | None = None
        self.outcome: _Outcome | None = None
        self.discarded_after_settlement = False
        self.ready = asyncio.Event()
        self.consumed = False


def _shape(
    obj: object, cls: type[pydantic.BaseModel], names: tuple[str, ...]
) -> dict[str, object] | None:
    """Admit one exact model's raw state: class, count, key types, presence, extras.

    Key types are checked before any hash or equality runs; ``None`` refuses.
    """
    try:
        if type(obj) is not cls:
            return None
        data = object.__getattribute__(obj, "__dict__")
        if type(data) is not dict:
            return None
        raw = typing.cast(dict[object, object], data)
        if len(raw) != len(names) or any(type(key) is not str for key in raw):
            return None
        values = {name: raw.get(name, _ABSENT) for name in names}
        if any(value is _ABSENT for value in values.values()):
            return None
        extra = object.__getattribute__(obj, "__pydantic_extra__")
        if not (extra is None or (type(extra) is dict and not extra)):
            return None
    except Exception:
        return None
    return values


def _is_lever(value: object) -> typing.TypeGuard[int]:
    """Whether a value is an exact ``int`` from 0 to 100."""
    return type(value) is int and 0 <= value <= 100


def _admit_decision(decision: object) -> tuple[int, int, bool, float, str] | None:
    """Admit a returned decision's exact primitive values, or refuse it as malformed."""
    values = _shape(decision, RoastDecision, _DECISION_NAMES)
    if values is None:
        return None
    heat, fan, drop, confidence, rationale = (values[name] for name in _DECISION_NAMES)
    if not (_is_lever(heat) and _is_lever(fan) and type(drop) is bool):
        return None
    if (
        type(confidence) is not float
        or not (0.0 <= confidence <= 1.0)
        or type(rationale) is not str
    ):
        return None
    return (heat, fan, drop, confidence, rationale)


def _admit_usage(pre: object, post: object) -> ColdAdvisoryUsageReading | None:
    """Admit a fresh post-call usage reading: exact, not the pre-call object, exact shape."""
    values = None if post is pre else _shape(post, AdvisorUsage, _USAGE_NAMES)
    if values is None:
        return None
    try:
        return ColdAdvisoryUsageReading.model_validate(values, strict=True)
    except Exception:
        return None


def _admit_evaluation(raw: object, heat: int, fan: int) -> ColdSafetyEvaluation | None:
    """Admit one typed evaluation of exactly the request, mapping the verdict by identity."""
    values = _shape(raw, SafetyEvaluation, _EVALUATION_NAMES)
    if values is None:
        return None
    verdict = next((cold for live, cold in _VERDICTS if values["verdict"] is live), None)
    input_heat, input_fan = values["input_heat"], values["input_fan"]
    if verdict is None or type(input_heat) is not int or type(input_fan) is not int:
        return None
    if input_heat != heat or input_fan != fan:
        return None
    try:
        return ColdSafetyEvaluation.model_validate({**values, "verdict": verdict}, strict=True)
    except Exception:
        return None


def _classify(kind: type[BaseException]) -> ColdAdvisoryResolution:
    """Classify a raised exception's exact type; subclasses are unclassified."""
    for cls, resolution in _CLASSIFICATION:
        if kind is cls:
            return resolution
    return ColdAdvisoryResolution.RAISED_UNCLASSIFIED


def _admit_outcome(
    kind: ColdAdvisoryResolution,
    decision: object,
    pre: object,
    post: object,
    resolved: _Instant | None,
) -> _Outcome:
    """Build the immutable outcome from admitted values only."""
    admitted = None
    if kind is ColdAdvisoryResolution.RETURNED_DECISION:
        admitted = _admit_decision(decision)
        if admitted is None:
            kind = ColdAdvisoryResolution.RETURNED_MALFORMED_OUTPUT
    usage = None
    if (
        kind is ColdAdvisoryResolution.RETURNED_DECISION
        or kind is ColdAdvisoryResolution.RETURNED_UNSAFE_OUTPUT
    ):
        usage = _admit_usage(pre, post)
    if admitted is None:
        return _Outcome(kind, resolved, None, None, None, None, None, usage)
    return _Outcome(kind, resolved, *admitted, usage)


async def _provider_call(
    advisor: ColdAdvisoryAdvisorPort,
    context: AdvisorContext,
    floor: _Floor,
    gate: _Gate,
    close: float,
    bound: float,
    cell: _CallCell,
) -> None:
    """Invoke the advisor at most once for one open intent; it holds no sink or sampler.

    The gate is checked before every advisor or clock access and immediately after
    the call returns or raises; after settlement a late completion is discarded with
    no further access.  ``CancelledError`` and housekeeping failures end the task
    without an outcome.  This proves invocation of the port, not HTTP dispatch.
    """
    try:
        if gate.closed:
            cell.refusal = _Refusal.SETTLED_BEFORE_DISPATCH
            return
        try:
            pre: object = _counted(gate, lambda: advisor.last_usage)
            if gate.closed:
                cell.refusal = _Refusal.SETTLED_BEFORE_DISPATCH
                return
            method = _counted(gate, lambda: advisor.get_recommendation)
        except Exception:
            cell.refusal = _Refusal.PORT_ACCESS_FAILED
            return
        invocation = floor.sample(_Owner.PROVIDER)
        if invocation is _Gated.CLOSED:
            cell.refusal = _Refusal.SETTLED_BEFORE_DISPATCH
            return
        if invocation is None:
            cell.refusal = _Refusal.CLOCK_INVALID
            return
        if invocation[0] > close:
            cell.refusal = _Refusal.AFTER_WINDOW
            return
        deadline = invocation[0] + bound
        if not math.isfinite(deadline):
            cell.refusal = _Refusal.DEADLINE_NOT_FINITE
            return
        kind = ColdAdvisoryResolution.RETURNED_DECISION
        decision: object = None
        cell.ready.set()
        if gate.closed:
            cell.refusal = _Refusal.SETTLED_BEFORE_DISPATCH
            return
        cell.invocation = invocation
        cell.deadline = deadline
        cell.invoked = True
        try:
            decision = await method(context)
        except Exception as error:
            kind = _classify(type(error))
            del error
        if gate.closed:
            cell.discarded_after_settlement = True
            return
        resolved = floor.sample(_Owner.PROVIDER)
        if resolved is _Gated.CLOSED:
            cell.discarded_after_settlement = True
            return
        try:
            post: object = _counted(gate, lambda: advisor.last_usage)
        except Exception:
            post = _ABSENT
        if gate.closed:
            cell.discarded_after_settlement = True
            return
        outcome = _admit_outcome(kind, decision, pre, post, resolved)
        del decision, pre, post, method
        if gate.closed:
            cell.discarded_after_settlement = True
            return
        cell.outcome = outcome
    finally:
        cell.ready.set()


def _may_start_attempt(task: "asyncio.Task[None] | None") -> bool:
    """Whether no provider task is outstanding."""
    return task is None or task.done()


def _discard_task_exception(task: "asyncio.Task[None]") -> None:
    """Retrieve and drop a finished provider task's exception; never writes or formats."""
    if not task.cancelled():
        task.exception()


def _append(gate: _Gate, sink: ColdAdvisorySinkPort, record: ColdAdvisoryAttemptRecord) -> None:
    """Make one counted durable sink append."""
    _counted(gate, lambda: sink.append_advisory_attempt(record))


def _sleeper(
    clock: ColdAdvisoryClockPort, seconds: float
) -> typing.Callable[[], typing.Coroutine[typing.Any, typing.Any, object]]:
    """Bind one clock sleep for an owned waiter; the coroutine is created by the waiter."""
    return lambda: clock.sleep(seconds)


async def _retire(waiter: "asyncio.Task[typing.Any]") -> None:
    """Cancel and explicitly await one owned waiter; re-raise the caller's cancellation.

    It never touches the provider task.  Retirement depends on the clock's
    cancellation contract and has no time bound.
    """
    if not waiter.done():
        waiter.cancel()
    pending: asyncio.CancelledError | None = None
    while not waiter.done():
        try:
            await asyncio.wait({waiter})
        except asyncio.CancelledError as cancelled:
            if pending is None:
                pending = cancelled
    if not waiter.cancelled():
        waiter.exception()
    if pending is not None:
        raise pending


async def _await_waiter(
    start: typing.Callable[[], typing.Coroutine[typing.Any, typing.Any, object]],
    task: "asyncio.Task[None] | None",
) -> bool:
    """Await one owned waiter, or the provider task's completion; ``False`` if it failed.

    The waiter is always retired by an explicit await; the provider task is only
    watched, never awaited, joined or cancelled.
    """
    try:
        waiter = asyncio.create_task(start())
    except Exception:
        return False
    try:
        watched: set[asyncio.Task[typing.Any]] = {waiter} if task is None else {waiter, task}
        done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
        return waiter not in done or not (waiter.cancelled() or waiter.exception() is not None)
    finally:
        await _retire(waiter)


class ColdAdvisorySampler:
    """One phase's standalone observation-only advisory sampler (D199, D200, D201).

    It has no lifecycle, finalisation, stop, kill, join or exit capability.  The
    caller must, after obtaining the stored settlement, cancel and await
    :meth:`run` within its own composition; if an inner call returned the
    provisional ``NOT_RECORDED_REENTRANT`` fact, it must settle again from outside
    any port.  An ``OUTSTANDING`` or ``ENDED_WITHOUT_OUTCOME`` provider task hands
    over to OD5 and OD7/D201, implemented only by the later integration and
    composition slices.  Terminating the local Agent proves neither that the remote
    request stopped, that the roaster is physically safe, nor that sealing succeeded.
    """

    def __init__(
        self,
        *,
        header: ColdRunHeader,
        established_session_id: str,
        scheduled_end_monotonic: float,
        spec: ColdAdvisorySpec,
        configured_call_bound_seconds: float,
        configured_dwell_seconds: float,
        advisor: ColdAdvisoryAdvisorPort,
        evaluator: ColdAdvisoryEvaluatorPort,
        sink: ColdAdvisorySinkPort,
        ticks: ColdAdvisoryTickPort,
        clock: ColdAdvisoryClockPort,
    ) -> None:
        """Admit every input, then make the one counted descriptor call.

        Args:
            header: The exact bound phase header.
            established_session_id: The phase's established MCP session identity.
            scheduled_end_monotonic: The retained scheduled phase end.
            spec: The explicit per-run advisory spec.
            configured_call_bound_seconds: Configured per-call bound (> 0), retained.
            configured_dwell_seconds: Configured post-completion dwell (>= 5.0), retained.
            advisor: The production advisor port.
            evaluator: The typed safety-evaluation port.
            sink: The durable advisory-attempt sink.
            ticks: The latest retained tick port.
            clock: The engine's own clock instance.

        Raises:
            ColdAdvisorySamplerRefusedError: With a fixed message and no cause.
        """
        failure: ColdAdvisorySamplerRefusedError | None = None
        try:
            if type(header) is not ColdRunHeader:
                raise _Refused
            self._header = typing.cast(ColdRunHeader, validate_record(header))
            session = established_session_id
            if (
                type(session) is not str
                or len(session) > MAX_TEXT_FIELD_BYTES
                or not is_admissible_session_id(session)
            ):
                raise _Refused
            self._session = session
            self._window = advisory_window_bounds(scheduled_end_monotonic)
            values = _shape(spec, ColdAdvisorySpec, _SPEC_NAMES)
            if values is None:
                raise _Refused
            self._spec = ColdAdvisorySpec.model_validate(values, strict=True)
            bound, dwell = configured_call_bound_seconds, configured_dwell_seconds
            if not (type(bound) is float and math.isfinite(bound) and bound > 0.0):
                raise _Refused
            if not (type(dwell) is float and math.isfinite(dwell)):
                raise _Refused
            if dwell < MIN_POST_COMPLETION_DWELL_SECONDS:
                raise _Refused
            self._bound, self._dwell = bound, dwell
            self._gate = _Gate()
            self._floor = _Floor(clock, self._gate)
            self._settlement: ColdAdvisorySettlement | None = None
            self._open: tuple[int, _CallCell] | None = None
            self._task: asyncio.Task[None] | None = None
            self._task_cell: _CallCell | None = None
            self._ran = False
            self._settling = False
            self._commit_latch = False
            self._attempts_resolved = 0
            self._advisor, self._evaluator, self._sink = advisor, evaluator, sink
            self._ticks, self._clock = ticks, clock
            descriptor = _counted(self._gate, lambda: advisor.descriptor_for(RoastPhase.PREHEATING))
            labels = _shape(descriptor, AdvisorDescriptor, _DESCRIPTOR_NAMES)
            if labels is None or not all(_is_label(label) for label in labels.values()):
                raise _Refused
            self._descriptor = typing.cast(
                tuple[str, str, str], tuple(labels[name] for name in _DESCRIPTOR_NAMES)
            )
        except Exception:
            failure = ColdAdvisorySamplerRefusedError()
        if failure is not None:
            raise failure

    def _latched(self, region: typing.Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
        """Run one synchronous commit region under the commit latch, reset on any exit.

        A settlement made while the latch is set (including from a carrier released
        inside the region) is provisional and unstored.
        """
        self._commit_latch = True
        try:
            return region(*args, **kwargs)
        finally:
            self._commit_latch = False

    def _context(self, now: float) -> tuple[ColdTickRecord, AdvisorContext] | _Gated | None:
        """Project the latest admitted retained tick into a fixed before-charge context."""
        try:
            raw: object = _counted(self._gate, lambda: self._ticks.latest_retained_tick())
        except Exception:
            raw = None
        if self._gate.closed:
            return _Gated.CLOSED
        try:
            if type(raw) is not ColdTickRecord:
                return None
            tick = typing.cast(ColdTickRecord, validate_record(raw))
            header, device, audio = self._header, tick.device, tick.audio
            if not (
                tick.run_id == header.run_id
                and tick.phase is header.phase
                and tick.identity_sha256 == header.identity_sha256
                and tick.session.session_id == self._session
                and tick.monotonic_seconds <= now
            ):
                return None
            if device is None or device.bean_temp_c is None or device.env_temp_c is None:
                return None
            if audio.status == "detected" or audio.detected_at_utc is not None:
                return None
            if audio.detected_monotonic_seconds is not None:
                return None
            spec = self._spec
            context = AdvisorContext(
                phase=RoastPhase.PREHEATING,
                roast_elapsed_seconds=0.0,
                development_elapsed_seconds=None,
                current_bean_temp_c=device.bean_temp_c,
                current_env_temp_c=device.env_temp_c,
                bean_ror_c_per_min=None,
                env_ror_c_per_min=None,
                target_drop_temp_c=spec.target_drop_temp_c,
                profile_name=spec.profile_name,
                charge_guidance_min_c=spec.charge_guidance_min_c,
                charge_guidance_max_c=spec.charge_guidance_max_c,
                first_crack_detected=False,
                seconds_since_charge=None,
            )
        except Exception:
            return None
        return tick, context

    async def run(self) -> ColdAdvisorySamplerRun:
        """Sample the advisory window once; a second call is refused.

        Returns:
            Why the run stopped and how many attempts it closed.  Cancelling the run
            re-raises after every owned waiter is retired; it never cancels or joins
            the provider task.

        Raises:
            ColdAdvisorySamplerRefusedError: If the sampler already ran.
        """
        if self._ran:
            raise ColdAdvisorySamplerRefusedError()
        self._ran = True
        stop = await self._attempts()
        return ColdAdvisorySamplerRun(stop=stop, attempts_resolved=self._attempts_resolved)

    async def _attempts(self) -> ColdAdvisorySamplerStop:
        """The attempt loop; every closed fact is one stop member."""
        stops, gate, floor, clock = ColdAdvisorySamplerStop, self._gate, self._floor, self._clock
        opened, close = self._window
        due, index = opened, 0
        while True:
            while True:
                now = floor.sample(_Owner.RUN)
                if now is _Gated.CLOSED:
                    return stops.SETTLED
                if now is None:
                    return stops.CLOCK_INVALID
                if now[0] > close:
                    return stops.WINDOW_EXHAUSTED if index else stops.WINDOW_CLOSED_BEFORE_START
                if now[0] >= due:
                    break
                woke = await _await_waiter(_sleeper(clock, due - now[0]), None)
                if gate.closed:
                    return stops.SETTLED
                if not woke:
                    return stops.CLOCK_INVALID
            if not _may_start_attempt(self._task):
                return stops.STOPPED_AFTER_ABANDONMENT
            opened = self._latched(self._open_attempt, index, now)
            if opened is not None:
                return opened
            if gate.closed:
                return stops.SETTLED
            task = typing.cast("asyncio.Task[None]", self._task)
            cell = typing.cast(_CallCell, self._task_cell)
            while cell.refusal is None and not task.done() and not cell.invoked:
                woke = await _await_waiter(cell.ready.wait, task)
                if gate.closed:
                    return stops.SETTLED
                if not woke:
                    return stops.CLOCK_INVALID
            while cell.refusal is None and not task.done():
                sample = floor.sample(_Owner.RUN)
                if sample is _Gated.CLOSED:
                    return stops.SETTLED
                if sample is None:
                    return stops.CLOCK_INVALID
                deadline = typing.cast(float, cell.deadline)
                if sample[0] >= deadline:
                    return self._latched(self._abandon, task, cell, index, sample)
                woke = await _await_waiter(_sleeper(clock, deadline - sample[0]), task)
                if gate.closed:
                    return stops.SETTLED
                if not woke:
                    return stops.CLOCK_INVALID
            if cell.refusal is not None:
                # Unreachable while the gate checks above hold: this refusal is only set
                # with the gate closed, which a lazy start meets at the post-wake check
                # and an eager start at the post-region check.  Kept as the explicit map.
                if cell.refusal is _Refusal.SETTLED_BEFORE_DISPATCH:  # pragma: no cover
                    return stops.SETTLED
                return stops.NOT_INVOKED
            outcome = cell.outcome
            if outcome is None:
                return stops.PROVIDER_TASK_ENDED_WITHOUT_OUTCOME
            if outcome.resolved is None:
                return stops.CLOCK_INVALID
            consumed = self._latched(self._consume, cell, outcome, _Owner.RUN)
            if consumed is _ConsumeResult.STOPPED_GATE:
                return stops.SETTLED
            if consumed is _ConsumeResult.CLOCK_INVALID:
                return stops.CLOCK_INVALID
            if consumed is _ConsumeResult.SINK_REFUSED:
                return stops.RESOLUTION_NOT_APPENDED
            due, index = outcome.resolved[0] + self._dwell, index + 1
            if not due <= close:
                return stops.WINDOW_EXHAUSTED

    def _open_attempt(self, index: int, now: _Instant) -> ColdAdvisorySamplerStop | None:
        """Project, build, append and bookkeep one intent, then start its provider task.

        A synchronous commit region run under the latch: ``None`` means the task was
        created and published; once the gate is observed closed no task is created.
        """
        stops, gate = ColdAdvisorySamplerStop, self._gate
        if gate.closed:
            return stops.SETTLED
        built = self._context(now[0])
        if built is _Gated.CLOSED:
            return stops.SETTLED
        if built is None:
            return stops.CONTEXT_UNAVAILABLE
        tick, context = built
        spec, descriptor = self._spec, self._descriptor
        cell = _CallCell()
        try:
            intent = build_advisory_intent_record(
                header=self._header,
                attempt_index=index,
                recorded_at_utc=now[1],
                monotonic_seconds=now[0],
                context_tick=tick.tick,
                context_tick_monotonic=tick.monotonic_seconds,
                context=context.model_dump(mode="json"),
                profile_name=spec.profile_name,
                target_drop_temp_c=spec.target_drop_temp_c,
                charge_guidance_min_c=spec.charge_guidance_min_c,
                charge_guidance_max_c=spec.charge_guidance_max_c,
                provider=descriptor[0],
                model=descriptor[1],
                prompt_version=descriptor[2],
                configured_call_bound_seconds=self._bound,
                configured_dwell_seconds=self._dwell,
            )
            if gate.closed:
                return stops.SETTLED
            _append(gate, self._sink, intent)
        except Exception:
            return stops.INTENT_NOT_APPENDED
        self._open = (index, cell)
        if gate.closed:
            return stops.SETTLED
        task = asyncio.create_task(
            _provider_call(
                self._advisor, context, self._floor, gate, self._window[1], self._bound, cell
            )
        )
        task.add_done_callback(_discard_task_exception)
        self._task, self._task_cell = task, cell
        return None

    def _abandon(
        self, task: "asyncio.Task[None]", cell: _CallCell, index: int, at: _Instant
    ) -> ColdAdvisorySamplerStop:
        """Request one cancellation, record the abandonment, keep ownership and stop.

        A commit region run under the latch: an already-settled entry neither cancels
        nor appends.
        """
        stops, gate = ColdAdvisorySamplerStop, self._gate
        if gate.closed:
            return stops.SETTLED
        task.cancel()
        invocation = typing.cast(_Instant, cell.invocation)
        try:
            record = build_advisory_resolution_record(
                header=self._header,
                attempt_index=index,
                recorded_at_utc=at[1],
                monotonic_seconds=at[0],
                resolution=ColdAdvisoryResolution.ABANDONED_AFTER_BOUND,
                invocation_utc=invocation[1],
                invocation_monotonic=invocation[0],
                resolved_utc=at[1],
                resolved_monotonic=at[0],
            )
            if gate.closed:
                return stops.SETTLED
            _append(gate, self._sink, record)
        except Exception:
            return stops.RESOLUTION_NOT_APPENDED
        cell.consumed = True
        self._attempts_resolved += 1
        if gate.closed:
            return stops.SETTLED
        return stops.STOPPED_AFTER_ABANDONMENT

    def _consume(self, cell: _CallCell, outcome: _Outcome, owner: _Owner) -> _ConsumeResult:
        """Evaluate, time and append one completed call's resolution from its snapshot.

        A commit region run under the latch.  Only settlement-owned consumption may
        call ports after the gate closed.
        """
        gate = self._gate
        run_owned = owner is _Owner.RUN
        if run_owned and gate.closed:
            return _ConsumeResult.STOPPED_GATE
        evaluation = None
        if outcome.kind is ColdAdvisoryResolution.RETURNED_DECISION:
            heat, fan = typing.cast(int, outcome.heat), typing.cast(int, outcome.fan)
            raw: object = None
            try:
                raw = _counted(
                    gate,
                    lambda: self._evaluator.evaluate_command(
                        requested_heat=heat,
                        requested_fan=fan,
                        seconds_since_last_command=None,
                        bounds=None,
                    ),
                )
            except Exception:
                raw = None
            if run_owned and gate.closed:
                return _ConsumeResult.STOPPED_GATE
            evaluation = _admit_evaluation(raw, heat, fan)
            del raw
        recorded = self._floor.sample(owner)
        if recorded is _Gated.CLOSED:
            return _ConsumeResult.STOPPED_GATE
        if recorded is None:
            return _ConsumeResult.CLOCK_INVALID
        invocation = typing.cast(_Instant, cell.invocation)
        resolved = typing.cast(_Instant, outcome.resolved)
        try:
            record = build_advisory_resolution_record(
                header=self._header,
                attempt_index=typing.cast(tuple[int, _CallCell], self._open)[0],
                recorded_at_utc=recorded[1],
                monotonic_seconds=recorded[0],
                resolution=outcome.kind,
                invocation_utc=invocation[1],
                invocation_monotonic=invocation[0],
                resolved_utc=resolved[1],
                resolved_monotonic=resolved[0],
                requested_heat=outcome.heat,
                requested_fan=outcome.fan,
                should_drop=outcome.should_drop,
                confidence=outcome.confidence,
                rationale=outcome.rationale,
                evaluation=evaluation,
                usage=outcome.usage,
            )
            if run_owned and gate.closed:
                return _ConsumeResult.STOPPED_GATE
            _append(gate, self._sink, record)
        except Exception:
            return _ConsumeResult.SINK_REFUSED
        cell.consumed = True
        self._attempts_resolved += 1
        if run_owned and gate.closed:
            return _ConsumeResult.STOPPED_GATE
        return _ConsumeResult.APPENDED

    def _task_fact(self) -> ColdAdvisoryProviderTask:
        """The provider task's state now, with no port call."""
        task, cell = self._task, self._task_cell
        if task is None or cell is None:
            return ColdAdvisoryProviderTask.NONE
        if not task.done():
            return ColdAdvisoryProviderTask.OUTSTANDING
        if cell.refusal is not None:
            return ColdAdvisoryProviderTask.NONE
        if cell.outcome is not None:
            return ColdAdvisoryProviderTask.COMPLETED
        return ColdAdvisoryProviderTask.ENDED_WITHOUT_OUTCOME

    def settle_at_phase_end(self) -> ColdAdvisorySettlement:
        """Close the gate and write at most one terminal resolution; synchronous, idempotent.

        A call made during any sampler-initiated synchronous port call (including
        settlement's own), or at any other point while a settlement or a synchronous
        commit region is in progress (for example from a finaliser of port-returned
        data released there), returns a provisional ``NOT_RECORDED_REENTRANT`` fact
        with no write, no port call and no storage.  If a ``BaseException`` escapes
        mid-settlement it propagates, nothing is stored and the latches are released.

        Returns:
            The stored settlement, or a fresh provisional fact on reentry.
        """
        stored = self._settlement
        if stored is not None:
            return stored
        gate = self._gate
        gate.closed = True
        if gate.depth > 0 or self._settling or self._commit_latch:
            return ColdAdvisorySettlement(
                closure=ColdAdvisorySettlementClosure.NOT_RECORDED_REENTRANT,
                provider_task=self._task_fact(),
                attempts_resolved=self._attempts_resolved,
            )
        self._settling = True
        try:
            closure = self._terminal()
            settlement = ColdAdvisorySettlement(
                closure=closure,
                provider_task=self._task_fact(),
                attempts_resolved=self._attempts_resolved,
            )
        finally:
            self._settling = False
        self._settlement = settlement
        return settlement

    def _terminal(self) -> ColdAdvisorySettlementClosure:
        """Record the open attempt's one terminal fact, never inventing an instant."""
        closures = ColdAdvisorySettlementClosure
        opened = self._open
        if opened is None or opened[1].consumed:
            return closures.NO_OPEN_ATTEMPT
        index, cell = opened
        task = self._task if self._task_cell is cell else None
        done = task is not None and task.done()
        outcome = cell.outcome
        if done and outcome is not None and outcome.resolved is not None:
            consumed = self._latched(self._consume, cell, outcome, _Owner.SETTLEMENT)
            if consumed is _ConsumeResult.APPENDED:
                return closures.RECORDED_COMPLETED_CALL
            if consumed is _ConsumeResult.SINK_REFUSED:
                return closures.NOT_RECORDED_SINK_REFUSED
            return closures.NOT_RECORDED_CLOCK_INVALID
        if cell.invoked and done:
            return closures.NOT_RECORDED_COMPLETION_UNKNOWN
        at = self._floor.sample(_Owner.SETTLEMENT)
        if not isinstance(at, tuple):
            return closures.NOT_RECORDED_CLOCK_INVALID
        invocation = cell.invocation if cell.invoked else None
        try:
            record = build_advisory_resolution_record(
                header=self._header,
                attempt_index=index,
                recorded_at_utc=at[1],
                monotonic_seconds=at[0],
                resolution=ColdAdvisoryResolution.UNRESOLVED_AT_PHASE_END,
                invocation_utc=None if invocation is None else invocation[1],
                invocation_monotonic=None if invocation is None else invocation[0],
                resolved_utc=at[1],
                resolved_monotonic=at[0],
            )
            _append(self._gate, self._sink, record)
        except Exception:
            return closures.NOT_RECORDED_SINK_REFUSED
        cell.consumed = True
        self._attempts_resolved += 1
        if invocation is None:
            return closures.RECORDED_UNRESOLVED_NOT_INVOKED
        return closures.RECORDED_UNRESOLVED_INVOKED
