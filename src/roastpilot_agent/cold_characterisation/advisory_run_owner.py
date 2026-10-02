"""Standalone run and phase owner for the cold advisory sampler (#954 5c-ii-a).

The owner holds one advisor (from one factory call) and at most one sampler per
cold phase.  It starts each phase's :meth:`ColdAdvisorySampler.run` in an owned task,
settles it synchronously and reports neutral, closed facts.  It implements no OD5,
OD7, OD8 or OD9 policy, termination, qualification or OD10 record, and nothing
wires it into a runtime.

Settlement order is fixed: settle the sampler, snapshot the run fact, request
provider cancellation, then request run cancellation when a handle exists.  Run
cancellation is never awaited or joined; a request proves neither delivery nor that
work stopped.  ``RECORDING_ON`` starts only after a stored ``RECORDING_OFF``
settlement whose run fact is not ``UNCONFIRMED`` and whose provider observation
exists and is not ``OUTSTANDING``.

Each phase has at most one start attempt that reaches foreign code (the factory,
its result's admission getters, sampler construction or task creation): the
interrupted result of each stage is stored before the stage runs, so a
``BaseException`` leaves it as the final start and the phase is never retried.
Under ``asyncio.eager_task_factory`` the run (and nested provider creation) may
execute before ``create_task`` returns or raises, so any failure of
``create_task`` is an unknown creation outcome, reported as
``TASK_CREATION_UNCONFIRMED`` with run state ``UNCONFIRMED``: the owner then has
no run handle to cancel.  A successfully published provider task stays reachable
through the sampler's cancellation request; task creation that exits before
publication is not proven observable or cancellable.  ``UNCONFIRMED`` is never
healthy and never admits ``RECORDING_ON``.  ``KeyboardInterrupt``, ``SystemExit``
and nonconforming task factories are not isolated.

Advisor admission is staged and non-protocol: the factory result must be non-null
with callable ``descriptor_for`` and ``get_recommendation``.  Freshness and
exclusive use of that advisor are a caller contract; the single-flight adapter
cannot police a raw advisor the caller retains.  No raw exception, task, future,
advisor or text is exposed.
"""

import asyncio
import enum
import typing
from collections.abc import Callable, Coroutine

import pydantic

from roastpilot_agent.advisor import AdvisorContext, AdvisorDescriptor, AdvisorUsage, RoastDecision
from roastpilot_agent.cold_characterisation.advisory_sampler import (
    ColdAdvisoryAdvisorPort,
    ColdAdvisoryCancellationRequest,
    ColdAdvisoryClockPort,
    ColdAdvisoryEvaluatorPort,
    ColdAdvisoryProviderObservation,
    ColdAdvisoryProviderTaskState,
    ColdAdvisorySampler,
    ColdAdvisorySamplerRefusedError,
    ColdAdvisorySamplerRun,
    ColdAdvisorySamplerStop,
    ColdAdvisorySettlement,
    ColdAdvisorySettlementClosure,
    ColdAdvisorySinkPort,
    ColdAdvisorySpec,
    ColdAdvisoryTickPort,
)
from roastpilot_agent.cold_characterisation.evidence_schema import (
    ColdPhaseKind,
    ColdRunHeader,
    validate_record,
)
from roastpilot_agent.models import RoastPhase

__all__ = (
    "ColdAdvisoryOwnerRefusal",
    "ColdAdvisoryPhaseObservation",
    "ColdAdvisoryPhaseRun",
    "ColdAdvisoryPhaseSettlement",
    "ColdAdvisoryPhaseStart",
    "ColdAdvisoryRunOwner",
    "ColdAdvisoryRunTaskState",
)


class ColdAdvisoryPhaseStart(enum.Enum):
    """A phase start result; every member but ``REFUSED_PREVIOUS_PHASE_NOT_SETTLED`` is stored."""

    STARTED = "started"
    REFUSED_PREVIOUS_PHASE_NOT_SETTLED = "refused_previous_phase_not_settled"
    REFUSED_PREVIOUS_RUN_UNCONFIRMED = "refused_previous_run_unconfirmed"
    REFUSED_PREVIOUS_PROVIDER_NOT_OBSERVABLE = "refused_previous_provider_not_observable"
    REFUSED_PREVIOUS_PROVIDER_OUTSTANDING = "refused_previous_provider_outstanding"
    REFUSED_HEADER_NOT_ADMITTED = "refused_header_not_admitted"
    REFUSED_NO_RUNNING_LOOP = "refused_no_running_loop"
    REFUSED_FACTORY_RAISED = "refused_factory_raised"
    REFUSED_FACTORY_INTERRUPTED = "refused_factory_interrupted"
    REFUSED_FACTORY_RESULT_NOT_ADMITTED = "refused_factory_result_not_admitted"
    REFUSED_SAMPLER_CONSTRUCTION = "refused_sampler_construction"
    REFUSED_SAMPLER_CONSTRUCTION_INTERRUPTED = "refused_sampler_construction_interrupted"
    TASK_CREATION_UNCONFIRMED = "task_creation_unconfirmed"


