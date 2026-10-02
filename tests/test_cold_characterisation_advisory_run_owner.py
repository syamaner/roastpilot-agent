"""Standalone advisory run and phase owner (#954 slice 5c-ii-a); hardware-free.

Every case drives the owner on the deterministic ``ManualClock`` with the sampler
test doubles (imported, not copied).  Labels: D = direct oracle, S = structural
(AST), R = redundant, W = white-box (private attribute access, test-only).  An
exception raised during the act step counts as a failure.  No provider, hardware,
network or process is used.  Tests own and terminate every task they create.
"""

import ast
import asyncio
import dataclasses
import enum
import gc
import typing
import warnings
from collections.abc import Callable
from pathlib import Path

import pydantic
import pytest

from roastpilot_agent.advisor import AdvisorContext
from roastpilot_agent.cold_characterisation import advisory_conformance as ac
from roastpilot_agent.cold_characterisation import advisory_run_owner as owner_module
from roastpilot_agent.cold_characterisation import advisory_sampler as sampler_module
from roastpilot_agent.cold_characterisation import evidence_advisory as advisory
from roastpilot_agent.cold_characterisation import evidence_schema as schema
from roastpilot_agent.cold_characterisation import evidence_store as store
from roastpilot_agent.cold_characterisation.advisory_run_owner import (
    ColdAdvisoryOwnerRefusal,
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
    ColdAdvisoryProviderTask,
    ColdAdvisoryProviderTaskState,
    ColdAdvisorySamplerStop,
    ColdAdvisorySettlementClosure,
    ColdAdvisoryTickPort,
)
from roastpilot_agent.config import SafetyLimits
from roastpilot_agent.models import RoastPhase
from roastpilot_agent.safety import SafetyPolicy
from tests.test_cold_characterisation_advisory_sampler import (
    DECISION,
    DESCRIPTOR,
    EAGER,
    FORBIDDEN_NAMES,
    MAX,
    OPEN,
    PHASES,
    PROFILE,
    SPEC,
    Act,
    CountingFuture,
    DoubleAdvisor,
    DoubleEvaluator,
    FutureAdvisor,
    Halt,
    ManualClock,
    Record,
    RecordingSink,
    TickPort,
    drive,
    drive_until,
    finish,
    fixture_run,
    idle,
    normal,
    priv,
    real_advisor,
    write_v3_sampled,
)
from tests.test_cold_characterisation_conformance import Plan, header_of, tick_record
from tests.test_cold_characterisation_evidence_store import OFF, ON

Start = ColdAdvisoryPhaseStart
Refusal = ColdAdvisoryOwnerRefusal
RunState = ColdAdvisoryRunTaskState
Req = ColdAdvisoryCancellationRequest
TaskState = ColdAdvisoryProviderTaskState
Call = ColdAdvisoryCallFact
Stop = ColdAdvisorySamplerStop
Closure = ColdAdvisorySettlementClosure
Fact = ColdAdvisoryProviderTask
#: Private owner members, reached through ``Any`` (labelled direct/white-box cases).
PRIVATE_OWNER: typing.Any = owner_module
OWNER_SOURCE = Path(owner_module.__file__)
OWNER_TREE = ast.parse(OWNER_SOURCE.read_text(encoding="utf-8"))
PACKAGE_ROOT = OWNER_SOURCE.parents[1]
FOREIGN_RUN_ID = "20260926T120000Z-cold-other"
CANARY = "CANARY-5c-ii-a"
NOT_STARTED_RUN = ColdAdvisoryPhaseRun(state=RunState.NOT_STARTED, stop=None)
CONTEXT = AdvisorContext(
    phase=RoastPhase.PREHEATING,
    roast_elapsed_seconds=0.0,
    development_elapsed_seconds=None,
    current_bean_temp_c=21.5,
    current_env_temp_c=22.0,
    bean_ror_c_per_min=None,
    env_ror_c_per_min=None,
    target_drop_temp_c=205.0,
    profile_name=PROFILE,
    charge_guidance_min_c=None,
    charge_guidance_max_c=190.0,
)


# ------------------------------------------------------------------ rig


@dataclasses.dataclass(frozen=True)
class OwnerBase:
    """The conforming plan with both phase headers and their context ticks."""

    run: Plan
    headers: dict[schema.ColdPhaseKind, schema.ColdRunHeader]
    ticks: dict[schema.ColdPhaseKind, schema.ColdTickRecord]


@pytest.fixture(scope="module")
def owner_base(tmp_path_factory: pytest.TempPathFactory) -> OwnerBase:
    """Build the shared headers and ticks once per module."""
    run = fixture_run(tmp_path_factory.mktemp("owner"))
    headers = {
        OFF: header_of(run.documents[OFF], OFF, 1.0),
        ON: header_of(run.documents[ON], ON, 2.0),
    }
    ticks = {phase: tick_record(headers[phase], run.ticks[phase][2]) for phase in (OFF, ON)}
    return OwnerBase(run, headers, ticks)


class Factory:
    """An advisor factory counting calls; one armed hook runs before ``make``."""

    def __init__(self, make: Callable[[], object]) -> None:
        self.make = make
        self.calls = 0
        self.on_call: Callable[[], object] | None = None

    def __call__(self) -> ColdAdvisoryAdvisorPort:
        """Count, run the hook, then return (or raise from) ``make``."""
        self.calls += 1
        hook, self.on_call = self.on_call, None
        if hook is not None:
            hook()
        return typing.cast(ColdAdvisoryAdvisorPort, self.make())


@dataclasses.dataclass
class OwnerRig:
    """One owner with its clock, advisor, factory, sinks and tick ports."""

    owner: ColdAdvisoryRunOwner
    clock: ManualClock
    advisor: typing.Any
    factory: Factory
    base: OwnerBase
    sinks: dict[schema.ColdPhaseKind, RecordingSink]
    ticks: dict[schema.ColdPhaseKind, TickPort]


def owner_rig(
    base: OwnerBase,
    *,
    clock: ManualClock | None = None,
    advisor: object = None,
    factory: Factory | None = None,
    dwell: float = MAX,
) -> OwnerRig:
    """An owner whose factory returns ``advisor`` (a ``DoubleAdvisor`` by default)."""
    clock = ManualClock(13.5) if clock is None else clock
    chosen: typing.Any = DoubleAdvisor(clock) if advisor is None else advisor
    factory = Factory(lambda: chosen) if factory is None else factory
    owner = ColdAdvisoryRunOwner(
        advisor_factory=factory,
        spec=SPEC,
        configured_call_bound_seconds=5.0,
        configured_dwell_seconds=dwell,
        clock=clock,
    )
    sinks = {OFF: RecordingSink(), ON: RecordingSink()}
    ticks = {OFF: TickPort(base.ticks[OFF]), ON: TickPort(base.ticks[ON])}
    return OwnerRig(owner, clock, chosen, factory, base, sinks, ticks)


_DEFAULT: typing.Any = object()


def begin(rig: OwnerRig, phase: schema.ColdPhaseKind, header: object = _DEFAULT) -> Start | Refusal:
    """Start ``phase`` with its own header (or ``header``), session, end and ports."""
    _, session, end = PHASES[phase]
    chosen: typing.Any = rig.base.headers[phase] if header is _DEFAULT else header
    evaluator: typing.Any = DoubleEvaluator()
    return rig.owner.start_phase(
        phase,
        header=chosen,
        established_session_id=session,
        scheduled_end_monotonic=end,
        evaluator=evaluator,
        sink=rig.sinks[phase],
        ticks=typing.cast(ColdAdvisoryTickPort, rig.ticks[phase]),
    )


def look(rig: OwnerRig, phase: schema.ColdPhaseKind) -> ColdAdvisoryPhaseObservation:
    """The phase observation, which must not be a refusal."""
    observation = rig.owner.observe_phase(phase)
    assert isinstance(observation, ColdAdvisoryPhaseObservation), observation
    return observation


def settled(rig: OwnerRig, phase: schema.ColdPhaseKind) -> ColdAdvisoryPhaseSettlement:
    """Settle ``phase`` and require a stored settlement."""
    settlement = rig.owner.settle_phase(phase)
    assert isinstance(settlement, ColdAdvisoryPhaseSettlement), settlement
    return settlement


def reentries(rig: OwnerRig) -> list[object]:
    """Start, settle and observe ``RECORDING_OFF`` from inside a port."""
    return [begin(rig, OFF), rig.owner.settle_phase(OFF), rig.owner.observe_phase(OFF)]


def run_task(rig: OwnerRig, phase: schema.ColdPhaseKind) -> "asyncio.Task[typing.Any]":
    """White-box: the owner's run task for ``phase`` (must exist)."""
    return typing.cast("asyncio.Task[typing.Any]", priv(rig.owner)._task[phase])


def provider_of(rig: OwnerRig, phase: schema.ColdPhaseKind) -> "asyncio.Task[None]":
    """White-box: the retained sampler's published provider task (must exist)."""
    task = priv(priv(rig.owner)._sampler[phase])._task
    assert task is not None
    return typing.cast("asyncio.Task[None]", task)


def _halt() -> None:
    raise Halt()


async def wind_owner(rig: OwnerRig, *extra: "asyncio.Task[typing.Any]") -> None:
    """Test cleanup: release providers, end every run and provider task, then idle."""
    advisor = rig.advisor
    if isinstance(advisor, FutureAdvisor):
        advisor.release.set()
        if not advisor.future.done():
            advisor.future.set_result(None)
    for task in [*priv(rig.owner)._task.values(), *extra]:
        await finish(task, rig.clock)
    for sampler in priv(rig.owner)._sampler.values():
        provider = priv(sampler)._task
        if provider is not None:
            if not provider.done():
                provider.cancel()
            await drive_until(provider.done)
    await idle()


def default_task(
    loop: asyncio.AbstractEventLoop, coro: typing.Any, **kwargs: typing.Any
) -> "asyncio.Task[typing.Any]":
    """The default task factory's behaviour."""
    return asyncio.Task(coro, loop=loop, **kwargs)