class ColdAdvisoryOwnerRefusal(enum.Enum):
    """A call the owner refused without storing anything."""

    REENTRANT = "reentrant"
    PHASE_NOT_ADMITTED = "phase_not_admitted"
    NOT_STARTED = "not_started"
    SAMPLER_PROVISIONAL = "sampler_provisional"
    CANCELLATION_PROVISIONAL = "cancellation_provisional"


class ColdAdvisoryRunTaskState(enum.Enum):
    """The owned run task's state; ``PENDING`` and ``UNCONFIRMED`` are never healthy.

    ``NOT_STARTED`` means no run coroutine was ever created; ``UNCONFIRMED`` means
    one was handed to ``create_task``, which returned no handle.
    """

    NOT_STARTED = "not_started"
    PENDING = "pending"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    RAISED = "raised"
    UNCONFIRMED = "unconfirmed"


_CONFIG = pydantic.ConfigDict(frozen=True, strict=True, extra="forbid")


class ColdAdvisoryPhaseRun(pydantic.BaseModel):
    """One run fact; ``stop`` is present exactly when the run finished."""

    model_config = _CONFIG

    state: ColdAdvisoryRunTaskState
    stop: ColdAdvisorySamplerStop | None

    @pydantic.model_validator(mode="after")
    def _stop_iff_finished(self) -> typing.Self:
        """Refuse a stop without a finished run, or a finished run without a stop."""
        if (self.stop is None) is (self.state is ColdAdvisoryRunTaskState.FINISHED):
            raise ValueError("stop must be present exactly when the run finished")
        return self


class ColdAdvisoryPhaseSettlement(pydantic.BaseModel):
    """The stored, neutral settlement facts of one phase; never a health verdict."""

    model_config = _CONFIG

    sampler: ColdAdvisorySettlement
    run_at_settlement: ColdAdvisoryPhaseRun
    provider_cancellation: ColdAdvisoryCancellationRequest
    run_cancel_requested: bool


class ColdAdvisoryPhaseObservation(pydantic.BaseModel):
    """The stored start and settlement with the live run and provider facts."""

    model_config = _CONFIG

    start: ColdAdvisoryPhaseStart | None
    run: ColdAdvisoryPhaseRun
    provider: ColdAdvisoryProviderObservation | None
    settlement: ColdAdvisoryPhaseSettlement | None


class _ConcurrentAdvisoryCall(Exception):
    """Private refusal of an overlapping recommendation call."""


class _ExclusiveAdvisor:
    """Single-flight adapter over the factory's advisor (defence in depth).

    ``last_usage`` and ``descriptor_for`` pass straight through.  The raw
    ``get_recommendation`` is looked up at access time, so the sampler's counted
    port access sees a getter failure, a non-callable value or a reentry exactly
    as on the raw advisor; a callable is returned wrapped so an overlapping call is
    refused.  It cannot police use of a raw advisor that the caller retains.
    """

    __slots__ = ("_advisor", "_busy")

    def __init__(self, advisor: ColdAdvisoryAdvisorPort) -> None:
        """Wrap one admitted advisor."""
        self._advisor = advisor
        self._busy = False

    @property
    def last_usage(self) -> AdvisorUsage | None:
        """The wrapped advisor's identical usage object."""
        return self._advisor.last_usage

    def descriptor_for(self, phase: RoastPhase) -> AdvisorDescriptor:
        """The wrapped advisor's descriptor for ``phase``."""
        return self._advisor.descriptor_for(phase)

    @property
    def get_recommendation(
        self,
    ) -> Callable[[AdvisorContext], Coroutine[typing.Any, typing.Any, RoastDecision]]:
        """The raw method looked up now; a callable is wrapped as one single-flight call.

        A getter exception propagates from this lookup, before any invocation.  A
        nonconforming non-callable value is returned unchanged so the sampler's own
        callable check refuses it; an overlapping wrapped call raises and leaves the
        first intact.
        """
        method = self._advisor.get_recommendation
        if not callable(method):
            return method

        async def single_flight(context: AdvisorContext) -> RoastDecision:
            if self._busy:
                raise _ConcurrentAdvisoryCall
            self._busy = True
            try:
                return await method(context)
            finally:
                self._busy = False

        return single_flight


_ADVISOR_METHODS = ("descriptor_for", "get_recommendation")


def _admitted_advisor(raw: object) -> bool:
    """Staged, non-protocol admission: non-null with both methods callable."""
    if raw is None:
        return False
    try:
        return all(callable(getattr(raw, name)) for name in _ADVISOR_METHODS)
    except Exception:
        return False


def _admitted_phase(phase: object) -> bool:
    """Whether ``phase`` is identical to one of the two cold phases."""
    return phase is ColdPhaseKind.RECORDING_OFF or phase is ColdPhaseKind.RECORDING_ON


def _retrieve_run_exception(task: "asyncio.Task[ColdAdvisorySamplerRun]") -> None:
    """Retrieve and drop a finished run task's exception; never formats it."""
    if not task.cancelled():
        task.exception()


class ColdAdvisoryRunOwner:
    """Owns one cold run's advisory phases; no lifecycle, join, termination or policy.

    Every method is synchronous and none awaits.  A call made while another owner
    method is in progress (for example from a port) is refused as ``REENTRANT``.
    """

    def __init__(
        self,
        *,
        advisor_factory: Callable[[], ColdAdvisoryAdvisorPort],
        spec: ColdAdvisorySpec,
        configured_call_bound_seconds: float,
        configured_dwell_seconds: float,
        clock: ColdAdvisoryClockPort,
    ) -> None:
        """Store the inputs; no port or factory is called here.

        Args:
            advisor_factory: Called at most once per owner for a fresh advisor.
            spec: The explicit per-run advisory spec.
            configured_call_bound_seconds: The sampler's per-call bound.
            configured_dwell_seconds: The sampler's post-completion dwell.
            clock: The engine's own clock instance.
        """
        self._advisor_factory = advisor_factory
        self._spec = spec
        self._bound = configured_call_bound_seconds
        self._dwell = configured_dwell_seconds
        self._clock = clock
        self._adapter: _ExclusiveAdvisor | None = None
        self._busy = False
        self._off_header: ColdRunHeader | None = None
        self._start: dict[ColdPhaseKind, ColdAdvisoryPhaseStart] = {}
        self._sampler: dict[ColdPhaseKind, ColdAdvisorySampler] = {}
        self._task: dict[ColdPhaseKind, asyncio.Task[ColdAdvisorySamplerRun]] = {}
        self._settlement: dict[ColdPhaseKind, ColdAdvisoryPhaseSettlement] = {}

    def start_phase(
        self,
        phase: ColdPhaseKind,
        *,
        header: ColdRunHeader,
        established_session_id: str,
        scheduled_end_monotonic: float,
        evaluator: ColdAdvisoryEvaluatorPort,
        sink: ColdAdvisorySinkPort,
        ticks: ColdAdvisoryTickPort,
    ) -> "ColdAdvisoryPhaseStart | ColdAdvisoryOwnerRefusal":
        """Start one phase's sampler run at most once; synchronous, never awaits.

        Args:
            phase: Exactly ``RECORDING_OFF`` or ``RECORDING_ON``.
            header: The exact phase header; ``RECORDING_ON`` must share the run id.
            established_session_id: The phase's established MCP session identity.
            scheduled_end_monotonic: The retained scheduled phase end.
            evaluator: The typed safety-evaluation port.
            sink: The durable advisory-attempt sink.
            ticks: The latest retained tick port.

        Returns:
            The stored start (``REFUSED_PREVIOUS_PHASE_NOT_SETTLED`` is not stored),
            or an unstored owner refusal.
        """
        if self._busy:
            return ColdAdvisoryOwnerRefusal.REENTRANT
        if not _admitted_phase(phase):
            return ColdAdvisoryOwnerRefusal.PHASE_NOT_ADMITTED
        stored = self._start.get(phase)
        if stored is not None:
            return stored
        self._busy = True
        try:
            return self._begin(
                phase,
                header,
                established_session_id,
                scheduled_end_monotonic,
                evaluator,
                sink,
                ticks,
            )
        finally:
            self._busy = False

    def _record(
        self, phase: ColdPhaseKind, start: ColdAdvisoryPhaseStart
    ) -> ColdAdvisoryPhaseStart:
        """Store and return one start result."""
        self._start[phase] = start
        return start

    def _begin(
        self,
        phase: ColdPhaseKind,
        header: ColdRunHeader,
        session: str,
        scheduled_end: float,
        evaluator: ColdAdvisoryEvaluatorPort,
        sink: ColdAdvisorySinkPort,
        ticks: ColdAdvisoryTickPort,
    ) -> ColdAdvisoryPhaseStart:
        """The staged start; each foreign stage pre-stores its interrupted result."""
        starts = ColdAdvisoryPhaseStart
        if phase is ColdPhaseKind.RECORDING_ON:
            refused = self._previous_refusal()
            if refused is not None:
                return refused
        snapshot = self._admitted_header(phase, header)
        if snapshot is None:
            return self._record(phase, starts.REFUSED_HEADER_NOT_ADMITTED)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._record(phase, starts.REFUSED_NO_RUNNING_LOOP)
        adapter = self._adapter
        if adapter is None:
            self._start[phase] = starts.REFUSED_FACTORY_INTERRUPTED
            try:
                raw = self._advisor_factory()
            except Exception:
                return self._record(phase, starts.REFUSED_FACTORY_RAISED)
            if not _admitted_advisor(raw):
                return self._record(phase, starts.REFUSED_FACTORY_RESULT_NOT_ADMITTED)
            adapter = self._adapter = _ExclusiveAdvisor(raw)
        self._start[phase] = starts.REFUSED_SAMPLER_CONSTRUCTION_INTERRUPTED
        # The adapter's access-time ``get_recommendation`` property yields the callable
        # the port's method would (the sampler only looks it up, then calls it); the
        # checker cannot equate a property with a protocol method, hence this cast.
        port = typing.cast(ColdAdvisoryAdvisorPort, adapter)
        try:
            sampler = ColdAdvisorySampler(
                header=header,
                established_session_id=session,
                scheduled_end_monotonic=scheduled_end,
                spec=self._spec,
                configured_call_bound_seconds=self._bound,
                configured_dwell_seconds=self._dwell,
                advisor=port,
                evaluator=evaluator,
                sink=sink,
                ticks=ticks,
                clock=self._clock,
            )
        except ColdAdvisorySamplerRefusedError:
            return self._record(phase, starts.REFUSED_SAMPLER_CONSTRUCTION)
        self._sampler[phase] = sampler
        if phase is ColdPhaseKind.RECORDING_OFF:
            self._off_header = snapshot
        self._start[phase] = starts.TASK_CREATION_UNCONFIRMED
        run = sampler.run()
        try:
            task = asyncio.create_task(run)
        except Exception:
            return starts.TASK_CREATION_UNCONFIRMED
        task.add_done_callback(_retrieve_run_exception)
        self._task[phase] = task
        return self._record(phase, starts.STARTED)

    def _previous_refusal(self) -> ColdAdvisoryPhaseStart | None:
        """``RECORDING_ON`` admission, checked before any ``RECORDING_OFF`` header read."""
        starts, on = ColdAdvisoryPhaseStart, ColdPhaseKind.RECORDING_ON
        settled = self._settlement.get(ColdPhaseKind.RECORDING_OFF)
        if settled is None:
            return starts.REFUSED_PREVIOUS_PHASE_NOT_SETTLED
        if settled.run_at_settlement.state is ColdAdvisoryRunTaskState.UNCONFIRMED:
            return self._record(on, starts.REFUSED_PREVIOUS_RUN_UNCONFIRMED)
        observed = self._sampler[ColdPhaseKind.RECORDING_OFF].observe_provider_task()
        if observed is None:
            return self._record(on, starts.REFUSED_PREVIOUS_PROVIDER_NOT_OBSERVABLE)
        if observed.task is ColdAdvisoryProviderTaskState.OUTSTANDING:
            return self._record(on, starts.REFUSED_PREVIOUS_PROVIDER_OUTSTANDING)
        return None

    def _admitted_header(self, phase: ColdPhaseKind, header: object) -> ColdRunHeader | None:
        """The validated header snapshot for ``phase``, or ``None``."""
        if type(header) is not ColdRunHeader:
            return None
        try:
            snapshot = typing.cast(ColdRunHeader, validate_record(header))
        except Exception:
            return None
        if snapshot.phase is not phase:
            return None
        if phase is ColdPhaseKind.RECORDING_ON:
            off = typing.cast(ColdRunHeader, self._off_header)
            if snapshot.run_id != off.run_id:
                return None
        return snapshot

    def _run_fact(self, phase: ColdPhaseKind) -> ColdAdvisoryPhaseRun:
        """The run task's state now; a missing handle alone is never ``NOT_STARTED``."""
        states = ColdAdvisoryRunTaskState
        task = self._task.get(phase)
        if task is None:
            if self._start.get(phase) is ColdAdvisoryPhaseStart.TASK_CREATION_UNCONFIRMED:
                return ColdAdvisoryPhaseRun(state=states.UNCONFIRMED, stop=None)
            return ColdAdvisoryPhaseRun(state=states.NOT_STARTED, stop=None)
        if not task.done():
            return ColdAdvisoryPhaseRun(state=states.PENDING, stop=None)
        if task.cancelled():
            return ColdAdvisoryPhaseRun(state=states.CANCELLED, stop=None)
        if task.exception() is not None:
            return ColdAdvisoryPhaseRun(state=states.RAISED, stop=None)
        return ColdAdvisoryPhaseRun(state=states.FINISHED, stop=task.result().stop)

    def settle_phase(
        self, phase: ColdPhaseKind
    ) -> "ColdAdvisoryPhaseSettlement | ColdAdvisoryOwnerRefusal":
        """Settle one started phase at most once; synchronous, never awaits or joins.

        Order: settle the sampler, snapshot the run fact, request provider
        cancellation, then request run cancellation only when a handle exists.
        Provisional facts are returned as refusals and nothing is stored or
        requested after them.  The stored run snapshot is taken by the call that
        completes and stores the settlement; an earlier call interrupted by a
        ``BaseException`` (for example from a provider future's ``cancel``) stores
        no snapshot, and the sampler's latched request is never repeated.

        Args:
            phase: Exactly ``RECORDING_OFF`` or ``RECORDING_ON``.

        Returns:
            The stored settlement, or an unstored owner refusal.
        """
        refusals = ColdAdvisoryOwnerRefusal
        if self._busy:
            return refusals.REENTRANT
        if not _admitted_phase(phase):
            return refusals.PHASE_NOT_ADMITTED
        stored = self._settlement.get(phase)
        if stored is not None:
            return stored
        sampler = self._sampler.get(phase)
        if sampler is None:
            return refusals.NOT_STARTED
        self._busy = True
        try:
            closed = sampler.settle_at_phase_end()
            if closed.closure is ColdAdvisorySettlementClosure.NOT_RECORDED_REENTRANT:
                return refusals.SAMPLER_PROVISIONAL
            run_at = self._run_fact(phase)
            requested = sampler.request_provider_cancellation()
            if (
                requested is ColdAdvisoryCancellationRequest.NOT_SETTLED
                or requested is ColdAdvisoryCancellationRequest.IN_PROGRESS
            ):
                return refusals.CANCELLATION_PROVISIONAL
            task = self._task.get(phase)
            run_cancel_requested = task is not None and not task.done() and task.cancel()
            settlement = ColdAdvisoryPhaseSettlement(
                sampler=closed,
                run_at_settlement=run_at,
                provider_cancellation=requested,
                run_cancel_requested=run_cancel_requested,
            )
            self._settlement[phase] = settlement
            return settlement
        finally:
            self._busy = False

    def observe_phase(
        self, phase: ColdPhaseKind
    ) -> "ColdAdvisoryPhaseObservation | ColdAdvisoryOwnerRefusal":
        """Observe one phase; pure and synchronous.

        Args:
            phase: Exactly ``RECORDING_OFF`` or ``RECORDING_ON``.

        Returns:
            The stored start and settlement with the live run and provider facts,
            or an unstored owner refusal.  ``UNCONFIRMED`` is reported as such.
        """
        if self._busy:
            return ColdAdvisoryOwnerRefusal.REENTRANT
        if not _admitted_phase(phase):
            return ColdAdvisoryOwnerRefusal.PHASE_NOT_ADMITTED
        sampler = self._sampler.get(phase)
        return ColdAdvisoryPhaseObservation(
            start=self._start.get(phase),
            run=self._run_fact(phase),
            provider=None if sampler is None else sampler.observe_provider_task(),
            settlement=self._settlement.get(phase),
        )