class InterruptingFactory:
    """A one-shot, outermost-only task factory that interrupts ``create_task``.

    On its first outermost call it delegates (unless ``delegate`` is ``None``), keeps
    the delegated task for test teardown only, runs ``on_delegated`` and raises a
    fresh ``error`` instance (so no stored traceback keeps the coroutine alive).
    Nested calls made during that delegation, and every later call, pass straight
    through.
    """

    def __init__(
        self,
        error: type[BaseException],
        delegate: Callable[..., typing.Any] | None = default_task,
        on_delegated: Callable[[], object] | None = None,
    ) -> None:
        self.error = error
        self.delegate = delegate
        self.on_delegated = on_delegated
        self.armed = True
        self.delegating = False
        self.retained: list[asyncio.Task[typing.Any]] = []

    def __call__(
        self, loop: asyncio.AbstractEventLoop, coro: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        """Interrupt the first outermost call; pass every other call through."""
        passthrough = self.delegate or default_task
        if self.delegating or not self.armed:
            return passthrough(loop, coro, **kwargs)
        self.armed = False
        if self.delegate is None:
            raise self.error()
        self.delegating = True
        try:
            task = self.delegate(loop, coro, **kwargs)
        finally:
            self.delegating = False
        self.retained.append(task)
        if self.on_delegated is not None:
            self.on_delegated()
        raise self.error()


def install(factory: InterruptingFactory) -> object:
    """Install the test task factory; return the previous one for restoration."""
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    loop.set_task_factory(typing.cast(typing.Any, factory))
    return previous


def restore(previous: object) -> None:
    """Restore the task factory captured by ``install``."""
    asyncio.get_running_loop().set_task_factory(typing.cast(typing.Any, previous))


# ------------------------------------------------------------------ baseline probes


@pytest.mark.asyncio
async def test_probe_one_task_cancel_calls_override_once() -> None:
    """Baseline probe: one ``task.cancel()`` calls the awaited future's override once."""
    future = CountingFuture()

    async def waiter() -> None:
        await future

    task = asyncio.create_task(waiter())
    await idle()
    assert task.cancel() is True
    assert future.requests == 1
    await asyncio.wait({task})
    assert task.cancelled()
    assert future.requests == 1


@pytest.mark.skipif(EAGER is None, reason="asyncio.eager_task_factory needs Python 3.12+")
@pytest.mark.asyncio
async def test_probe_eager_delegation_runs_before_interruption() -> None:
    """Baseline probe: the delegated eager coroutine is entered before the wrapper raises."""
    future = CountingFuture()
    entries: list[int] = []
    at_raise: list[int] = []

    async def provider() -> None:
        entries.append(1)
        await future

    factory = InterruptingFactory(Halt, EAGER, lambda: at_raise.append(len(entries)))
    previous = install(factory)
    coro = provider()
    try:
        with pytest.raises(Halt):
            asyncio.create_task(coro)
    finally:
        restore(previous)
    assert at_raise == [1]
    [task] = factory.retained
    future.set_result(None)
    await task
    assert entries == [1]


# ------------------------------------------------------------------ O-T1 integration


@pytest.mark.asyncio
async def test_owner_records_conform_policy_2_both_phases(tmp_path: Path) -> None:
    """O-T1: one owner across both phases, one factory call, policy-2 conformant records."""
    run = fixture_run(tmp_path)
    clock = ManualClock(13.5)
    factory = Factory(lambda: real_advisor(clock, normal))
    owner = ColdAdvisoryRunOwner(
        advisor_factory=factory,
        spec=SPEC,
        configured_call_bound_seconds=5.0,
        configured_dwell_seconds=5.0,
        clock=clock,
    )
    facts: dict[schema.ColdPhaseKind, tuple[object, object, object]] = {}

    async def drive_phase(
        phase: schema.ColdPhaseKind, header: schema.ColdRunHeader, writer: store.ColdEvidenceWriter
    ) -> list[Record]:
        start_at, session, end = PHASES[phase]
        clock.jump(start_at - clock.now)
        sink = RecordingSink(inner=writer)
        started = owner.start_phase(
            phase,
            header=header,
            established_session_id=session,
            scheduled_end_monotonic=end,
            evaluator=SafetyPolicy(SafetyLimits()),
            sink=sink,
            ticks=typing.cast(
                ColdAdvisoryTickPort, TickPort(tick_record(header, run.ticks[phase][2]))
            ),
        )
        await drive(clock, end)
        observed = owner.observe_phase(phase)
        assert isinstance(observed, ColdAdvisoryPhaseObservation)
        facts[phase] = (started, observed.run, owner.settle_phase(phase))
        await idle()
        return sink.records

    v3, recorded = await write_v3_sampled(tmp_path, run, drive_phase)
    assert factory.calls == 1
    finished = ColdAdvisoryPhaseRun(state=RunState.FINISHED, stop=Stop.WINDOW_EXHAUSTED)
    for phase in (OFF, ON):
        started, live, settlement = facts[phase]
        assert (started, live) == (Start.STARTED, finished)
        assert isinstance(settlement, ColdAdvisoryPhaseSettlement)
        assert settlement.run_at_settlement == finished
        assert settlement.sampler.closure is Closure.NO_OPEN_ATTEMPT
        assert settlement.provider_cancellation is Req.TASK_ALREADY_DONE
        assert settlement.run_cancel_requested is False
        intents = [
            r for r in recorded if type(r) is advisory.ColdAdvisoryIntentRecord and r.phase is phase
        ]
        assert len(intents) == 41
    assert v3.advisory_attempts == tuple(recorded)
    result = ac.check_advisory_conformance(v3)
    assert result.outcome is ac.ColdAdvisoryConformanceOutcome.ADVISORY_CONFORMANT
    assert result.findings == ()
    assert result.pre_advisory_findings == ()


# ------------------------------------------------------------------ factory admission (O-T2)


def _raise_canary() -> object:
    raise RuntimeError(CANARY)


class NoRecommendation:
    """A factory result with only ``descriptor_for``."""

    def descriptor_for(self, phase: RoastPhase) -> object:
        """Return the fixed descriptor."""
        return DESCRIPTOR


class NotCallableRecommendation(NoRecommendation):
    """A factory result whose ``get_recommendation`` is not callable."""

    get_recommendation = CANARY


FACTORY_REFUSALS: dict[str, tuple[Callable[[], object], Start]] = {
    "raises": (_raise_canary, Start.REFUSED_FACTORY_RAISED),
    "returns_none": (lambda: None, Start.REFUSED_FACTORY_RESULT_NOT_ADMITTED),
    "missing_method": (NoRecommendation, Start.REFUSED_FACTORY_RESULT_NOT_ADMITTED),
    "not_callable": (NotCallableRecommendation, Start.REFUSED_FACTORY_RESULT_NOT_ADMITTED),
    "getter_raises": (
        lambda: DoubleAdvisor(ManualClock(13.5), lookup_raises=True),
        Start.REFUSED_FACTORY_RESULT_NOT_ADMITTED,
    ),
}


@pytest.mark.parametrize("pid", sorted(FACTORY_REFUSALS))
@pytest.mark.asyncio
async def test_factory_refusal_is_stored(owner_base: OwnerBase, pid: str) -> None:
    """O-T2: exact stored refusal, one factory call, no raw text, nothing started."""
    make, expected = FACTORY_REFUSALS[pid]
    rig = owner_rig(owner_base, factory=Factory(make))
    assert begin(rig, OFF) is expected
    assert begin(rig, OFF) is expected
    assert rig.factory.calls == 1
    observation = look(rig, OFF)
    assert observation == ColdAdvisoryPhaseObservation(
        start=expected, run=NOT_STARTED_RUN, provider=None, settlement=None
    )
    assert CANARY not in observation.model_dump_json()
    assert CANARY not in repr(observation)
    assert rig.owner.settle_phase(OFF) is Refusal.NOT_STARTED
    assert begin(rig, ON) is Start.REFUSED_PREVIOUS_PHASE_NOT_SETTLED


@pytest.mark.asyncio
async def test_factory_result_getter_reentry(owner_base: OwnerBase) -> None:
    """O-T2: a reentry from an admission getter is refused; the start then completes."""
    rig = owner_rig(owner_base)
    inner: list[object] = []
    rig.advisor.on_lookup = lambda: inner.extend(reentries(rig))
    try:
        assert begin(rig, OFF) is Start.STARTED
        assert inner == [Refusal.REENTRANT] * 3
        assert (rig.factory.calls, rig.advisor.lookup_reads) == (1, 1)
    finally:
        await wind_owner(rig)


INTERRUPTED_STAGES: dict[str, Start] = {
    "factory": Start.REFUSED_FACTORY_INTERRUPTED,
    "getter": Start.REFUSED_FACTORY_INTERRUPTED,
    "descriptor": Start.REFUSED_SAMPLER_CONSTRUCTION_INTERRUPTED,
}


@pytest.mark.parametrize("pid", sorted(INTERRUPTED_STAGES))
@pytest.mark.asyncio
async def test_interrupted_start_stage_is_final(owner_base: OwnerBase, pid: str) -> None:
    """O-T2h/O-T2g/O-T2d (direct): an interrupted stage is the stored, final start.

    The factory, its result's ``get_recommendation`` getter, or the adapter's
    ``descriptor_for`` during sampler construction raises a ``BaseException``; it
    propagates, the stage's pre-stored result is returned on every repeat, and no
    foreign call is ever repeated.
    """
    rig = owner_rig(owner_base)
    expected = INTERRUPTED_STAGES[pid]
    if pid == "factory":
        rig.factory.on_call = _halt
    elif pid == "getter":
        rig.advisor.on_lookup = _halt
    else:
        rig.advisor.on_descriptor = _halt
    try:
        with pytest.raises(Halt):
            begin(rig, OFF)
        assert begin(rig, OFF) is expected
        assert begin(rig, OFF) is expected
        assert rig.factory.calls == 1
        assert rig.advisor.lookup_reads == (0 if pid == "factory" else 1)
        assert len(rig.advisor.phases) == (1 if pid == "descriptor" else 0)
        assert look(rig, OFF) == ColdAdvisoryPhaseObservation(
            start=expected, run=NOT_STARTED_RUN, provider=None, settlement=None
        )
        assert rig.owner.settle_phase(OFF) is Refusal.NOT_STARTED
        assert begin(rig, ON) is Start.REFUSED_PREVIOUS_PHASE_NOT_SETTLED
        await idle()
        assert rig.advisor.entries == []
    finally:
        await wind_owner(rig)


# ------------------------------------------------------------------ header and phase order (O-T3)


@pytest.mark.asyncio
async def test_on_requires_settled_off(owner_base: OwnerBase) -> None:
    """O-T3a: ``RECORDING_ON`` first is refused unstored; after OFF settles it starts."""
    rig = owner_rig(owner_base)
    try:
        assert begin(rig, ON) is Start.REFUSED_PREVIOUS_PHASE_NOT_SETTLED
        assert look(rig, ON).start is None
        assert rig.factory.calls == 0
        assert begin(rig, OFF) is Start.STARTED
        assert begin(rig, ON) is Start.REFUSED_PREVIOUS_PHASE_NOT_SETTLED
        settlement = settled(rig, OFF)
        assert settlement.run_at_settlement.state is RunState.PENDING
        assert settlement.provider_cancellation is Req.NO_PROVIDER_TASK
        assert settlement.run_cancel_requested is True
        assert begin(rig, ON) is Start.STARTED
        assert rig.factory.calls == 1
        assert look(rig, ON).run.state is RunState.PENDING
    finally:
        await wind_owner(rig)


class SubHeader(schema.ColdRunHeader):
    """A header subclass."""


HEADER_REFUSALS: dict[str, Callable[[OwnerBase], object]] = {
    "subclass": lambda b: SubHeader(**dict(b.headers[OFF])),
    "forged_digest": lambda b: b.headers[OFF].model_copy(
        update={"identity_sha256": "not-a-digest"}
    ),
    "wrong_phase": lambda b: b.headers[ON],
}


@pytest.mark.parametrize("pid", sorted(HEADER_REFUSALS))
@pytest.mark.asyncio
async def test_header_refusal_is_stored(owner_base: OwnerBase, pid: str) -> None:
    """O-T3b (direct): a refused header is stored; a valid replacement cannot restart."""
    rig = owner_rig(owner_base)
    fresh = owner_rig(owner_base)
    try:
        first = begin(rig, OFF, HEADER_REFUSALS[pid](owner_base))
        assert first is Start.REFUSED_HEADER_NOT_ADMITTED
        assert begin(rig, OFF) is first
        assert rig.factory.calls == 0
        assert priv(rig.owner)._sampler == {}
        assert rig.owner.settle_phase(OFF) is Refusal.NOT_STARTED
        assert begin(rig, ON) is Start.REFUSED_PREVIOUS_PHASE_NOT_SETTLED
        assert begin(fresh, OFF) is Start.STARTED
    finally:
        await wind_owner(rig)
        await wind_owner(fresh)


@pytest.mark.asyncio
async def test_on_foreign_run_id_is_refused(owner_base: OwnerBase) -> None:
    """O-T3c: an ``RECORDING_ON`` header for another run is refused and stored."""
    foreign = owner_base.headers[ON].model_copy(update={"run_id": FOREIGN_RUN_ID})
    assert schema.validate_record(foreign).run_id == FOREIGN_RUN_ID
    rig = owner_rig(owner_base)
    try:
        assert begin(rig, OFF) is Start.STARTED
        settled(rig, OFF)
        assert begin(rig, ON, foreign) is Start.REFUSED_HEADER_NOT_ADMITTED
        assert begin(rig, ON) is Start.REFUSED_HEADER_NOT_ADMITTED
        assert ON not in priv(rig.owner)._sampler
        assert rig.factory.calls == 1
    finally:
        await wind_owner(rig)


@pytest.mark.asyncio
async def test_sampler_construction_refusal(owner_base: OwnerBase) -> None:
    """O-T4: a refused sampler construction is stored; no run or descriptor call exists."""
    rig = owner_rig(owner_base, dwell=4.999)
    assert begin(rig, OFF) is Start.REFUSED_SAMPLER_CONSTRUCTION
    assert begin(rig, OFF) is Start.REFUSED_SAMPLER_CONSTRUCTION
    assert (rig.factory.calls, rig.advisor.phases) == (1, [])
    assert priv(rig.owner)._task == {}
    assert look(rig, OFF).run == NOT_STARTED_RUN
    assert rig.owner.settle_phase(OFF) is Refusal.NOT_STARTED


def test_start_outside_running_loop(owner_base: OwnerBase) -> None:
    """O-T5: no running loop is refused before any factory call or coroutine exists."""
    rig = owner_rig(owner_base)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert begin(rig, OFF) is Start.REFUSED_NO_RUNNING_LOOP
    assert [w for w in caught if issubclass(w.category, RuntimeWarning)] == []
    assert (rig.factory.calls, priv(rig.owner)._sampler, priv(rig.owner)._task) == (0, {}, {})
    assert begin(rig, OFF) is Start.REFUSED_NO_RUNNING_LOOP


# ------------------------------------------------------------------ ON admission (O-T6)


@pytest.mark.asyncio
async def test_on_refused_while_off_provider_outstanding(owner_base: OwnerBase) -> None:
    """O-T6: an OFF provider ignoring cancellation keeps ON refused, even after release."""
    future = CountingFuture()
    rig = owner_rig(owner_base, advisor=FutureAdvisor(future, "ignore"))
    try:
        assert begin(rig, OFF) is Start.STARTED
        await drive(rig.clock, OPEN)
        settlement = settled(rig, OFF)
        assert settlement.provider_cancellation is Req.REQUESTED
        assert settlement.run_at_settlement.state is RunState.PENDING
        await idle()
        provider = look(rig, OFF).provider
        assert provider is not None and provider.task is TaskState.OUTSTANDING
        assert begin(rig, ON) is Start.REFUSED_PREVIOUS_PROVIDER_OUTSTANDING
        assert look(rig, ON).start is Start.REFUSED_PREVIOUS_PROVIDER_OUTSTANDING
        rig.advisor.release.set()
        await drive_until(provider_of(rig, OFF).done)
        assert begin(rig, ON) is Start.REFUSED_PREVIOUS_PROVIDER_OUTSTANDING
        assert ON not in priv(rig.owner)._sampler
        assert (rig.advisor.entries, future.requests, rig.factory.calls) == (1, 1, 1)
    finally:
        await wind_owner(rig)


@pytest.mark.asyncio
async def test_on_refused_when_off_provider_not_observable(owner_base: OwnerBase) -> None:
    """O-T6b (white-box): a missing OFF observation is a stored refusal before the header."""
    rig = owner_rig(owner_base)
    try:
        assert begin(rig, OFF) is Start.STARTED
        settled(rig, OFF)
        priv(rig.owner)._sampler[OFF].observe_provider_task = lambda: None
        assert begin(rig, ON, None) is Start.REFUSED_PREVIOUS_PROVIDER_NOT_OBSERVABLE
        assert begin(rig, ON) is Start.REFUSED_PREVIOUS_PROVIDER_NOT_OBSERVABLE
        assert rig.factory.calls == 1
    finally:
        await wind_owner(rig)


# ------------------------------------------------------------------ adapter (O-T7)


@pytest.mark.asyncio
async def test_adapter_single_flight() -> None:
    """O-T7 (direct; flag checks white-box): overlap refused, first call unaffected."""
    future = CountingFuture()
    raw = FutureAdvisor(future)
    adapter = PRIVATE_OWNER._ExclusiveAdvisor(raw)
    assert adapter.last_usage is raw.usage
    assert adapter.descriptor_for(RoastPhase.PREHEATING) is DESCRIPTOR
    assert raw.descriptor_calls == 1
    first = asyncio.create_task(adapter.get_recommendation(CONTEXT))
    second: asyncio.Task[typing.Any] | None = None
    try:
        await idle()
        second = asyncio.create_task(adapter.get_recommendation(CONTEXT))
        await idle()
        assert second.done()
        assert type(second.exception()) is PRIVATE_OWNER._ConcurrentAdvisoryCall
        assert priv(adapter)._busy is True
        assert not first.done()
        future.set_result(None)
        assert await first is DECISION
        assert priv(adapter)._busy is False
        assert raw.entries == 1
    finally:
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.wait({task})


@pytest.mark.parametrize("pid", ["raises", "cancelled"])
@pytest.mark.asyncio
async def test_adapter_flag_cleared_on_every_exit(pid: str) -> None:
    """O-T7 (white-box): the flag clears when the call raises or is cancelled."""
    clock = ManualClock(13.5)
    if pid == "raises":
        adapter = PRIVATE_OWNER._ExclusiveAdvisor(
            DoubleAdvisor(clock, [Act(error=RuntimeError("advisor fault"))])
        )
        with pytest.raises(RuntimeError):
            await adapter.get_recommendation(CONTEXT)
    else:
        adapter = PRIVATE_OWNER._ExclusiveAdvisor(FutureAdvisor(CountingFuture()))
        task = asyncio.create_task(adapter.get_recommendation(CONTEXT))
        await idle()
        assert priv(adapter)._busy is True
        task.cancel()
        await asyncio.wait({task})
        assert task.cancelled()
    assert priv(adapter)._busy is False
    if pid == "raises":
        assert await adapter.get_recommendation(CONTEXT) is DECISION


# ------------------------------------------------------------------ settlement (O-T8 .. O-T11)


@pytest.mark.asyncio
async def test_settlement_order(owner_base: OwnerBase) -> None:
    """O-T8: settle, snapshot, request the provider cancel, then cancel the pending run."""
    future = CountingFuture()
    rig = owner_rig(owner_base, advisor=FutureAdvisor(future))
    try:
        assert begin(rig, OFF) is Start.STARTED
        await drive(rig.clock, OPEN)
        settlement = settled(rig, OFF)
        assert settlement.provider_cancellation is Req.REQUESTED
        assert settlement.run_at_settlement.state is RunState.PENDING
        assert settlement.run_cancel_requested is True
        assert settlement.sampler.closure is Closure.RECORDED_UNRESOLVED_INVOKED
        assert future.requests == 1
        await drive_until(run_task(rig, OFF).done)
        observation = look(rig, OFF)
        assert observation.run.state is RunState.CANCELLED
        assert observation.provider is not None
        assert observation.provider.task is TaskState.DONE_CANCELLED
    finally:
        await wind_owner(rig)


def _owner_method(name: str) -> ast.FunctionDef:
    owner_class = next(
        node
        for node in ast.walk(OWNER_TREE)
        if isinstance(node, ast.ClassDef) and node.name == "ColdAdvisoryRunOwner"
    )
    [method] = [
        node for node in owner_class.body if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    return method


def _call_line(function: ast.AST, attribute: str) -> int:
    [line] = [
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attribute
    ]
    return line


def test_settlement_order_structure() -> None:
    """O-T8 (structural): the settlement call order, and no owner method awaits."""
    settle = _owner_method("settle_phase")
    lines = [
        _call_line(settle, name)
        for name in ("settle_at_phase_end", "_run_fact", "request_provider_cancellation", "cancel")
    ]
    assert lines == sorted(lines)
    owner_class = next(
        node
        for node in ast.walk(OWNER_TREE)
        if isinstance(node, ast.ClassDef) and node.name == "ColdAdvisoryRunOwner"
    )
    assert [
        n for n in ast.walk(owner_class) if isinstance(n, ast.Await | ast.AsyncFunctionDef)
    ] == []


@pytest.mark.parametrize("pid", ["finished", "pending_then_cancelled", "raised"])
@pytest.mark.asyncio
async def test_run_fact_snapshot(owner_base: OwnerBase, pid: str) -> None:
    """O-T9: the stored run snapshot never changes; the live observation does."""
    rig = owner_rig(owner_base, clock=ManualClock(OPEN if pid == "raised" else 13.5))
    if pid == "raised":
        rig.ticks[OFF] = TickPort(Halt())
    rig.clock.cancel_lag = 3
    try:
        assert begin(rig, OFF) is Start.STARTED
        if pid == "finished":
            await drive(rig.clock, 1800.0)
        else:
            await idle()
        settlement = settled(rig, OFF)
        expected = {
            "finished": ColdAdvisoryPhaseRun(state=RunState.FINISHED, stop=Stop.WINDOW_EXHAUSTED),
            "pending_then_cancelled": ColdAdvisoryPhaseRun(state=RunState.PENDING, stop=None),
            "raised": ColdAdvisoryPhaseRun(state=RunState.RAISED, stop=None),
        }[pid]
        assert settlement.run_at_settlement == expected
        assert settlement.run_cancel_requested is (pid == "pending_then_cancelled")
        assert look(rig, OFF).run == expected
        await drive_until(run_task(rig, OFF).done)
        live = RunState.CANCELLED if pid == "pending_then_cancelled" else expected.state
        assert look(rig, OFF).run.state is live
        assert rig.owner.settle_phase(OFF) is settlement
        assert settlement.run_at_settlement == expected
        assert look(rig, OFF).settlement is settlement
    finally:
        await wind_owner(rig)


@pytest.mark.asyncio
async def test_run_exception_retrieved_without_observation(owner_base: OwnerBase) -> None:
    """O-T9b (white-box): a raised run's exception is retrieved by the done callback."""
    rig = owner_rig(owner_base, clock=ManualClock(OPEN))
    rig.ticks[OFF] = TickPort(Halt())
    try:
        assert begin(rig, OFF) is Start.STARTED
        task = run_task(rig, OFF)
        await drive_until(task.done)
        await idle()
        assert type(priv(task)._exception) is Halt
        assert priv(task)._log_traceback is False
        gc.collect()
    finally:
        await wind_owner(rig)


REENTRY = ["factory", "descriptor", "on_cancel", "ticks", "sink_append"]


@pytest.mark.parametrize("pid", REENTRY)
@pytest.mark.asyncio
async def test_reentry_and_provisional_settlement(owner_base: OwnerBase, pid: str) -> None:
    """O-T10: reentry while busy is refused; a provisional sampler fact is inert.

    Factory, descriptor and cancel-override reentries happen while the owner is
    busy.  Tick and sink callbacks run inside the run task with the sampler's port
    depth or commit latch set: a settlement there is ``SAMPLER_PROVISIONAL`` with no
    request, no run cancel and nothing stored; a later outside settlement is genuine.
    """
    future = CountingFuture()
    advisor = FutureAdvisor(future) if pid == "on_cancel" else None
    rig = owner_rig(owner_base, advisor=advisor)
    inner: list[object] = []

    def busy_hook() -> None:
        inner.extend(reentries(rig))

    def provisional_hook() -> None:
        inner.append(rig.owner.settle_phase(OFF))
        inner.append(look(rig, OFF).settlement)

    def on_append(record: Record) -> None:
        if type(record) is advisory.ColdAdvisoryIntentRecord:
            provisional_hook()

    if pid == "factory":
        rig.factory.on_call = busy_hook
    elif pid == "descriptor":
        rig.advisor.on_descriptor = busy_hook
    elif pid == "on_cancel":
        future.on_cancel = busy_hook
    elif pid == "ticks":
        rig.ticks[OFF].on_call = provisional_hook
    else:
        rig.sinks[OFF].on_append = on_append
    try:
        assert begin(rig, OFF) is Start.STARTED
        await drive(rig.clock, OPEN)
        if pid in {"ticks", "sink_append"}:
            assert inner == [Refusal.SAMPLER_PROVISIONAL, None]
            await drive_until(run_task(rig, OFF).done)
            assert look(rig, OFF).run == ColdAdvisoryPhaseRun(
                state=RunState.FINISHED, stop=Stop.SETTLED
            )
            assert look(rig, OFF).settlement is None
            settlement = settled(rig, OFF)
            closure = (
                Closure.NO_OPEN_ATTEMPT
                if pid == "ticks"
                else Closure.RECORDED_UNRESOLVED_NOT_INVOKED
            )
            assert settlement.sampler.closure is closure
            assert settlement.provider_cancellation is Req.NO_PROVIDER_TASK
            assert settlement.run_cancel_requested is False
        else:
            if pid == "on_cancel":
                assert inner == []
            settlement = settled(rig, OFF)
            assert inner == [Refusal.REENTRANT] * 3
            if pid == "on_cancel":
                assert (settlement.provider_cancellation, future.requests) == (Req.REQUESTED, 1)
        assert rig.owner.settle_phase(OFF) is settlement
    finally:
        await wind_owner(rig)


@pytest.mark.asyncio
async def test_cancellation_provisional_is_inert(owner_base: OwnerBase) -> None:
    """O-T10b (white-box): a provisional request stores nothing and cancels no run."""
    rig = owner_rig(owner_base)
    try:
        assert begin(rig, OFF) is Start.STARTED
        await idle()
        priv(rig.owner)._sampler[OFF].request_provider_cancellation = lambda: Req.IN_PROGRESS
        assert rig.owner.settle_phase(OFF) is Refusal.CANCELLATION_PROVISIONAL
        assert look(rig, OFF).settlement is None
        await idle()
        task = run_task(rig, OFF)
        assert (task.done(), task.cancelling()) == (False, 0)
        assert look(rig, OFF).run.state is RunState.PENDING
    finally:
        await wind_owner(rig)


@pytest.mark.asyncio
async def test_late_provider_release_after_settlement(owner_base: OwnerBase) -> None:
    """O-T11: a released provider changes the live facts only; the stored ones stand."""
    rig = owner_rig(owner_base, advisor=FutureAdvisor(CountingFuture(), "ignore"))
    try:
        assert begin(rig, OFF) is Start.STARTED
        await drive(rig.clock, OPEN)
        settlement = settled(rig, OFF)
        assert settlement.sampler.provider_task is Fact.OUTSTANDING
        await idle()
        before = look(rig, OFF).provider
        assert before is not None and before.task is TaskState.OUTSTANDING
        rig.advisor.release.set()
        await drive_until(provider_of(rig, OFF).done)
        after = look(rig, OFF).provider
        assert after is not None
        assert (after.task, after.call) == (
            TaskState.DONE_RETURNED,
            Call.DISCARDED_AFTER_SETTLEMENT,
        )
        assert rig.owner.settle_phase(OFF) is settlement
        assert settlement.sampler.provider_task is Fact.OUTSTANDING
    finally:
        await wind_owner(rig)


@pytest.mark.asyncio
async def test_interrupted_owner_settlement_is_not_stored(owner_base: OwnerBase) -> None:
    """O-T17: a ``BaseException`` from a provider future's ``cancel`` stops the owner call.

    The sampler settlement and its ``REQUEST_INTERRUPTED`` request stay stored; the
    owner stores nothing and cancels no run.  The later completed owner call takes
    its own run snapshot and never repeats the provider request.
    """
    future = CountingFuture(error=Halt())
    rig = owner_rig(owner_base, advisor=FutureAdvisor(future))
    try:
        assert begin(rig, OFF) is Start.STARTED
        await drive(rig.clock, OPEN)
        with pytest.raises(Halt):
            rig.owner.settle_phase(OFF)
        observation = look(rig, OFF)
        assert observation.settlement is None
        assert observation.provider is not None
        assert observation.provider.settlement_cancellation is Req.REQUEST_INTERRUPTED
        assert observation.provider.task is TaskState.OUTSTANDING
        task = run_task(rig, OFF)
        assert (task.done(), task.cancelling(), future.requests) == (False, 0, 1)
        rig.clock.jump(5.0)
        await drive_until(task.done)
        settlement = settled(rig, OFF)
        assert settlement.run_at_settlement == ColdAdvisoryPhaseRun(
            state=RunState.FINISHED, stop=Stop.SETTLED
        )
        assert settlement.run_cancel_requested is False
        assert settlement.provider_cancellation is Req.REQUEST_INTERRUPTED
        assert settlement.sampler.closure is Closure.RECORDED_UNRESOLVED_INVOKED
        assert future.requests == 1
        assert rig.owner.settle_phase(OFF) is settlement
    finally:
        await wind_owner(rig)


LATE_LOOKUPS = ["raises", "not_callable", "reenters"]
SCHEDULING = [
    "ordinary",
    pytest.param(
        "eager",
        marks=pytest.mark.skipif(EAGER is None, reason="asyncio.eager_task_factory needs 3.12+"),
    ),
]


@pytest.mark.parametrize("scheduling", SCHEDULING)
@pytest.mark.parametrize("pid", LATE_LOOKUPS)
@pytest.mark.asyncio
async def test_late_lookup_keeps_sampler_classification(
    owner_base: OwnerBase, pid: str, scheduling: str
) -> None:
    """O-T16a/b/c (direct): a getter admitted at start but failing later is never invoked.

    After owner start completes, the sampler's own counted lookup through the real
    adapter raises, yields a non-callable, or re-enters settlement.  The provider
    task refuses before invocation (no entry, no stamp) and the existing not-invoked
    classification stands; the re-entry is ``SAMPLER_PROVISIONAL`` with no owner
    action, and a later settlement records the existing sampler facts.
    """
    rig = owner_rig(owner_base)
    inner: list[object] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    if scheduling == "eager":
        loop.set_task_factory(EAGER)
    try:
        assert begin(rig, OFF) is Start.STARTED
        assert (rig.factory.calls, rig.advisor.lookup_reads) == (1, 1)
        if pid == "raises":
            rig.advisor.lookup_raises = True
        elif pid == "not_callable":
            rig.advisor.override_lookup = True
        else:
            rig.advisor.on_lookup = lambda: inner.append(rig.owner.settle_phase(OFF))
        await drive(rig.clock, OPEN)
        await drive_until(run_task(rig, OFF).done)
        assert (rig.advisor.lookup_reads, rig.advisor.entries) == (2, [])
        cell = priv(priv(rig.owner)._sampler[OFF])._open[1]
        assert (cell.invoked, cell.invocation) == (False, None)
        stop = Stop.SETTLED if pid == "reenters" else Stop.NOT_INVOKED
        assert look(rig, OFF).run == ColdAdvisoryPhaseRun(state=RunState.FINISHED, stop=stop)
        assert inner == ([Refusal.SAMPLER_PROVISIONAL] if pid == "reenters" else [])
        assert look(rig, OFF).settlement is None
        settlement = settled(rig, OFF)
        assert settlement.sampler.closure is Closure.RECORDED_UNRESOLVED_NOT_INVOKED
        assert settlement.sampler.provider_task is Fact.NONE
        assert settlement.provider_cancellation is Req.TASK_ALREADY_DONE
        assert settlement.run_cancel_requested is False
        provider = look(rig, OFF).provider
        assert provider is not None
        assert (provider.task, provider.call) == (
            TaskState.DONE_RETURNED,
            Call.REFUSED_BEFORE_INVOCATION,
        )
        [record] = rig.sinks[OFF].resolutions
        assert (record.invocation_monotonic, record.invocation_utc) == (None, None)
    finally:
        loop.set_task_factory(previous)
        await wind_owner(rig)


# ------------------------------------------------------------------ runtime task factories


@pytest.mark.skipif(EAGER is None, reason="asyncio.eager_task_factory needs Python 3.12+")
@pytest.mark.asyncio
async def test_eager_run_completes_inside_create_task(owner_base: OwnerBase) -> None:
    """O-T12 (3.12): the eager run completes inside ``create_task``; port reentry refused."""
    rig = owner_rig(owner_base, clock=ManualClock(OPEN))
    inner: list[object] = []
    rig.ticks[OFF].on_call = lambda: inner.extend(
        [rig.owner.settle_phase(OFF), rig.owner.observe_phase(OFF)]
    )
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    loop.set_task_factory(EAGER)
    try:
        assert begin(rig, OFF) is Start.STARTED
        finished = ColdAdvisoryPhaseRun(state=RunState.FINISHED, stop=Stop.WINDOW_EXHAUSTED)
        assert look(rig, OFF).run == finished
        assert inner == [Refusal.REENTRANT, Refusal.REENTRANT]
        assert rig.advisor.entries == [OPEN]
        settlement = settled(rig, OFF)
        assert settlement.run_at_settlement == finished
        assert settlement.provider_cancellation is Req.TASK_ALREADY_DONE
    finally:
        loop.set_task_factory(previous)
        await wind_owner(rig)


async def _interrupted_start(
    owner_base: OwnerBase, delegate: Callable[..., typing.Any]
) -> tuple[OwnerRig, InterruptingFactory, CountingFuture, list[int]]:
    """Start OFF at the window open under an interrupting task factory (``Halt``)."""
    future = CountingFuture()
    rig = owner_rig(owner_base, clock=ManualClock(OPEN), advisor=FutureAdvisor(future))
    at_raise: list[int] = []
    factory = InterruptingFactory(Halt, delegate, lambda: at_raise.append(rig.advisor.entries))
    previous = install(factory)
    try:
        with pytest.raises(Halt):
            begin(rig, OFF)
    finally:
        restore(previous)
    return rig, factory, future, at_raise


async def _assert_unconfirmed_start(
    rig: OwnerRig, factory: InterruptingFactory, future: CountingFuture
) -> None:
    """The shared O-T14 oracle after the interrupted start (entries already checked)."""
    unconfirmed = ColdAdvisoryPhaseRun(state=RunState.UNCONFIRMED, stop=None)
    observation = look(rig, OFF)
    assert (observation.start, observation.run) == (Start.TASK_CREATION_UNCONFIRMED, unconfirmed)
    assert begin(rig, OFF) is Start.TASK_CREATION_UNCONFIRMED
    assert (rig.advisor.descriptor_calls, rig.factory.calls) == (1, 1)
    assert len(factory.retained) == 1
    await idle()
    assert rig.advisor.entries == 1
    settlement = settled(rig, OFF)
    assert settlement.run_at_settlement == unconfirmed
    assert settlement.run_cancel_requested is False
    assert (settlement.provider_cancellation, future.requests) == (Req.REQUESTED, 1)
    assert settlement.sampler.closure is Closure.RECORDED_UNRESOLVED_INVOKED
    await drive_until(provider_of(rig, OFF).done)
    assert begin(rig, ON) is Start.REFUSED_PREVIOUS_RUN_UNCONFIRMED
    assert begin(rig, ON) is Start.REFUSED_PREVIOUS_RUN_UNCONFIRMED
    await drive(rig.clock, 1800.0)
    assert rig.advisor.entries == 1
    assert look(rig, OFF).run == unconfirmed


@pytest.mark.asyncio
async def test_interrupted_task_creation_is_unconfirmed(owner_base: OwnerBase) -> None:
    """O-T14 (direct, 3.11+): ``create_task`` interrupted after delegation is unconfirmed.

    The orphaned run is driven after the interruption; it never yields a second
    provider, the owner never retries, ``UNCONFIRMED`` is never ``NOT_STARTED``, the
    published provider task is still reached by the latched request, and ON is refused.
    """
    rig, factory, future, at_raise = await _interrupted_start(owner_base, default_task)
    try:
        assert at_raise == [0]
        await _assert_unconfirmed_start(rig, factory, future)
    finally:
        await wind_owner(rig, *factory.retained)


@pytest.mark.skipif(EAGER is None, reason="asyncio.eager_task_factory needs Python 3.12+")
@pytest.mark.asyncio
async def test_interrupted_eager_task_creation_is_unconfirmed(owner_base: OwnerBase) -> None:
    """O-T14e (direct, 3.12 eager): interrupted after the eager run entered the provider."""
    rig, factory, future, at_raise = await _interrupted_start(owner_base, EAGER)
    try:
        assert at_raise == [1]
        await _assert_unconfirmed_start(rig, factory, future)
    finally:
        await wind_owner(rig, *factory.retained)


@pytest.mark.asyncio
async def test_task_creation_raising_is_unconfirmed(owner_base: OwnerBase) -> None:
    """O-T15 (direct): an ordinary ``create_task`` failure is unknown, never refused."""
    rig = owner_rig(owner_base, clock=ManualClock(OPEN))
    factory = InterruptingFactory(RuntimeError, None)
    previous = install(factory)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert begin(rig, OFF) is Start.TASK_CREATION_UNCONFIRMED
    finally:
        restore(previous)
    messages = [str(w.message) for w in caught if issubclass(w.category, RuntimeWarning)]
    assert messages.count("coroutine 'ColdAdvisorySampler.run' was never awaited") == 1
    try:
        assert begin(rig, OFF) is Start.TASK_CREATION_UNCONFIRMED
        assert OFF in priv(rig.owner)._sampler
        assert factory.retained == []
        assert rig.advisor.entries == []
        unconfirmed = ColdAdvisoryPhaseRun(state=RunState.UNCONFIRMED, stop=None)
        assert look(rig, OFF).run == unconfirmed
        assert (len(rig.advisor.phases), rig.factory.calls) == (1, 1)
        settlement = settled(rig, OFF)
        assert settlement.run_at_settlement == unconfirmed
        assert settlement.provider_cancellation is Req.NO_PROVIDER_TASK
        assert settlement.run_cancel_requested is False
        assert begin(rig, ON) is Start.REFUSED_PREVIOUS_RUN_UNCONFIRMED
    finally:
        await wind_owner(rig)


# ------------------------------------------------------------------ models and phase identity


@pytest.mark.parametrize(
    ("state", "stop"),
    [(RunState.FINISHED, None), (RunState.PENDING, Stop.WINDOW_EXHAUSTED)],
)
def test_phase_run_stop_iff_finished(state: RunState, stop: Stop | None) -> None:
    """A stop is present exactly when the run finished."""
    with pytest.raises(pydantic.ValidationError):
        ColdAdvisoryPhaseRun(state=state, stop=stop)


@pytest.mark.asyncio
async def test_phase_result_models_are_closed(owner_base: OwnerBase) -> None:
    """Real settlement and observation results are frozen, strict and refuse extras."""
    rig = owner_rig(owner_base)
    try:
        assert begin(rig, OFF) is Start.STARTED
        settlement = settled(rig, OFF)
        observation = look(rig, OFF)
    finally:
        await wind_owner(rig)
    assert observation.settlement is settlement
    settlement_values: dict[str, object] = {
        "sampler": settlement.sampler,
        "run_at_settlement": settlement.run_at_settlement,
        "provider_cancellation": settlement.provider_cancellation,
        "run_cancel_requested": settlement.run_cancel_requested,
    }
    observation_values: dict[str, object] = {
        "start": observation.start,
        "run": observation.run,
        "provider": observation.provider,
        "settlement": observation.settlement,
    }
    assert ColdAdvisoryPhaseSettlement.model_validate(settlement_values) == settlement
    assert ColdAdvisoryPhaseObservation.model_validate(observation_values) == observation
    rejected: list[tuple[type[pydantic.BaseModel], dict[str, object], dict[str, object]]] = [
        (ColdAdvisoryPhaseSettlement, settlement_values, {"provider_cancellation": "requested"}),
        (ColdAdvisoryPhaseSettlement, settlement_values, {"run_cancel_requested": 1}),
        (ColdAdvisoryPhaseSettlement, settlement_values, {"undeclared": 1}),
        (ColdAdvisoryPhaseObservation, observation_values, {"start": "started"}),
        (ColdAdvisoryPhaseObservation, observation_values, {"run": None}),
        (ColdAdvisoryPhaseObservation, observation_values, {"undeclared": 1}),
    ]
    for model, values, change in rejected:
        with pytest.raises(pydantic.ValidationError):
            model.model_validate({**values, **change})
    with pytest.raises(pydantic.ValidationError):
        typing.cast(typing.Any, settlement).run_cancel_requested = False
    with pytest.raises(pydantic.ValidationError):
        typing.cast(typing.Any, observation).start = None
    assert (settlement.run_cancel_requested, observation.start) == (True, Start.STARTED)


@pytest.mark.asyncio
async def test_phase_identity_is_exact(owner_base: OwnerBase) -> None:
    """Only the two exact phase members are admitted; a matching string is not."""
    rig = owner_rig(owner_base)
    for phase in ("recording_off", None, Stop.SETTLED):
        candidate: typing.Any = phase
        started = rig.owner.start_phase(
            candidate,
            header=owner_base.headers[OFF],
            established_session_id=PHASES[OFF][1],
            scheduled_end_monotonic=PHASES[OFF][2],
            evaluator=typing.cast(typing.Any, DoubleEvaluator()),
            sink=rig.sinks[OFF],
            ticks=typing.cast(ColdAdvisoryTickPort, rig.ticks[OFF]),
        )
        assert started is Refusal.PHASE_NOT_ADMITTED
        assert rig.owner.settle_phase(candidate) is Refusal.PHASE_NOT_ADMITTED
        assert rig.owner.observe_phase(candidate) is Refusal.PHASE_NOT_ADMITTED
    assert rig.factory.calls == 0


# ------------------------------------------------------------------ fences (O-T13, O-T13b)

OWNER_ALLOWED_IMPORTS: dict[str, set[str] | None] = {
    "collections.abc": None,
    "roastpilot_agent.advisor": {
        "AdvisorContext",
        "AdvisorDescriptor",
        "AdvisorUsage",
        "RoastDecision",
    },
    "roastpilot_agent.models": {"RoastPhase"},
    "roastpilot_agent.cold_characterisation.advisory_sampler": set(sampler_module.__all__),
    "roastpilot_agent.cold_characterisation.evidence_schema": {
        "ColdPhaseKind",
        "ColdRunHeader",
        "validate_record",
    },
}
OWNER_FORBIDDEN_MODULES = {
    "time", "os", "sys", "signal", "subprocess", "logging", "threading", "inspect", "config",
    "mcp", "mcp_client", "engine", "engine_policy", "two_phase", "conformance",
    "advisory_conformance", "advisory_window", "evidence_store", "evidence_reader",
    "evidence_lifecycle", "evidence_builders", "host", "identity", "pydantic_ai",
}  # fmt: skip


def _owner_class_named(name: str) -> ast.ClassDef:
    return next(n for n in ast.walk(OWNER_TREE) if isinstance(n, ast.ClassDef) and n.name == name)


def _fence(pid: str) -> None:
    nodes = list(ast.walk(OWNER_TREE))
    if pid == "imports":
        for node in nodes:
            if isinstance(node, ast.Import):
                assert {a.name for a in node.names} <= {"asyncio", "enum", "typing", "pydantic"}
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0
                module = node.module or ""
                assert module in OWNER_ALLOWED_IMPORTS, module
                allowed = OWNER_ALLOWED_IMPORTS[module]
                assert allowed is None or {a.name for a in node.names} <= allowed
                assert not set(module.split(".")) & OWNER_FORBIDDEN_MODULES
    elif pid == "names":
        for node in nodes:
            if isinstance(node, ast.Name):
                assert node.id not in FORBIDDEN_NAMES | {"all_tasks"}, node.id
            if isinstance(node, ast.Attribute):
                assert node.attr not in FORBIDDEN_NAMES | {"format", "all_tasks"}, node.attr
            if isinstance(node, ast.keyword):
                assert node.arg not in FORBIDDEN_NAMES
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                assert node.name not in FORBIDDEN_NAMES
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"print", "str", "repr", "format"}, node.func.id
            if isinstance(node, ast.JoinedStr):
                assert all(isinstance(value, ast.Constant) for value in node.values)
    elif pid == "no_private_reads":
        for node in nodes:
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                assert isinstance(node.value, ast.Name) and node.value.id == "self", ast.unparse(
                    node
                )
    elif pid == "single_run_cancel":
        cancels = [
            n for n in nodes if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "cancel"
        ]
        assert [ast.unparse(c.func) for c in cancels] == ["task.cancel"]
        assert cancels[0] in list(ast.walk(_owner_method("settle_phase")))
    elif pid == "await_only_in_adapter_wrapper":
        asyncs = [n for n in nodes if isinstance(n, ast.AsyncFunctionDef)]
        assert [n.name for n in asyncs] == ["single_flight"]
        adapter = _owner_class_named("_ExclusiveAdvisor")
        [lookup] = [
            n
            for n in adapter.body
            if isinstance(n, ast.FunctionDef) and n.name == "get_recommendation"
        ]
        assert [ast.unparse(d) for d in lookup.decorator_list] == ["property"]
        assert asyncs[0] in list(ast.walk(lookup))
        awaits = [n for n in nodes if isinstance(n, ast.Await)]
        assert len(awaits) == 1
        assert awaits[0] in list(ast.walk(asyncs[0]))
        scheduling = {"create_task", "ensure_future", "gather", "wait", "wait_for", "shield"}
        spawns = [
            n
            for n in nodes
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") in scheduling
        ]
        assert [ast.unparse(c.func) for c in spawns] == ["asyncio.create_task"]
        assert spawns[0] in list(ast.walk(_owner_method("_begin")))
    elif pid == "no_base_exception_handlers":
        types = [
            "" if n.type is None else ast.unparse(n.type)
            for n in nodes
            if isinstance(n, ast.ExceptHandler)
        ]
        assert types and "" not in types
        assert not set(types) & {"BaseException", "KeyboardInterrupt", "SystemExit"}
    else:
        assert owner_module.__all__ == (
            "ColdAdvisoryOwnerRefusal",
            "ColdAdvisoryPhaseObservation",
            "ColdAdvisoryPhaseRun",
            "ColdAdvisoryPhaseSettlement",
            "ColdAdvisoryPhaseStart",
            "ColdAdvisoryRunOwner",
            "ColdAdvisoryRunTaskState",
        )
        for member in (Start, Refusal, RunState):
            assert issubclass(member, enum.Enum) and not issubclass(member, str)
            assert all(item.value == item.name.lower() for item in member)
        for model in (
            ColdAdvisoryPhaseRun,
            ColdAdvisoryPhaseSettlement,
            ColdAdvisoryPhaseObservation,
        ):
            config = model.model_config
            assert (config.get("frozen"), config.get("strict"), config.get("extra")) == (
                True,
                True,
                "forbid",
            )


FENCES = [
    "imports",
    "names",
    "no_private_reads",
    "single_run_cancel",
    "await_only_in_adapter_wrapper",
    "no_base_exception_handlers",
    "public_surface",
]


@pytest.mark.parametrize("pid", FENCES)
def test_owner_fences(pid: str) -> None:
    """O-T13 (structural): the owner's import, capability and syntax fences."""
    _fence(pid)


def test_owner_has_no_production_caller() -> None:
    """O-T13b (structural): no other source module imports the owner."""
    importers: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if path.resolve() == OWNER_SOURCE.resolve():
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or "", *(alias.name for alias in node.names)]
            else:
                continue
            if any("advisory_run_owner" in name for name in names):
                importers.append(str(path))
    assert PACKAGE_ROOT.name == "roastpilot_agent"
    assert importers == []
