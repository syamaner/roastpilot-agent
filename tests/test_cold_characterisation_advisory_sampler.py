"""Standalone observation-only advisory sampler (#954 slice 5c-i); hardware-free.

Every case drives the sampler with a deterministic ``ManualClock`` (no wall clock):
time moves only by explicit ``advance``/``service``/``jump`` calls or by the bounded
``drive`` helper, and queued delays are ``call_soon`` callbacks whose ordering each
test asserts.  Production-path cases use a real ``PydanticAIAdvisor`` over a recorded
``FunctionModel`` and a real ``SafetyPolicy``; control-path cases use a hookable
advisor double.  White-box (private attribute), direct private-helper and AST
structural cases are labelled.  No provider, hardware, network or process is used.
"""

import ast
import asyncio
import dataclasses
import enum
import inspect
import math
import sys
import typing
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pydantic
import pytest
from pydantic_ai import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from roastpilot_agent.advisor import (
    AdvisorContext,
    AdvisorDescriptor,
    AdvisorMalformedOutputError,
    AdvisorProviderError,
    AdvisorUnsafeOutputError,
    AdvisorUsage,
    PydanticAIAdvisor,
    RoastDecision,
)
from roastpilot_agent.cold_characterisation import advisory_conformance as ac
from roastpilot_agent.cold_characterisation import advisory_sampler as sampler_module
from roastpilot_agent.cold_characterisation import evidence_advisory as advisory
from roastpilot_agent.cold_characterisation import evidence_builders as builders
from roastpilot_agent.cold_characterisation import evidence_lifecycle as lifecycle
from roastpilot_agent.cold_characterisation import evidence_reader as reader
from roastpilot_agent.cold_characterisation import evidence_schema as schema
from roastpilot_agent.cold_characterisation import evidence_store as store
from roastpilot_agent.cold_characterisation.advisory_sampler import (
    ColdAdvisoryAdvisorPort,
    ColdAdvisoryClockPort,
    ColdAdvisoryEvaluatorPort,
    ColdAdvisoryProviderTask,
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
from roastpilot_agent.cold_characterisation.engine import MonotonicEngineClock
from roastpilot_agent.config import AdvisorConfig, SafetyLimits
from roastpilot_agent.models import RoastPhase
from roastpilot_agent.safety import SafetyEvaluation, SafetyPolicy, SafetyVerdict
from tests.test_cold_characterisation_conformance import (
    S_OFF,
    S_ON,
    T0,
    Plan,
    finalisation,
    header_of,
    host_record,
    plan,
    tick_record,
)
from tests.test_cold_characterisation_evidence_builders import RUN_ID
from tests.test_cold_characterisation_evidence_store import OFF, ON, open_writer

Kind = advisory.ColdAdvisoryResolution
Stop = ColdAdvisorySamplerStop
Closure = ColdAdvisorySettlementClosure
Fact = ColdAdvisoryProviderTask
Intent = advisory.ColdAdvisoryIntentRecord
Resolution = advisory.ColdAdvisoryResolutionRecord
Record = Intent | Resolution
F = ac.ColdAdvisoryConformanceFinding
#: Private module members, reached through ``Any`` (labelled direct/white-box cases).
PRIVATE: typing.Any = sampler_module
SOURCE = Path(sampler_module.__file__)
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
MAX = sys.float_info.max
STEP_CAP = 10_000
#: Hand-derived from ``plan()``: OFF activation 10.0 so S is 1810.0; ON S is 3630.0.
OPEN, CLOSE, S_END = 1450.0, 1750.0, 1810.0
UTC_BASE = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
PROFILE = "Cold characterisation"
SPEC = ColdAdvisorySpec(
    profile_name=PROFILE,
    target_drop_temp_c=205.0,
    charge_guidance_min_c=None,
    charge_guidance_max_c=190.0,
)
DESCRIPTOR = AdvisorDescriptor(
    provider="openai_compatible", model="test/model", prompt_version="v1"
)
DECISION = RoastDecision(
    target_heat=40, target_fan=60, should_drop=False, confidence=0.5, rationale="Hold heat."
)
REPLY: dict[str, object] = {
    "target_heat": 40,
    "target_fan": 60,
    "should_drop": False,
    "confidence": 0.5,
    "rationale": "Hold heat.",
}
CONTEXT_KEYWORDS = frozenset(
    {
        "phase",
        "roast_elapsed_seconds",
        "development_elapsed_seconds",
        "current_bean_temp_c",
        "current_env_temp_c",
        "bean_ror_c_per_min",
        "env_ror_c_per_min",
        "target_drop_temp_c",
        "profile_name",
        "charge_guidance_min_c",
        "charge_guidance_max_c",
        "first_crack_detected",
        "seconds_since_charge",
    }
)


#: Model classes reached through ``Any`` so hostile ``model_construct`` carriers type-check.
UNCHECKED_DECISION: typing.Any = RoastDecision
UNCHECKED_USAGE: typing.Any = AdvisorUsage
UNCHECKED_EVALUATION: typing.Any = SafetyEvaluation


def priv(value: object) -> typing.Any:
    """Return a value as ``Any`` for labelled white-box access to private state."""
    return value


# ------------------------------------------------------------------ manual clock


class ManualClock:
    """Deterministic clock: ``advance`` moves time only; ``service`` wakes due sleepers."""

    def __init__(self, start: float, *, utc: Callable[[float], str] | None = None) -> None:
        self.now = start
        self._utc = utc
        self.monotonic_calls = 0
        self.utc_calls = 0
        self.on_monotonic: Callable[[], object] | None = None
        self.on_utc: Callable[[], object] | None = None
        self.on_sleep_cancelled: Callable[[], object] | None = None
        self.cancel_lag = 0
        self.fail_next_monotonic = False
        self.regress_next_to: float | None = None
        self.bad_next_utc = False
        self.raise_on_next_sleep = False
        self.cancel_self_on_next_sleep = False
        #: Monotonic call number -> ``"raise"`` or a returned float.
        self.monotonic_plan: dict[int, object] = {}
        self.registered: list[float] = []
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = 0

    def monotonic(self) -> float:
        """Return ``now`` after one armed hook, unless a fault switch fires."""
        self.monotonic_calls += 1
        hook, self.on_monotonic = self.on_monotonic, None
        if hook is not None:
            hook()
        planned = self.monotonic_plan.pop(self.monotonic_calls, None)
        if self.fail_next_monotonic or planned == "raise":
            self.fail_next_monotonic = False
            raise RuntimeError("clock fault")
        if type(planned) is float:
            return planned
        if self.regress_next_to is not None:
            value, self.regress_next_to = self.regress_next_to, None
            return value
        return self.now

    def utc_now_iso(self) -> str:
        """Return a UTC instant derived from ``now`` (or the injected mapping)."""
        self.utc_calls += 1
        hook, self.on_utc = self.on_utc, None
        if hook is not None:
            hook()
        if self.bad_next_utc:
            self.bad_next_utc = False
            return "not-a-utc-instant"
        if self._utc is not None:
            return self._utc(self.now)
        return (UTC_BASE + timedelta(seconds=self.now)).isoformat()

    async def sleep(self, seconds: float) -> None:
        """Register a sleeper at ``now + max(seconds, 0)`` and wait for ``service``."""
        if self.raise_on_next_sleep:
            self.raise_on_next_sleep = False
            raise RuntimeError("sleep fault")
        if self.cancel_self_on_next_sleep:
            self.cancel_self_on_next_sleep = False
            raise asyncio.CancelledError
        deadline = self.now + max(seconds, 0.0)
        self.registered.append(deadline)
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        entry = (deadline, self._seq, future)
        self._seq += 1
        self._sleepers.append(entry)
        try:
            await future
        except asyncio.CancelledError:
            if entry in self._sleepers:
                self._sleepers.remove(entry)
            hook, self.on_sleep_cancelled = self.on_sleep_cancelled, None
            if hook is not None:
                hook()
            for _ in range(self.cancel_lag):
                await asyncio.sleep(0)
            raise

    def advance(self, seconds: float) -> None:
        """Move time only; never wakes a sleeper."""
        self.now += seconds

    def service(self) -> None:
        """Wake every sleeper whose deadline is at or before ``now``, in order."""
        for entry in sorted(item for item in self._sleepers if item[0] <= self.now):
            self._sleepers.remove(entry)
            if not entry[2].done():
                entry[2].set_result(None)

    def wake_all(self) -> None:
        """Wake every sleeper early (a platform early wake-up)."""
        for entry in list(self._sleepers):
            self._sleepers.remove(entry)
            if not entry[2].done():
                entry[2].set_result(None)

    def jump(self, seconds: float) -> None:
        """``advance`` then ``service``."""
        self.advance(seconds)
        self.service()

    def deadlines(self) -> list[float]:
        """Live sleeper deadlines."""
        return [item[0] for item in self._sleepers if not item[2].done()]

    def live_sleepers(self) -> int:
        """Count of live sleepers."""
        return len(self.deadlines())


async def idle() -> None:
    """Yield until the event loop has no ready callback, under a step cap."""
    loop: typing.Any = asyncio.get_running_loop()
    for _ in range(50):
        await asyncio.sleep(0)
    for _ in range(STEP_CAP):
        if not loop._ready:
            return
        await asyncio.sleep(0)
    raise AssertionError("step cap")


async def drive(clock: ManualClock, until: float, *, steps: int = STEP_CAP) -> None:
    """Service due sleepers, else jump to the earliest deadline at or before ``until``."""
    for _ in range(steps):
        await idle()
        if any(deadline <= clock.now for deadline in clock.deadlines()):
            clock.service()
            continue
        pending = [deadline for deadline in clock.deadlines() if deadline <= until]
        if not pending:
            return
        clock.now = max(clock.now, min(pending))
        clock.service()
    raise AssertionError("step cap")


async def drive_until(predicate: Callable[[], bool], *, steps: int = STEP_CAP) -> None:
    """Yield until ``predicate()`` holds; never moves time."""
    for _ in range(steps):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("step cap")


# ------------------------------------------------------------------ port doubles


@dataclasses.dataclass
class Act:
    """One scripted advisor call."""

    mode: typing.Literal[
        "return", "raise", "latency", "block", "ignore_cancel", "raise_cancelled"
    ] = "return"
    value: object = None
    error: BaseException | None = None
    seconds: float = 0.0
    event: asyncio.Event | None = None
    fresh: bool = True


class DoubleAdvisor:
    """A hookable advisor double counting every getter, lookup and entry."""

    def __init__(
        self,
        clock: ManualClock,
        acts: Sequence[Act] = (),
        *,
        usage_mode: typing.Literal["fresh", "fixed", "raise"] = "fresh",
        descriptor: object = DESCRIPTOR,
        lookup_raises: bool = False,
        post_usage: Callable[[], object] | None = None,
    ) -> None:
        self.clock = clock
        self.acts = list(acts)
        self.usage_mode = usage_mode
        self.descriptor = descriptor
        self.lookup_raises = lookup_raises
        self.post_usage = post_usage
        self.usage: object = AdvisorUsage(input_tokens=100, output_tokens=10, total_tokens=110)
        self.getter_reads = 0
        self.lookup_reads = 0
        self.cancels = 0
        self.getter_errors: dict[int, BaseException] = {}
        self.entries: list[float] = []
        self.contexts: list[AdvisorContext] = []
        self.phases: list[RoastPhase] = []
        self.on_getter: Callable[[], object] | None = None
        self.on_lookup: Callable[[], object] | None = None
        self.on_enter: Callable[[], object] | None = None
        self.on_descriptor: Callable[[], object] | None = None
        self.schedule_on_return: Callable[[], object] | None = None

    @property
    def last_usage(self) -> object:
        """Count the read, run one armed hook, then return (or raise) the usage."""
        self.getter_reads += 1
        hook, self.on_getter = self.on_getter, None
        if hook is not None:
            hook()
        error = self.getter_errors.get(self.getter_reads)
        if error is not None:
            raise error
        if self.usage_mode == "raise":
            raise RuntimeError("usage fault")
        return self.usage

    @property
    def get_recommendation(self) -> Callable[[AdvisorContext], Awaitable[object]]:
        """Count the method lookup and run one armed hook."""
        self.lookup_reads += 1
        hook, self.on_lookup = self.on_lookup, None
        if hook is not None:
            hook()
        if self.lookup_raises:
            raise RuntimeError("lookup fault")
        return self._recommend

    def descriptor_for(self, phase: RoastPhase) -> object:
        """Return (or raise) the scripted descriptor."""
        self.phases.append(phase)
        hook, self.on_descriptor = self.on_descriptor, None
        if hook is not None:
            hook()
        if isinstance(self.descriptor, BaseException):
            raise self.descriptor
        return self.descriptor

    async def _recommend(self, context: AdvisorContext) -> object:
        self.contexts.append(context)
        self.entries.append(self.clock.now)
        hook, self.on_enter = self.on_enter, None
        if hook is not None:
            hook()
        act = self.acts.pop(0) if self.acts else Act()
        if act.mode == "latency":
            await self.clock.sleep(act.seconds)
        elif act.mode == "block":
            try:
                await typing.cast(asyncio.Event, act.event).wait()
            except asyncio.CancelledError:
                self.cancels += 1
                raise
        elif act.mode == "ignore_cancel":
            while True:
                try:
                    await typing.cast(asyncio.Event, act.event).wait()
                    break
                except asyncio.CancelledError:
                    self.cancels += 1
        elif act.mode == "raise_cancelled":
            raise asyncio.CancelledError
        if self.usage_mode == "fresh" and act.fresh:
            self.usage = (
                self.post_usage()
                if self.post_usage is not None
                else AdvisorUsage(input_tokens=100, output_tokens=10, total_tokens=110)
            )
        scheduled, self.schedule_on_return = self.schedule_on_return, None
        if scheduled is not None:
            asyncio.get_running_loop().call_soon(scheduled)
        if act.error is not None:
            raise act.error
        return DECISION if act.value is None else act.value


class DoubleEvaluator:
    """Records keyword arguments; delegates to a real ``SafetyPolicy`` by default."""

    def __init__(
        self,
        result: Callable[[dict[str, typing.Any]], object] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, typing.Any]] = []
        self.on_call: Callable[[], object] | None = None
        self.policy = SafetyPolicy(SafetyLimits())

    def evaluate_command(self, **kwargs: typing.Any) -> object:
        """Record the call, run one armed hook, then return or raise."""
        self.calls.append(dict(kwargs))
        hook, self.on_call = self.on_call, None
        if hook is not None:
            hook()
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result(kwargs)
        return self.policy.evaluate_command(**kwargs)


class RecordingSink:
    """Records each call, forwards it, then runs ``on_append`` for an accepted record."""

    def __init__(
        self,
        inner: store.ColdEvidenceWriter | None = None,
        *,
        refuse: Callable[[Record], BaseException | None] | None = None,
        on_append: Callable[[Record], object] | None = None,
    ) -> None:
        self.inner = inner
        self.refuse = refuse
        self.on_append = on_append
        self.calls: list[Record] = []
        self.records: list[Record] = []

    def append_advisory_attempt(self, record: Record) -> None:
        """Record the call; refuse, forward, keep, then run the hook."""
        self.calls.append(record)
        error = None if self.refuse is None else self.refuse(record)
        if error is not None:
            raise error
        if self.inner is not None:
            self.inner.append_advisory_attempt(record)
        self.records.append(record)
        if self.on_append is not None:
            self.on_append(record)

    @property
    def intents(self) -> list[Intent]:
        """Accepted intent records."""
        return [record for record in self.records if type(record) is Intent]

    @property
    def resolutions(self) -> list[Resolution]:
        """Accepted resolution records."""
        return [record for record in self.records if type(record) is Resolution]


class TickPort:
    """Returns (or raises) one scripted tick and runs one armed hook."""

    def __init__(self, record: object, on_call: Callable[[], object] | None = None) -> None:
        self.record = record
        self.on_call = on_call
        self.calls = 0

    def latest_retained_tick(self) -> object:
        """Return the scripted tick."""
        self.calls += 1
        hook, self.on_call = self.on_call, None
        if hook is not None:
            hook()
        if isinstance(self.record, BaseException):
            raise self.record
        return self.record


# ------------------------------------------------------------------ real advisor


@dataclasses.dataclass(frozen=True)
class Latency:
    """Sleep on the manual clock inside the model call."""

    seconds: float


@dataclasses.dataclass(frozen=True)
class Reply:
    """Call the output tool with ``REPLY`` plus overrides."""

    values: dict[str, object] = dataclasses.field(default_factory=dict[str, object])


@dataclasses.dataclass(frozen=True)
class Prose:
    """Return prose only (never the output tool)."""


@dataclasses.dataclass(frozen=True)
class Http:
    """Raise a provider HTTP 503."""


@dataclasses.dataclass(frozen=True)
class Timeout:
    """Raise ``TimeoutError``."""


@dataclasses.dataclass(frozen=True)
class Block:
    """Wait for an event inside the model call."""

    event: asyncio.Event


Step = Latency | Reply | Prose | Http | Timeout | Block
Script = Callable[[int], Sequence[Step]]


def scripted_model(clock: ManualClock, script: Script) -> FunctionModel:
    """A recorded ``FunctionModel`` whose n-th model call follows ``script(n)``."""
    calls = [0]

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        index = calls[0]
        calls[0] += 1
        for step in script(index):
            if isinstance(step, Latency):
                await clock.sleep(step.seconds)
            elif isinstance(step, Block):
                await step.event.wait()
            elif isinstance(step, Http):
                raise ModelHTTPError(503, "x", "down")
            elif isinstance(step, Timeout):
                raise TimeoutError
            elif isinstance(step, Prose):
                return ModelResponse(parts=[TextPart("prose only")])
            else:
                arguments = {**REPLY, **step.values}
                return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, arguments)])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, dict(REPLY))])

    return FunctionModel(respond)


def real_advisor(
    clock: ManualClock,
    script: Script = lambda index: (Reply(),),
    by_phase: dict[RoastPhase, str] | None = None,
) -> PydanticAIAdvisor:
    """A real production advisor over a recorded model."""
    config = AdvisorConfig(
        provider="openai_compatible",
        model_slug="test/model",
        prompt_version="v1",
        model_slug_by_phase=by_phase or {},
    )
    return PydanticAIAdvisor(config, model=scripted_model(clock, script))


# ------------------------------------------------------------------ fixture rig


def fixture_run(tmp_path: Path) -> Plan:
    """The conforming base plan with the identity advisor provider made admissible."""
    run = plan(tmp_path)
    for phase in (OFF, ON):
        document = run.documents[phase]
        assert document["advisor_provider"] == "openrouter"
        assert (document["advisor_model"], document["advisor_prompt_version"]) == (
            "test/model",
            "v1",
        )
        document["advisor_provider"] = "openai_compatible"
    return run


@dataclasses.dataclass(frozen=True)
class Base:
    """The shared OFF header and the OFF tick at index 2 (13.0)."""

    run: Plan
    header: schema.ColdRunHeader
    tick: schema.ColdTickRecord


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> Base:
    """Build the shared fixture once per module."""
    run = fixture_run(tmp_path_factory.mktemp("sampler"))
    header = header_of(run.documents[OFF], OFF, 1.0)
    return Base(run, header, tick_record(header, run.ticks[OFF][2]))


@dataclasses.dataclass
class Rig:
    """One sampler and its doubles."""

    sampler: ColdAdvisorySampler
    clock: ManualClock
    advisor: typing.Any
    sink: RecordingSink
    ticks: TickPort
    evaluator: typing.Any


def make(
    base: Base,
    *,
    clock: ManualClock | None = None,
    acts: Sequence[Act] = (),
    advisor: object = None,
    sink: RecordingSink | None = None,
    ticks: TickPort | None = None,
    evaluator: object = None,
    **overrides: typing.Any,
) -> Rig:
    """Construct a sampler with the contract defaults and any overrides."""
    clock = ManualClock(13.5) if clock is None else clock
    advisor = DoubleAdvisor(clock, acts) if advisor is None else advisor
    sink = RecordingSink() if sink is None else sink
    ticks = TickPort(base.tick) if ticks is None else ticks
    evaluator = DoubleEvaluator() if evaluator is None else evaluator
    arguments: dict[str, typing.Any] = {
        "header": base.header,
        "established_session_id": S_OFF,
        "scheduled_end_monotonic": S_END,
        "spec": SPEC,
        "configured_call_bound_seconds": 5.0,
        "configured_dwell_seconds": 5.0,
        "advisor": advisor,
        "evaluator": evaluator,
        "sink": sink,
        "ticks": ticks,
        "clock": clock,
        **overrides,
    }
    return Rig(ColdAdvisorySampler(**arguments), clock, advisor, sink, ticks, evaluator)


def start(rig: Rig) -> "asyncio.Task[ColdAdvisorySamplerRun]":
    """Start the run as a task."""
    return asyncio.create_task(rig.sampler.run())


async def run_all(rig: Rig, until: float = 1800.0) -> ColdAdvisorySamplerRun:
    """Run to completion under ``drive`` and return the result."""
    task = start(rig)
    await drive(rig.clock, until)
    assert task.done()
    assert not task.cancelled()
    assert task.exception() is None, type(task.exception())
    return task.result()


def only(rig: Rig) -> Resolution:
    """Assert exactly one accepted resolution and return it."""
    assert len(rig.sink.resolutions) == 1, len(rig.sink.resolutions)
    return rig.sink.resolutions[0]


def construct(base: Base, **overrides: typing.Any) -> Rig | BaseException:
    """Construct, returning (not raising) any exception, so oracles assert on it."""
    try:
        return make(base, **overrides)
    except Exception as error:
        return error


def admitted(base: Base, **overrides: typing.Any) -> Rig:
    """Construct and assert that construction was admitted."""
    rig = construct(base, **overrides)
    assert isinstance(rig, Rig), type(rig)
    return rig


def settle_safely(rig: Rig) -> ColdAdvisorySettlement:
    """Settle and assert that settlement itself never raises (AC-S13)."""
    try:
        return rig.sampler.settle_at_phase_end()
    except Exception as error:
        raise AssertionError(type(error).__name__) from None


def settler(rig: Rig, box: list[ColdAdvisorySettlement]) -> Callable[[], None]:
    """A hook that settles and keeps the returned fact."""

    def call() -> None:
        box.append(rig.sampler.settle_at_phase_end())

    return call


def first_intent(rig: Rig, hook: Callable[[], object]) -> None:
    """Run ``hook`` once, from the sink, right after the first intent append."""

    def on_append(record: Record) -> None:
        if type(record) is Intent and record.attempt_index == 0:
            hook()

    rig.sink.on_append = on_append


def provider_task(rig: Rig) -> "asyncio.Task[None]":
    """White-box: the sampler's current provider task (must exist)."""
    task = priv(rig.sampler)._task
    assert task is not None
    return typing.cast("asyncio.Task[None]", task)


def provider_settled(rig: Rig) -> bool:
    """White-box: no provider task exists, or it is done."""
    task = priv(rig.sampler)._task
    return task is None or bool(task.done())


def provider_cell(rig: Rig) -> typing.Any:
    """White-box: the open attempt's private cell."""
    return priv(rig.sampler)._open[1]


async def cancel_and_wait(task: "asyncio.Task[typing.Any]") -> None:
    """Cancel a task and wait for it to end (test cleanup only)."""
    task.cancel()
    await asyncio.wait({task})


async def finish(task: "asyncio.Task[typing.Any]", clock: ManualClock) -> None:
    """Bounded test cleanup: cancel the run and wake sleepers until it has ended."""
    for _ in range(STEP_CAP):
        if task.done():
            return
        task.cancel()
        clock.wake_all()
        await asyncio.sleep(0)
    raise AssertionError("cleanup step cap")


def forged(model: pydantic.BaseModel, mutate: Callable[[dict[object, object]], None]) -> typing.Any:
    """A shallow copy whose raw ``__dict__`` was mutated (a hostile carrier)."""
    copy = model.model_copy()
    mutate(typing.cast(dict[object, object], object.__getattribute__(copy, "__dict__")))
    return copy


class SpyKey(str):
    """A ``str`` subclass key counting every hash and equality."""

    calls = 0

    def __hash__(self) -> int:
        SpyKey.calls += 1
        return str.__hash__(self)

    def __eq__(self, other: object) -> bool:
        SpyKey.calls += 1
        return str.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        SpyKey.calls += 1
        return str.__ne__(self, other)


class SubStr(str):
    """A ``str`` subclass."""


def rename(name: str, new: object) -> Callable[[dict[object, object]], None]:
    """Replace one raw key, keeping the cardinality."""

    def mutate(data: dict[object, object]) -> None:
        data[new] = data.pop(name)

    return mutate


def add_key(data: dict[object, object]) -> None:
    """Add one undeclared raw key."""
    data["undeclared"] = 1


def with_extra(model: pydantic.BaseModel) -> typing.Any:
    """A copy carrying a non-empty ``__pydantic_extra__``."""
    copy = model.model_copy()
    object.__setattr__(copy, "__pydantic_extra__", {"undeclared": 1})
    return copy


# ------------------------------------------------------------------ construction


class SubHeader(schema.ColdRunHeader):
    """A header subclass."""


class SubSpec(ColdAdvisorySpec):
    """A spec subclass."""


class SubDescriptor(AdvisorDescriptor):
    """A descriptor subclass."""


Overrides = Callable[[Base], dict[str, typing.Any]]


def _advisor_with(descriptor: object) -> dict[str, typing.Any]:
    return {"advisor": DoubleAdvisor(ManualClock(13.5), descriptor=descriptor)}


def _spy(model: pydantic.BaseModel, name: str) -> typing.Any:
    value = forged(model, rename(name, SpyKey(name)))
    SpyKey.calls = 0
    return value


REFUSALS: dict[str, Overrides] = {
    "header_subclass": lambda b: {"header": SubHeader(**dict(b.header))},
    "header_forged_digest": lambda b: {
        "header": b.header.model_copy(update={"identity_sha256": "not-a-digest"})
    },
    "session_empty": lambda b: {"established_session_id": ""},
    "session_space": lambda b: {"established_session_id": " "},
    "session_tab": lambda b: {"established_session_id": "\t"},
    "session_2049_bytes": lambda b: {"established_session_id": "é" * 1025},
    "session_subclass": lambda b: {"established_session_id": SubStr(S_OFF)},
    "session_surrogate": lambda b: {"established_session_id": "s\ud800"},
    "end_int": lambda b: {"scheduled_end_monotonic": 1810},
    "end_true": lambda b: {"scheduled_end_monotonic": True},
    "end_nan": lambda b: {"scheduled_end_monotonic": math.nan},
    "end_inf": lambda b: {"scheduled_end_monotonic": math.inf},
    "spec_subclass": lambda b: {"spec": SubSpec(**dict(SPEC))},
    "spec_extra_key": lambda b: {"spec": forged(SPEC, add_key)},
    "spec_spy_key": lambda b: {"spec": _spy(SPEC, "profile_name")},
    "spec_missing_key": lambda b: {"spec": forged(SPEC, rename("profile_name", "profile_nam"))},
    "spec_pydantic_extra": lambda b: {"spec": with_extra(SPEC)},
    "spec_forged_blank_profile": lambda b: {
        "spec": SPEC.model_copy(update={"profile_name": "   "})
    },
    "bound_zero": lambda b: {"configured_call_bound_seconds": 0.0},
    "bound_int": lambda b: {"configured_call_bound_seconds": 5},
    "bound_nan": lambda b: {"configured_call_bound_seconds": math.nan},
    "dwell_4999": lambda b: {"configured_dwell_seconds": 4.999},
    "dwell_true": lambda b: {"configured_dwell_seconds": True},
    "descriptor_raises": lambda b: _advisor_with(RuntimeError("descriptor fault")),
    "descriptor_subclass": lambda b: _advisor_with(SubDescriptor(**dict(DESCRIPTOR))),
    "descriptor_extra_key": lambda b: _advisor_with(forged(DESCRIPTOR, add_key)),
    "descriptor_spy_key": lambda b: _advisor_with(_spy(DESCRIPTOR, "model")),
    "descriptor_blank_model": lambda b: _advisor_with(
        AdvisorDescriptor(provider="openai_compatible", model=" ", prompt_version="v1")
    ),
}


@pytest.mark.parametrize("pid", sorted(REFUSALS))
def test_construction_refuses(base: Base, pid: str) -> None:
    """Every refused input raises the fixed, causeless refusal; spy keys are never hashed."""
    overrides = REFUSALS[pid](base)
    refused = construct(base, **overrides)
    assert type(refused) is ColdAdvisorySamplerRefusedError, type(refused)
    assert refused.args == ("Cold advisory sampler refused.",)
    assert refused.__cause__ is None
    assert refused.__context__ is None
    if pid.endswith("spy_key"):
        assert SpyKey.calls == 0


@pytest.mark.parametrize(
    ("pid", "overrides"),
    [
        pytest.param(pid, overrides, id=pid)
        for pid, overrides in (
            ("session_padded", {"established_session_id": " s "}),
            ("profile_padded", {"spec": SPEC.model_copy(update={"profile_name": " P "})}),
            ("dwell_exact_floor", {"configured_dwell_seconds": 5.0}),
            ("bound_tiny", {"configured_call_bound_seconds": 5e-324}),
            ("bound_max_float", {"configured_call_bound_seconds": MAX}),
        )
    ],
)
def test_construction_admits_edge_values(
    base: Base, pid: str, overrides: dict[str, typing.Any]
) -> None:
    """White-box: edge values are admitted and retained byte-exact."""
    sampler = priv(admitted(base, **overrides).sampler)
    if pid == "session_padded":
        assert sampler._session == " s "
    elif pid == "profile_padded":
        assert sampler._spec.profile_name == " P "
    elif pid == "dwell_exact_floor":
        assert sampler._dwell == 5.0
    else:
        assert sampler._bound == overrides["configured_call_bound_seconds"]


SPEC_REFUSALS: dict[str, dict[str, object]] = {
    "blank_space": {"profile_name": " "},
    "blank_tab": {"profile_name": "\t"},
    "chars_2049": {"profile_name": "a" * 2049},
    "bytes_2049": {"profile_name": "é" * 1025},
    "str_subclass": {"profile_name": SubStr("P")},
    "target_int": {"target_drop_temp_c": 205},
    "target_inf": {"target_drop_temp_c": math.inf},
    "guidance_true": {"charge_guidance_min_c": True},
    "guidance_nan": {"charge_guidance_max_c": math.nan},
    "surrogate": {"profile_name": "P\ud800"},
}


@pytest.mark.parametrize("pid", sorted(SPEC_REFUSALS))
def test_spec_model_refuses(pid: str) -> None:
    """The spec model has no defaults, no range and admits only exact finite values."""
    try:
        ColdAdvisorySpec.model_validate({**dict(SPEC), **SPEC_REFUSALS[pid]})
    except pydantic.ValidationError:
        refused = True
    else:
        refused = False
    assert refused


class DictSubclass(dict[str, object]):
    """A ``dict`` subclass standing in for a model's raw state."""


def _with_dict(model: pydantic.BaseModel, data: dict[str, object]) -> typing.Any:
    copy = model.model_copy()
    object.__setattr__(copy, "__dict__", data)
    return copy


def _unset_extra(model: pydantic.BaseModel) -> typing.Any:
    blank = object.__new__(type(model))
    object.__setattr__(blank, "__dict__", dict(model))
    return blank


SHAPE_NAMES = ("provider", "model", "prompt_version")
SHAPE_CASES: dict[str, Callable[[], object]] = {
    "missing_key": lambda: forged(DESCRIPTOR, rename("model", "modle")),
    "extra_key": lambda: forged(DESCRIPTOR, add_key),
    "spy_key": lambda: _spy(DESCRIPTOR, "model"),
    "nonempty_extra": lambda: with_extra(DESCRIPTOR),
    "subclass": lambda: SubDescriptor(**dict(DESCRIPTOR)),
    "dict_subclass": lambda: _with_dict(DESCRIPTOR, DictSubclass(dict(DESCRIPTOR))),
    "unset_extra": lambda: _unset_extra(DESCRIPTOR),
}


@pytest.mark.parametrize("pid", sorted(SHAPE_CASES))
def test_shape_helper(pid: str) -> None:
    """Direct oracle on private ``_shape``: refusal, with the exact positive control."""
    assert PRIVATE._shape(DESCRIPTOR, AdvisorDescriptor, SHAPE_NAMES) == {
        "provider": "openai_compatible",
        "model": "test/model",
        "prompt_version": "v1",
    }
    candidate = SHAPE_CASES[pid]()
    assert PRIVATE._shape(candidate, AdvisorDescriptor, SHAPE_NAMES) is None
    if pid == "spy_key":
        assert SpyKey.calls == 0


def test_construction_succeeds_with_counted_descriptor_call(base: Base) -> None:
    """White-box: the one descriptor call is depth-counted on already initialised state."""
    clock = ManualClock(13.5)
    advisor = DoubleAdvisor(clock)
    seen: list[int] = []

    def observe() -> None:
        frame = inspect.currentframe()
        while frame is not None and frame.f_code is not ColdAdvisorySampler.__init__.__code__:
            frame = frame.f_back
        assert frame is not None
        seen.append(frame.f_locals["self"]._gate.depth)

    advisor.on_descriptor = observe
    rig = admitted(base, clock=clock, advisor=advisor)
    assert seen == [1]
    assert advisor.phases == [RoastPhase.PREHEATING]
    assert priv(rig.sampler)._gate.depth == 0
    assert priv(rig.sampler)._descriptor == ("openai_compatible", "test/model", "v1")


# ------------------------------------------------------------------ context


@pytest.mark.asyncio
async def test_context_projection_fields(base: Base) -> None:
    """The context is a fixed PREHEATING before-charge projection of the tick."""
    rig = make(base, configured_dwell_seconds=MAX)
    result = await run_all(rig)
    assert result.stop is Stop.WINDOW_EXHAUSTED
    context = rig.advisor.contexts[0]
    assert context.model_fields_set == CONTEXT_KEYWORDS
    assert context == AdvisorContext(
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
    assert context.phase is RoastPhase.PREHEATING
    intent = rig.sink.intents[0]
    assert (intent.context_tick, intent.context_tick_monotonic) == (2, 13.0)


def _tick_update(record: schema.ColdTickRecord, **changes: object) -> schema.ColdTickRecord:
    copy = record.model_copy(update=changes)
    schema.validate_record(copy)
    return copy


def _device_update(record: schema.ColdTickRecord, **changes: object) -> schema.ColdTickRecord:
    device = typing.cast(schema.ColdTickDeviceEvidence, record.device)
    return _tick_update(record, device=device.model_copy(update=changes))


def _audio_update(record: schema.ColdTickRecord, **changes: object) -> schema.ColdTickRecord:
    return _tick_update(record, audio=record.audio.model_copy(update=changes))


class SubTick(schema.ColdTickRecord):
    """A tick subclass."""


CONTEXT_REFUSALS: dict[str, Callable[[schema.ColdTickRecord], object]] = {
    "none": lambda t: None,
    "subclass": lambda t: SubTick(**dict(t)),
    "forged": lambda t: t.model_copy(update={"tick": -1}),
    "foreign_run": lambda t: _tick_update(t, run_id="20260926T120000Z-cold-other"),
    "foreign_phase": lambda t: _tick_update(t, phase=ON),
    "foreign_digest": lambda t: _tick_update(t, identity_sha256="e" * 64),
    "foreign_session": lambda t: _tick_update(
        t, session=t.session.model_copy(update={"session_id": S_ON})
    ),
    "tick_after_now": lambda t: _tick_update(t, monotonic_seconds=1450.5),
    "device_none": lambda t: _tick_update(t, device=None),
    "bean_none": lambda t: _device_update(t, bean_temp_c=None),
    "env_none": lambda t: _device_update(t, env_temp_c=None),
    "audio_detected": lambda t: _audio_update(t, status="detected"),
    "detected_utc_set": lambda t: _audio_update(t, detected_at_utc=T0),
    "detected_monotonic_set": lambda t: _audio_update(t, detected_monotonic_seconds=12.0),
    "ticks_raise": lambda t: RuntimeError("ticks fault"),
}


@pytest.mark.parametrize("pid", sorted(CONTEXT_REFUSALS))
@pytest.mark.asyncio
async def test_context_refuses(base: Base, pid: str) -> None:
    """An unbound, absent or detected tick gives no intent and no advisor access."""
    rig = make(base, ticks=TickPort(CONTEXT_REFUSALS[pid](base.tick)))
    result = await run_all(rig)
    assert result == ColdAdvisorySamplerRun(stop=Stop.CONTEXT_UNAVAILABLE, attempts_resolved=0)
    assert rig.sink.calls == []
    assert rig.advisor.getter_reads == 0
    assert rig.ticks.calls == 1


@pytest.mark.asyncio
async def test_context_round_trip_and_measured_max_size(base: Base) -> None:
    """Measured: a 2048 x U+0001 profile keeps the context below the 5a builder bound.

    The builder bound is unreachable via the public spec; this measures, it proves nothing
    about real provider contexts.
    """
    profile = "\u0001" * 2048
    spec = SPEC.model_copy(update={"profile_name": profile})
    rig = make(base, spec=ColdAdvisorySpec.model_validate(dict(spec)), configured_dwell_seconds=MAX)
    await run_all(rig)
    intent = rig.sink.intents[0]
    assert intent.context_profile_name == profile
    assert (
        AdvisorContext.model_validate_json(intent.context_canonical_json)
        == (rig.advisor.contexts[0])
    )
    assert intent.context_byte_length == len(intent.context_canonical_json.encode("utf-8"))
    assert intent.context_byte_length < advisory.MAX_ADVISORY_CONTEXT_BYTES
    assert intent.context_byte_length == MEASURED_MAX_CONTEXT_BYTES


#: Measured canonical context size for the maximum public profile (see the test above).
MEASURED_MAX_CONTEXT_BYTES = 13_681


@pytest.mark.asyncio
async def test_descriptor_is_phase_resolved(base: Base) -> None:
    """The descriptor comes from ``descriptor_for(PREHEATING)``, not the base slug."""
    clock = ManualClock(13.5)
    advisor = real_advisor(clock, by_phase={RoastPhase.PREHEATING: "test/pre"})
    rig = make(base, clock=clock, advisor=advisor)
    await run_all(rig)
    assert rig.sink.intents
    assert {intent.descriptor_model for intent in rig.sink.intents} == {"test/pre"}


def test_ports_strict_assignment(base: Base, tmp_path: Path) -> None:
    """Pyright-checked: the production classes satisfy the consumer-owned ports."""
    advisor: ColdAdvisoryAdvisorPort = real_advisor(ManualClock(0.0))
    evaluator: ColdAdvisoryEvaluatorPort = SafetyPolicy(SafetyLimits())
    writer, _ = open_writer(tmp_path)
    sink: ColdAdvisorySinkPort = writer
    clock: ColdAdvisoryClockPort = MonotonicEngineClock()
    ticks: ColdAdvisoryTickPort = typing.cast(ColdAdvisoryTickPort, TickPort(base.tick))
    assert all(item is not None for item in (advisor, evaluator, sink, clock, ticks))


# ------------------------------------------------------------------ timing


def invocations(rig: Rig) -> list[float | None]:
    """Retained invocation instants, in order."""
    return [record.invocation_monotonic for record in rig.sink.resolutions]


@pytest.mark.asyncio
async def test_first_intent_waits_for_open(base: Base) -> None:
    """The first intent and invocation are at the window open, not before."""
    rig = make(base, configured_dwell_seconds=MAX)
    await run_all(rig)
    assert rig.sink.intents[0].monotonic_seconds == OPEN
    assert invocations(rig) == [OPEN]
    assert rig.advisor.entries == [OPEN]


@pytest.mark.asyncio
async def test_dwell_from_observed_completion(base: Base) -> None:
    """Each due instant is the observed completion plus the dwell."""
    rig = make(base, acts=[Act("latency", seconds=2.5)] * 3)
    task = start(rig)
    await drive(rig.clock, 1470.0)
    assert invocations(rig)[:3] == [1450.0, 1457.5, 1465.0]
    assert rig.advisor.entries[:3] == [1450.0, 1457.5, 1465.0]
    await cancel_and_wait(task)


@pytest.mark.asyncio
async def test_due_exactly_at_close_is_made_then_exhausted(base: Base) -> None:
    """An attempt exactly at close is made; the next due is past close."""
    rig = make(base, acts=[Act("latency", seconds=2.5)] * 41)
    result = await run_all(rig)
    assert result == ColdAdvisorySamplerRun(stop=Stop.WINDOW_EXHAUSTED, attempts_resolved=41)
    assert len(rig.sink.intents) == 41
    assert invocations(rig)[-1] == CLOSE


@pytest.mark.asyncio
async def test_window_closed_before_start(base: Base) -> None:
    """Starting after close makes no attempt."""
    rig = make(base, clock=ManualClock(1750.5))
    result = await run_all(rig)
    assert result.stop is Stop.WINDOW_CLOSED_BEFORE_START
    assert rig.sink.calls == []


@pytest.mark.parametrize("pid", ["bound_inf_sum", "bound_max_finite", "dwell_max"])
@pytest.mark.asyncio
async def test_overflow(base: Base, pid: str) -> None:
    """Overflow never masks the bound: refused before invocation, or exhausted."""
    if pid == "bound_inf_sum":
        clock = ManualClock(MAX, utc=lambda now: T0)
        rig = make(
            base, clock=clock, scheduled_end_monotonic=MAX, configured_call_bound_seconds=MAX
        )
        result = await run_all(rig, until=MAX)
        assert result.stop is Stop.NOT_INVOKED
        assert rig.advisor.entries == []
        assert all(math.isfinite(deadline) for deadline in clock.registered)
    elif pid == "bound_max_finite":
        rig = make(base, configured_call_bound_seconds=MAX, configured_dwell_seconds=MAX)
        result = await run_all(rig)
        assert result.stop is Stop.WINDOW_EXHAUSTED
        assert [record.resolution for record in rig.sink.resolutions] == [Kind.RETURNED_DECISION]
    else:
        rig = make(base, configured_dwell_seconds=MAX)
        result = await run_all(rig)
        assert result == ColdAdvisorySamplerRun(stop=Stop.WINDOW_EXHAUSTED, attempts_resolved=1)
        assert rig.clock.registered == [OPEN]


# ------------------------------------------------------------------ invocation


@pytest.mark.asyncio
async def test_intent_precedes_advisor_entry(base: Base) -> None:
    """At advisor entry the sink already holds that attempt's intent."""
    rig = make(base, configured_dwell_seconds=MAX)
    seen: list[int] = []
    rig.advisor.on_enter = lambda: seen.append(len(rig.sink.intents))
    await run_all(rig)
    assert seen == [1]


INTENT_ERRORS: dict[str, BaseException] = {
    "cold_evidence_error": schema.ColdEvidenceError(
        schema.ColdEvidenceFailure.RECORD_NOT_VALIDATED
    ),
    "cold_store_error": store.ColdEvidenceStoreError(store.ColdEvidenceStoreFailure.WRITE_FAILED),
    "cold_attempt_error": advisory.ColdAdvisoryAttemptError(
        advisory.ColdAdvisoryAttemptFailure.ATTEMPT_ALREADY_OPEN
    ),
    "runtime_error": RuntimeError("sink fault"),
}


@pytest.mark.parametrize("pid", sorted(INTENT_ERRORS))
@pytest.mark.asyncio
async def test_intent_refused_never_invokes(base: Base, pid: str) -> None:
    """A refused intent append starts no provider task and touches no advisor."""
    sink = RecordingSink(refuse=lambda record: INTENT_ERRORS[pid])
    rig = make(base, sink=sink)
    result = await run_all(rig)
    assert result == ColdAdvisorySamplerRun(stop=Stop.INTENT_NOT_APPENDED, attempts_resolved=0)
    await drive(rig.clock, rig.clock.now + 10.0)
    assert rig.advisor.getter_reads == 0
    assert rig.advisor.entries == []
    assert len(sink.calls) == 1
    assert priv(rig.sampler)._open is None


@pytest.mark.asyncio
async def test_real_writer_refusal_never_invokes(base: Base, tmp_path: Path) -> None:
    """The real writer refuses an intent for a phase that is not latest; it stays usable."""
    run = fixture_run(tmp_path)
    writer, _ = open_writer(tmp_path)
    off = header_of(run.documents[OFF], OFF, 1.0)
    on = header_of(run.documents[ON], ON, 2.0)
    writer.append(off)
    writer.append(on)
    rig = make(
        base,
        header=off,
        sink=RecordingSink(inner=writer),
        ticks=TickPort(tick_record(off, run.ticks[OFF][2])),
    )
    result = await run_all(rig)
    assert result.stop is Stop.INTENT_NOT_APPENDED
    assert rig.advisor.entries == []
    writer.append(tick_record(on, run.ticks[ON][0]))


@pytest.mark.parametrize(
    "pid", ["queued_dispatch_delay", "getter_advances", "scheduled_advance_after_getter"]
)
@pytest.mark.asyncio
async def test_invocation_sample_order(base: Base, pid: str) -> None:
    """The invocation instant is sampled in the task, after the getters, with no await."""
    rig = make(base, configured_dwell_seconds=MAX)
    order: list[str] = []
    loop = asyncio.get_running_loop()
    delta = {"queued_dispatch_delay": 0.4, "getter_advances": 0.2}.get(pid, 0.3)

    def advance() -> None:
        order.append("advance")
        rig.clock.advance(delta)

    def enter() -> None:
        order.append("enter")

    rig.advisor.on_enter = enter
    if pid == "queued_dispatch_delay":
        first_intent(rig, lambda: loop.call_soon(advance))
    elif pid == "getter_advances":
        rig.advisor.on_getter = advance
    else:
        rig.advisor.on_getter = lambda: loop.call_soon(advance)
    await run_all(rig)
    invocation = rig.sink.resolutions[0].invocation_monotonic
    assert rig.sink.intents[0].monotonic_seconds == OPEN
    if pid == "scheduled_advance_after_getter":
        assert order == ["enter", "advance"]
        assert invocation == OPEN
    else:
        assert order == ["advance", "enter"]
        assert invocation == OPEN + delta
    assert rig.advisor.entries == [invocation]
    if pid == "queued_dispatch_delay":
        assert typing.cast(float, invocation) - OPEN <= 1.0


@pytest.mark.asyncio
async def test_no_invocation_after_window(base: Base) -> None:
    """An invocation sample past close is refused; settlement records it as not invoked."""
    rig = make(base, clock=ManualClock(CLOSE))
    loop = asyncio.get_running_loop()
    first_intent(rig, lambda: loop.call_soon(rig.clock.advance, 0.4))
    result = await run_all(rig)
    assert rig.sink.intents[0].monotonic_seconds == CLOSE
    assert result.stop is Stop.NOT_INVOKED
    assert rig.advisor.entries == []
    settled = rig.sampler.settle_at_phase_end()
    assert settled.closure is Closure.RECORDED_UNRESOLVED_NOT_INVOKED
    assert rig.sink.resolutions[0].invocation_state is (
        advisory.ColdAdvisoryInvocationState.NOT_INVOKED
    )


@pytest.mark.parametrize("pid", ["getter_raises", "lookup_raises"])
@pytest.mark.asyncio
async def test_port_access_failure(base: Base, pid: str) -> None:
    """A failed getter or lookup is not an invocation."""
    clock = ManualClock(13.5)
    advisor = DoubleAdvisor(
        clock,
        usage_mode="raise" if pid == "getter_raises" else "fresh",
        lookup_raises=pid == "lookup_raises",
    )
    rig = make(base, clock=clock, advisor=advisor)
    result = await run_all(rig)
    assert result.stop is Stop.NOT_INVOKED
    assert advisor.entries == []
    assert rig.sampler.settle_at_phase_end().closure is Closure.RECORDED_UNRESOLVED_NOT_INVOKED


@pytest.mark.asyncio
async def test_floor_shared_across_tasks(base: Base) -> None:
    """The provider's invocation sample shares the run's monotonic floor."""
    rig = make(base)

    def regress() -> None:
        rig.clock.regress_next_to = OPEN - 1.0

    first_intent(rig, regress)
    result = await run_all(rig)
    assert result.stop is Stop.NOT_INVOKED
    assert rig.advisor.entries == []


@pytest.mark.parametrize("pid", ["none", "done_task", "pending_task"])
@pytest.mark.asyncio
async def test_may_start_attempt_helper(pid: str) -> None:
    """Direct oracle on private ``_may_start_attempt``."""
    if pid == "none":
        assert PRIVATE._may_start_attempt(None) is True
        return
    event = asyncio.Event()
    task = asyncio.create_task(event.wait())
    try:
        if pid == "done_task":
            event.set()
            await task
            assert PRIVATE._may_start_attempt(task) is True
        else:
            await asyncio.sleep(0)
            assert PRIVATE._may_start_attempt(task) is False
    finally:
        event.set()
        await task


# ------------------------------------------------------------------ gate and settlement


@pytest.mark.asyncio
async def test_queued_settlement_before_task_step(base: Base) -> None:
    """Settlement queued before the task's first step: no advisor access at all."""
    rig = make(base)
    loop = asyncio.get_running_loop()
    box: list[ColdAdvisorySettlement] = []
    first_intent(rig, lambda: loop.call_soon(settler(rig, box)))
    result = await run_all(rig)
    assert result.stop is Stop.SETTLED
    assert box[0].closure is Closure.RECORDED_UNRESOLVED_NOT_INVOKED
    assert box[0].provider_task is Fact.OUTSTANDING
    assert (rig.advisor.getter_reads, rig.advisor.lookup_reads, rig.advisor.entries) == (0, 0, [])
    await drive_until(lambda: provider_task(rig).done())
    assert provider_cell(rig).refusal is PRIVATE._Refusal.SETTLED_BEFORE_DISPATCH
    assert rig.sampler.settle_at_phase_end() is box[0]


def _snapshot(rig: Rig) -> tuple[int, int, int, int]:
    return (
        rig.clock.monotonic_calls,
        rig.clock.utc_calls,
        rig.advisor.getter_reads,
        rig.advisor.lookup_reads,
    )


REENTRY = [
    "ticks",
    "clock_control",
    "clock_recording",
    "sink_intent",
    "usage_getter",
    "lookup",
    "clock_invocation",
    "clock_invocation_utc",
    "clock_bound_sample",
    "usage_getter_post",
    "clock_completion_inner",
    "clock_completion_outer",
    "evaluator",
    "sink_resolution",
    "sink_abandonment",
    "terminal_evaluator",
    "terminal_sink",
]


@pytest.mark.parametrize("pid", REENTRY)
@pytest.mark.asyncio
async def test_reentrant_settlement(base: Base, pid: str) -> None:
    """Settlement re-entered from a port call is provisional, unstored and port-free."""
    event = asyncio.Event()
    blocking = {"sink_abandonment", "terminal_sink", "clock_bound_sample"}
    acts = [Act("block", event=event)] if pid in blocking else []
    rig = make(base, acts=acts)
    inner: list[ColdAdvisorySettlement] = []
    reenter = settler(rig, inner)
    marks: dict[str, tuple[int, int, int, int]] = {}

    def reenter_and_mark() -> None:
        reenter()
        marks["after"] = _snapshot(rig)

    def on_resolution(record: Record) -> None:
        if type(record) is Resolution:
            reenter()

    loop = asyncio.get_running_loop()
    outer: list[ColdAdvisorySettlement] = []
    if pid == "ticks":
        rig.ticks.on_call = reenter
    elif pid == "clock_control":
        rig.clock.on_monotonic = reenter_and_mark
    elif pid == "clock_recording":

        def arm_recording() -> None:
            rig.clock.on_monotonic = reenter_and_mark

        rig.evaluator.on_call = arm_recording
    elif pid == "sink_intent":
        first_intent(rig, reenter)
    elif pid == "usage_getter":
        rig.advisor.on_getter = reenter_and_mark
    elif pid == "lookup":
        rig.advisor.on_lookup = reenter_and_mark
    elif pid == "clock_invocation":

        def arm_invocation() -> None:
            rig.clock.on_monotonic = reenter_and_mark

        first_intent(rig, arm_invocation)
    elif pid == "clock_invocation_utc":

        def arm_invocation_utc() -> None:
            rig.clock.on_utc = reenter_and_mark

        first_intent(rig, arm_invocation_utc)
    elif pid == "clock_bound_sample":

        def arm_bound_sample() -> None:
            rig.clock.on_monotonic = reenter_and_mark

        rig.advisor.on_enter = arm_bound_sample
    elif pid == "usage_getter_post":

        def arm_post_getter() -> None:
            rig.advisor.on_getter = reenter_and_mark

        rig.advisor.on_enter = arm_post_getter
    elif pid in {"clock_completion_inner", "clock_completion_outer"}:

        def arm_completion() -> None:
            rig.clock.on_monotonic = reenter_and_mark

        rig.advisor.on_enter = arm_completion
    elif pid == "evaluator":
        rig.evaluator.on_call = reenter
    elif pid in {"sink_resolution", "sink_abandonment"}:
        rig.sink.on_append = on_resolution
    elif pid == "terminal_evaluator":

        def settle_with_inner() -> None:
            rig.evaluator.on_call = reenter
            outer.append(rig.sampler.settle_at_phase_end())

        rig.advisor.schedule_on_return = settle_with_inner
    else:
        rig.sink.on_append = on_resolution

    task = start(rig)
    try:
        if pid == "sink_abandonment":
            await drive(rig.clock, OPEN)
            rig.clock.jump(5.0)
            await drive_until(task.done)
        elif pid == "terminal_sink":
            await drive(rig.clock, OPEN)
            rig.clock.advance(2.0)
            outer.append(rig.sampler.settle_at_phase_end())
            event.set()
            await drive_until(task.done)
        else:
            await drive(rig.clock, 1800.0)
        assert task.done()
        result = task.result()
        assert len(inner) == 1
        assert inner[0].closure is Closure.NOT_RECORDED_REENTRANT
        assert priv(rig.sampler)._settlement is not inner[0]
        if pid not in {"terminal_evaluator", "terminal_sink"}:
            assert result.stop is Stop.SETTLED
        if pid != "clock_bound_sample":
            await drive_until(lambda: provider_settled(rig))
        post_run = _snapshot(rig)
        if "after" in marks:
            assert post_run == marks["after"]
        if pid == "clock_completion_inner":
            assert inner[0].provider_task is Fact.OUTSTANDING
            assert inner[0].attempts_resolved == 0
            assert priv(rig.sampler)._settlement is None
            assert provider_cell(rig).discarded_after_settlement is True
            return
        if pid not in {"terminal_evaluator", "terminal_sink"}:
            outer.append(rig.sampler.settle_at_phase_end())
        settled = outer[0]
        assert rig.sampler.settle_at_phase_end() is settled
        unresolved = [
            record
            for record in rig.sink.resolutions
            if record.resolution is Kind.UNRESOLVED_AT_PHASE_END
        ]
        if pid in {"ticks", "clock_control"}:
            assert settled.closure is Closure.NO_OPEN_ATTEMPT
            assert rig.sink.calls == []
        elif pid in {
            "sink_intent",
            "usage_getter",
            "lookup",
            "clock_invocation",
            "clock_invocation_utc",
        }:
            assert settled.closure is Closure.RECORDED_UNRESOLVED_NOT_INVOKED
            assert len(unresolved) == 1
            assert rig.advisor.entries == []
            if pid == "sink_intent":
                assert settled.provider_task is Fact.NONE
                assert priv(rig.sampler)._task is None
                assert (rig.advisor.getter_reads, rig.advisor.lookup_reads) == (0, 0)
            if pid == "usage_getter":
                assert post_run[2:] == (1, 0)
            if pid == "lookup":
                assert post_run[2:] == (1, 1)
            if pid in {"usage_getter", "lookup"}:
                assert post_run[:2] == (2, 2)
        elif pid in {"clock_completion_outer", "usage_getter_post"}:
            assert settled.closure is Closure.NOT_RECORDED_COMPLETION_UNKNOWN
            assert settled.provider_task is Fact.ENDED_WITHOUT_OUTCOME
            assert rig.sink.resolutions == []
            assert provider_cell(rig).discarded_after_settlement is True
        elif pid in {"evaluator", "clock_recording", "terminal_evaluator"}:
            assert settled.closure is Closure.RECORDED_COMPLETED_CALL
            assert [record.resolution for record in rig.sink.resolutions] == [
                Kind.RETURNED_DECISION
            ]
            assert len(rig.evaluator.calls) == (1 if pid == "terminal_evaluator" else 2)
        elif pid in {"sink_resolution", "sink_abandonment"}:
            assert settled.closure is Closure.NO_OPEN_ATTEMPT
            assert len(rig.sink.resolutions) == 1
            assert unresolved == []
            expected = (
                Kind.ABANDONED_AFTER_BOUND if pid == "sink_abandonment" else Kind.RETURNED_DECISION
            )
            assert rig.sink.resolutions[0].resolution is expected
        else:
            assert settled.closure is Closure.RECORDED_UNRESOLVED_INVOKED
            assert len(unresolved) == 1
            assert len(rig.sink.resolutions) == 1
            if pid == "clock_bound_sample":
                assert settled.provider_task is Fact.OUTSTANDING
                assert inner[0].provider_task is Fact.OUTSTANDING
    finally:
        event.set()
        if not task.done():
            await cancel_and_wait(task)
        await idle()
        assert loop is asyncio.get_running_loop()


@pytest.mark.asyncio
async def test_settlement_closes_gate_before_port_calls(base: Base) -> None:
    """White-box: every settlement-owned port call happens with the gate already closed."""
    rig = make(base)
    seen: list[bool] = []
    box: list[ColdAdvisorySettlement] = []

    def settle() -> None:
        def spy(*_: object) -> None:
            seen.append(priv(rig.sampler)._gate.closed)

        rig.clock.on_monotonic = spy
        rig.evaluator.on_call = spy
        rig.sink.on_append = spy
        box.append(rig.sampler.settle_at_phase_end())

    rig.advisor.schedule_on_return = settle
    await run_all(rig)
    assert box[0].closure is Closure.RECORDED_COMPLETED_CALL
    assert seen == [True, True, True]


@pytest.mark.asyncio
async def test_settlement_consumes_done_call(base: Base) -> None:
    """A done, unconsumed call is consumed truthfully by settlement, once."""
    rig = make(base)
    box: list[ColdAdvisorySettlement] = []
    rig.advisor.schedule_on_return = settler(rig, box)
    result = await run_all(rig)
    assert result.stop is Stop.SETTLED
    assert box[0] == ColdAdvisorySettlement(
        closure=Closure.RECORDED_COMPLETED_CALL, provider_task=Fact.COMPLETED, attempts_resolved=1
    )
    resolution = only(rig)
    assert resolution.resolution is Kind.RETURNED_DECISION
    assert (resolution.invocation_monotonic, resolution.resolved_monotonic) == (OPEN, OPEN)
    assert len(rig.evaluator.calls) == 1
    assert len(rig.sink.records) == 2


@pytest.mark.asyncio
async def test_settlement_idempotent(base: Base) -> None:
    """A second settlement returns the stored fact and writes nothing."""
    rig = make(base, configured_dwell_seconds=MAX)
    await run_all(rig)
    first = rig.sampler.settle_at_phase_end()
    count = len(rig.sink.calls)
    second = rig.sampler.settle_at_phase_end()
    assert second is first
    assert len(rig.sink.calls) == count


@pytest.mark.asyncio
async def test_run_after_settlement_writes_nothing(base: Base) -> None:
    """Settling while the run sleeps toward due stops it at the next wake."""
    rig = make(base)
    task = start(rig)
    await idle()
    assert rig.clock.deadlines() == [OPEN]
    assert rig.sampler.settle_at_phase_end().closure is Closure.NO_OPEN_ATTEMPT
    rig.clock.jump(OPEN - rig.clock.now)
    await drive_until(task.done)
    assert task.result().stop is Stop.SETTLED
    assert rig.sink.calls == []


@pytest.mark.parametrize("pid", ["returned", "raised"])
@pytest.mark.asyncio
async def test_late_completion_after_settlement_touches_nothing(base: Base, pid: str) -> None:
    """A late return or raise after settlement makes no clock or advisor access."""
    event = asyncio.Event()
    error = AdvisorProviderError("late") if pid == "raised" else None
    rig = make(base, acts=[Act("block", event=event, error=error)])
    task = start(rig)
    await drive(rig.clock, OPEN)
    settled = rig.sampler.settle_at_phase_end()
    assert settled.closure is Closure.RECORDED_UNRESOLVED_INVOKED
    assert settled.provider_task is Fact.OUTSTANDING
    before, count = _snapshot(rig), len(rig.sink.records)
    event.set()
    await drive_until(lambda: provider_task(rig).done())
    await drive_until(task.done)
    assert _snapshot(rig) == before
    assert provider_cell(rig).discarded_after_settlement is True
    assert provider_cell(rig).outcome is None
    assert len(rig.sink.records) == count
    assert rig.sampler.settle_at_phase_end() is settled
    assert settled.provider_task is Fact.OUTSTANDING
    assert task.result().stop is Stop.SETTLED


@pytest.mark.parametrize(
    "pid",
    [
        "clock_invalid",
        "sink_refuses",
        "completion_unknown",
        "consume_clock_invalid",
        "consume_sink_refuses",
    ],
)
@pytest.mark.asyncio
async def test_settlement_failure_facts(base: Base, pid: str) -> None:
    """Settlement failures are closed facts; nothing raises and no instant is invented."""
    event = asyncio.Event()
    if pid == "completion_unknown":
        rig = make(base)

        def fail_completion() -> None:
            rig.clock.fail_next_monotonic = True

        rig.advisor.on_enter = fail_completion
        result = await run_all(rig)
        assert result.stop is Stop.CLOCK_INVALID
        expected = Closure.NOT_RECORDED_COMPLETION_UNKNOWN
    elif pid.startswith("consume_"):
        sink = RecordingSink(
            refuse=lambda record: (
                RuntimeError("refused")
                if pid == "consume_sink_refuses" and type(record) is Resolution
                else None
            )
        )
        rig = make(base, sink=sink)
        box: list[ColdAdvisorySettlement] = []

        def settle() -> None:
            rig.clock.fail_next_monotonic = pid == "consume_clock_invalid"
            box.append(settle_safely(rig))

        rig.advisor.schedule_on_return = settle
        result = await run_all(rig)
        assert result.stop is Stop.SETTLED
        expected = (
            Closure.NOT_RECORDED_CLOCK_INVALID
            if pid == "consume_clock_invalid"
            else Closure.NOT_RECORDED_SINK_REFUSED
        )
        assert box[0].closure is expected
    else:
        sink = RecordingSink(
            refuse=lambda record: (
                RuntimeError("refused")
                if pid == "sink_refuses" and type(record) is Resolution
                else None
            )
        )
        rig = make(base, acts=[Act("block", event=event)], sink=sink)
        task = start(rig)
        await drive(rig.clock, OPEN)
        rig.clock.fail_next_monotonic = pid == "clock_invalid"
        expected = (
            Closure.NOT_RECORDED_CLOCK_INVALID
            if pid == "clock_invalid"
            else Closure.NOT_RECORDED_SINK_REFUSED
        )
        settled = settle_safely(rig)
        event.set()
        await drive_until(task.done)
        assert settled.closure is expected
    count = len(rig.sink.resolutions)
    assert settle_safely(rig).closure is expected
    assert len(rig.sink.resolutions) == count == 0


@pytest.mark.parametrize(
    "pid", ["outstanding_after_abandon", "ended_without_outcome", "completed", "none"]
)
@pytest.mark.asyncio
async def test_settlement_provider_task_fact(base: Base, pid: str) -> None:
    """The provider-task fact describes the task at the settlement instant."""
    event = asyncio.Event()
    try:
        if pid in {"outstanding_after_abandon", "ended_without_outcome"}:
            mode: typing.Any = "ignore_cancel" if pid == "outstanding_after_abandon" else "block"
            rig = make(base, acts=[Act(mode, event=event)])
            result = await run_all(rig)
            assert result.stop is Stop.STOPPED_AFTER_ABANDONMENT
            expected = (Closure.NO_OPEN_ATTEMPT, Fact.OUTSTANDING)
            if pid == "ended_without_outcome":
                expected = (Closure.NO_OPEN_ATTEMPT, Fact.ENDED_WITHOUT_OUTCOME)
        elif pid == "completed":
            rig = make(base, configured_dwell_seconds=MAX)
            await run_all(rig)
            expected = (Closure.NO_OPEN_ATTEMPT, Fact.COMPLETED)
        else:
            clock = ManualClock(13.5)
            rig = make(base, clock=clock, advisor=DoubleAdvisor(clock, usage_mode="raise"))
            await run_all(rig)
            expected = (Closure.RECORDED_UNRESOLVED_NOT_INVOKED, Fact.NONE)
        settled = rig.sampler.settle_at_phase_end()
        assert (settled.closure, settled.provider_task) == expected
    finally:
        event.set()
        await idle()


@pytest.mark.asyncio
async def test_settlement_unresolved_invoked(base: Base) -> None:
    """An in-flight call settles as unresolved with the actual observation instant."""
    event = asyncio.Event()
    rig = make(base, acts=[Act("block", event=event)])
    task = start(rig)
    await drive(rig.clock, OPEN)
    rig.clock.advance(2.0)
    settled = rig.sampler.settle_at_phase_end()
    assert settled == ColdAdvisorySettlement(
        closure=Closure.RECORDED_UNRESOLVED_INVOKED,
        provider_task=Fact.OUTSTANDING,
        attempts_resolved=1,
    )
    record = only(rig)
    assert record.resolution is Kind.UNRESOLVED_AT_PHASE_END
    assert (record.invocation_monotonic, record.resolved_monotonic) == (OPEN, 1452.0)
    assert record.monotonic_seconds == 1452.0
    event.set()
    await drive_until(task.done)


# ------------------------------------------------------------------ outcome, usage, evaluation


def one_attempt(base: Base, **overrides: typing.Any) -> Rig:
    """A rig whose dwell is so long that exactly one attempt is made."""
    return make(base, configured_dwell_seconds=MAX, **overrides)


REAL_CASES: dict[str, Script] = {
    "decision": lambda index: (Reply(),),
    "unsafe_heat_150": lambda index: (Reply({"target_heat": 150}),),
    "prose_malformed": lambda index: (Prose(),),
    "http_503_provider": lambda index: (Http(),),
    "timeout_unclassified": lambda index: (Timeout(),),
    "rationale_3000": lambda index: (Reply({"rationale": "r" * 3000}),),
}


@pytest.mark.parametrize("pid", sorted(REAL_CASES))
@pytest.mark.asyncio
async def test_outcome_payloads_real_advisor(base: Base, pid: str) -> None:
    """The real advisor and policy: kinds, payloads, evaluation and usage, as retained."""
    clock = ManualClock(13.5)
    rig = one_attempt(
        base,
        clock=clock,
        advisor=real_advisor(clock, REAL_CASES[pid]),
        evaluator=SafetyPolicy(SafetyLimits()),
    )
    result = await run_all(rig)
    assert result == ColdAdvisorySamplerRun(stop=Stop.WINDOW_EXHAUSTED, attempts_resolved=1)
    record = only(rig)
    recorded = advisory.ColdAdvisoryUsageState.RECORDED
    expected_kind = {
        "decision": Kind.RETURNED_DECISION,
        "rationale_3000": Kind.RETURNED_DECISION,
        "unsafe_heat_150": Kind.RETURNED_UNSAFE_OUTPUT,
        "prose_malformed": Kind.RETURNED_MALFORMED_OUTPUT,
        "http_503_provider": Kind.RETURNED_PROVIDER_ERROR,
        "timeout_unclassified": Kind.RAISED_UNCLASSIFIED,
    }[pid]
    assert record.resolution is expected_kind
    if expected_kind is Kind.RETURNED_DECISION:
        assert (record.requested_heat, record.requested_fan, record.should_drop) == (40, 60, False)
        assert record.confidence == 0.5
        assert record.evaluation_verdict is schema.ColdSafetyVerdict.ALLOW
        assert record.evaluation_rule == "all_clear"
        assert record.usage_state is recorded
        if pid == "rationale_3000":
            assert (
                record.rationale_state
                is advisory.ColdAdvisoryRationaleState.NOT_RETAINED_OVER_BOUND
            )
            assert record.rationale is None
        else:
            assert record.rationale == "Hold heat."
    elif expected_kind is Kind.RETURNED_UNSAFE_OUTPUT:
        assert record.usage_state is recorded
        assert record.requested_heat is None
    else:
        assert record.usage_state is None
        assert record.evaluation_state is None


class SubProviderError(AdvisorProviderError):
    """A provider-error subclass."""


CLASSIFICATION: dict[str, tuple[Act, Kind]] = {
    "provider_subclass": (Act("raise", error=SubProviderError("x")), Kind.RAISED_UNCLASSIFIED),
    "non_decision": (Act(value="not a decision"), Kind.RETURNED_MALFORMED_OUTPUT),
    "forged_heat_150": (
        Act(value=UNCHECKED_DECISION.model_construct(**{**REPLY, "target_heat": 150})),
        Kind.RETURNED_MALFORMED_OUTPUT,
    ),
    "bool_heat": (
        Act(value=UNCHECKED_DECISION.model_construct(**{**REPLY, "target_heat": True})),
        Kind.RETURNED_MALFORMED_OUTPUT,
    ),
    "confidence_out_of_range": (
        Act(value=UNCHECKED_DECISION.model_construct(**{**REPLY, "confidence": 1.5})),
        Kind.RETURNED_MALFORMED_OUTPUT,
    ),
    "rationale_not_str": (
        Act(value=UNCHECKED_DECISION.model_construct(**{**REPLY, "rationale": b"x"})),
        Kind.RETURNED_MALFORMED_OUTPUT,
    ),
    "extra_key": (Act(value=forged(DECISION, add_key)), Kind.RETURNED_MALFORMED_OUTPUT),
    "spy_key": (
        Act(value=forged(DECISION, rename("rationale", SpyKey("rationale")))),
        Kind.RETURNED_MALFORMED_OUTPUT,
    ),
}


@pytest.mark.parametrize("pid", sorted(CLASSIFICATION))
@pytest.mark.asyncio
async def test_classification(base: Base, pid: str) -> None:
    """Exact-type classification; a non-admitted decision is malformed with no payload."""
    act, kind = CLASSIFICATION[pid]
    SpyKey.calls = 0
    rig = one_attempt(base, acts=[act])
    await run_all(rig)
    record = only(rig)
    assert record.resolution is kind
    assert record.requested_heat is None
    assert record.usage_state is None
    if pid == "spy_key":
        assert SpyKey.calls == 0


@pytest.mark.parametrize("pid", ["decision", "unsafe", "malformed"])
@pytest.mark.asyncio
async def test_outcome_snapshot_holds_admitted_primitives(base: Base, pid: str) -> None:
    """White-box: the private outcome holds only admitted primitives and a usage reading."""
    act = {
        "decision": Act(),
        "unsafe": Act("raise", error=AdvisorUnsafeOutputError("x")),
        "malformed": Act("raise", error=AdvisorMalformedOutputError("x")),
    }[pid]
    rig = one_attempt(base, acts=[act])
    await run_all(rig)
    outcome = provider_cell(rig).outcome
    assert type(outcome) is PRIVATE._Outcome
    assert outcome.resolved == (OPEN, rig.clock.utc_now_iso())
    if pid == "decision":
        assert outcome[2:7] == (40, 60, False, 0.5, "Hold heat.")
        assert [type(value) for value in outcome[2:7]] == [int, int, bool, float, str]
    else:
        assert outcome[2:7] == (None,) * 5
    if pid == "malformed":
        assert outcome.usage is None
    else:
        assert type(outcome.usage) is advisory.ColdAdvisoryUsageReading
    assert not any(
        isinstance(value, RoastDecision | AdvisorUsage | BaseException) for value in outcome
    )


@pytest.mark.asyncio
async def test_usage_freshness_identity(base: Base) -> None:
    """The same usage object before and after the call is not recorded."""
    clock = ManualClock(13.5)
    rig = one_attempt(base, clock=clock, advisor=DoubleAdvisor(clock, usage_mode="fixed"))
    await run_all(rig)
    record = only(rig)
    assert record.resolution is Kind.RETURNED_DECISION
    assert record.usage_state is advisory.ColdAdvisoryUsageState.NOT_RECORDED


@pytest.mark.parametrize("pid", ["provider_error", "malformed", "unclassified"])
@pytest.mark.asyncio
async def test_usage_only_for_decision_or_unsafe(base: Base, pid: str) -> None:
    """Fresh usage is never retained for a failed call."""
    error = {
        "provider_error": AdvisorProviderError("x"),
        "malformed": AdvisorMalformedOutputError("x"),
        "unclassified": RuntimeError("x"),
    }[pid]
    rig = one_attempt(base, acts=[Act("raise", error=error)])
    result = await run_all(rig)
    assert result.stop is not Stop.RESOLUTION_NOT_APPENDED
    record = only(rig)
    assert record.usage_state is None


class SubUsage(AdvisorUsage):
    """A usage subclass."""


USAGE_CASES: dict[str, Callable[[], object]] = {
    "subclass": lambda: SubUsage(input_tokens=1, output_tokens=1, total_tokens=2),
    "extra_key": lambda: forged(
        AdvisorUsage(input_tokens=1, output_tokens=1, total_tokens=2), add_key
    ),
    "spy_key": lambda: forged(
        AdvisorUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        rename("input_tokens", SpyKey("input_tokens")),
    ),
    "post_getter_raises": lambda: AdvisorUsage(input_tokens=1, output_tokens=1, total_tokens=2),
    "bool_count": lambda: UNCHECKED_USAGE.model_construct(
        input_tokens=True, output_tokens=1, total_tokens=2, reasoning_tokens=None
    ),
    "negative": lambda: UNCHECKED_USAGE.model_construct(
        input_tokens=-1, output_tokens=1, total_tokens=2, reasoning_tokens=None
    ),
    "float_count": lambda: UNCHECKED_USAGE.model_construct(
        input_tokens=1.0, output_tokens=1, total_tokens=2, reasoning_tokens=None
    ),
}


@pytest.mark.parametrize("pid", sorted(USAGE_CASES))
@pytest.mark.asyncio
async def test_usage_admission(base: Base, pid: str) -> None:
    """A hostile or failing post-call usage carrier is not recorded."""
    clock = ManualClock(13.5)
    candidate = USAGE_CASES[pid]()
    advisor = DoubleAdvisor(clock, post_usage=lambda: candidate)
    if pid == "post_getter_raises":
        advisor.getter_errors[2] = RuntimeError("post usage fault")
    rig = one_attempt(base, clock=clock, advisor=advisor)
    SpyKey.calls = 0
    await run_all(rig)
    record = only(rig)
    assert record.resolution is Kind.RETURNED_DECISION
    assert record.usage_state is advisory.ColdAdvisoryUsageState.NOT_RECORDED
    if pid == "spy_key":
        assert SpyKey.calls == 0


def evaluation(**changes: object) -> SafetyEvaluation:
    """A typed evaluation of the default request."""
    values: dict[str, object] = {
        "rule": "r",
        "verdict": SafetyVerdict.REJECT,
        "input_heat": 40,
        "input_fan": 60,
        "adjusted_heat": None,
        "adjusted_fan": None,
        "reason": "x",
    }
    return SafetyEvaluation(**{**values, **changes})  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("verdict", list(SafetyVerdict), ids=lambda v: v.name)
@pytest.mark.asyncio
async def test_evaluation_verdicts(base: Base, verdict: SafetyVerdict) -> None:
    """All six verdicts map by identity to the same-named cold verdict."""
    rig = one_attempt(base, evaluator=DoubleEvaluator(lambda kw: evaluation(verdict=verdict)))
    await run_all(rig)
    record = only(rig)
    assert record.evaluation_verdict is schema.ColdSafetyVerdict[verdict.name]


@pytest.mark.asyncio
async def test_evaluator_receives_request_only(base: Base) -> None:
    """The evaluator receives exactly the request plus ``None``/``None``."""
    rig = one_attempt(base)
    await run_all(rig)
    assert rig.evaluator.calls == [
        {
            "requested_heat": 40,
            "requested_fan": 60,
            "seconds_since_last_command": None,
            "bounds": None,
        }
    ]


class CounterfeitVerdict(enum.Enum):
    """A look-alike verdict enum."""

    ALLOW = "allow"


EVALUATION_REFUSALS: dict[str, Callable[[], object]] = {
    "subclass": lambda: type("SubEvaluation", (SafetyEvaluation,), {})(**dict(evaluation())),
    "extra_key": lambda: forged(evaluation(), add_key),
    "spy_key": lambda: forged(evaluation(), rename("rule", SpyKey("rule"))),
    "cold_verdict": lambda: UNCHECKED_EVALUATION.model_construct(
        **{**dict(evaluation()), "verdict": schema.ColdSafetyVerdict.ALLOW}
    ),
    "string_allow": lambda: UNCHECKED_EVALUATION.model_construct(
        **{**dict(evaluation()), "verdict": "allow"}
    ),
    "counterfeit_member": lambda: UNCHECKED_EVALUATION.model_construct(
        **{**dict(evaluation()), "verdict": CounterfeitVerdict.ALLOW}
    ),
    "input_mismatch": lambda: evaluation(input_heat=41),
    "input_bool": lambda: UNCHECKED_EVALUATION.model_construct(
        **{**dict(evaluation()), "input_heat": True}
    ),
    "adjusted_101": lambda: UNCHECKED_EVALUATION.model_construct(
        **{**dict(evaluation()), "adjusted_heat": 101}
    ),
    "rule_empty": lambda: UNCHECKED_EVALUATION.model_construct(
        **{**dict(evaluation()), "rule": ""}
    ),
    "reason_2049": lambda: evaluation(reason="r" * 2049),
}


@pytest.mark.parametrize("pid", [*sorted(EVALUATION_REFUSALS), "raises"])
@pytest.mark.asyncio
async def test_evaluation_refused(base: Base, pid: str) -> None:
    """A refused evaluation is not recorded; the decision is kept."""
    if pid == "raises":
        evaluator = DoubleEvaluator(error=RuntimeError("evaluator fault"))
    else:
        candidate = EVALUATION_REFUSALS[pid]()
        evaluator = DoubleEvaluator(lambda kw: candidate)
    rig = one_attempt(base, evaluator=evaluator)
    SpyKey.calls = 0
    await run_all(rig)
    record = only(rig)
    assert record.resolution is Kind.RETURNED_DECISION
    assert record.evaluation_state is advisory.ColdAdvisoryEvaluationState.NOT_RECORDED
    assert (record.requested_heat, record.requested_fan) == (40, 60)
    if pid == "spy_key":
        assert SpyKey.calls == 0


# ------------------------------------------------------------------ bound, ownership, waiters


@pytest.mark.asyncio
async def test_bound_abandons_at_exact_deadline(base: Base) -> None:
    """At observed now == invocation + bound: one cancel request, abandonment, stop."""
    event = asyncio.Event()
    rig = make(base, acts=[Act("block", event=event)])
    task = start(rig)
    await drive(rig.clock, OPEN)
    assert rig.clock.deadlines() == [OPEN + 5.0]
    rig.clock.jump(5.0)
    await drive_until(task.done)
    assert task.result() == ColdAdvisorySamplerRun(
        stop=Stop.STOPPED_AFTER_ABANDONMENT, attempts_resolved=1
    )
    record = only(rig)
    assert record.resolution is Kind.ABANDONED_AFTER_BOUND
    assert (record.invocation_monotonic, record.resolved_monotonic) == (OPEN, 1455.0)
    await idle()
    assert rig.advisor.cancels == 1
    event.set()


@pytest.mark.asyncio
async def test_bound_deadline_from_invocation(base: Base) -> None:
    """The deadline is the actual invocation plus the bound, not the intent instant."""
    event = asyncio.Event()
    rig = make(base, acts=[Act("block", event=event)])
    loop = asyncio.get_running_loop()
    first_intent(rig, lambda: loop.call_soon(rig.clock.advance, 0.4))
    task = start(rig)
    await drive(rig.clock, 1460.0)
    await drive_until(task.done)
    record = only(rig)
    assert record.resolution is Kind.ABANDONED_AFTER_BOUND
    assert record.invocation_monotonic == OPEN + 0.4
    assert record.resolved_monotonic == pytest.approx(OPEN + 0.4 + 5.0, abs=1e-9)
    event.set()


@pytest.mark.asyncio
async def test_no_abandonment_before_deadline(base: Base) -> None:
    """An early wake one ulp before the deadline neither abandons nor cancels."""
    event = asyncio.Event()
    rig = make(base, acts=[Act("block", event=event)])
    task = start(rig)
    await drive(rig.clock, OPEN)
    rig.clock.now = math.nextafter(1455.0, 0.0)
    rig.clock.service()
    rig.clock.wake_all()
    await idle()
    assert rig.sink.resolutions == []
    assert rig.advisor.cancels == 0
    assert not task.done()
    await cancel_and_wait(task)
    event.set()


@pytest.mark.parametrize("pid", ["at_deadline", "after_deadline"])
@pytest.mark.asyncio
async def test_completion_priority(base: Base, pid: str) -> None:
    """A completion observed at or after the deadline is a completion, not abandonment."""
    event = asyncio.Event()
    rig = make(base, acts=[Act("block", event=event)])
    task = start(rig)
    await drive(rig.clock, OPEN)
    delay = 5.0 if pid == "at_deadline" else 6.0
    rig.clock.advance(delay)
    event.set()
    await drive_until(lambda: len(rig.sink.resolutions) == 1)
    record = only(rig)
    assert record.resolution is Kind.RETURNED_DECISION
    assert record.resolved_monotonic == OPEN + delay
    assert rig.advisor.cancels == 0
    await idle()
    assert OPEN + 5.0 not in rig.clock.deadlines()
    await cancel_and_wait(task)


@pytest.mark.asyncio
async def test_no_call_after_abandonment(base: Base) -> None:
    """After abandonment the run is done; no further attempt is made in the window."""
    release = asyncio.Event()
    rig = make(base, acts=[Act("ignore_cancel", event=release)])
    task = start(rig)
    try:
        await drive(rig.clock, OPEN)
        rig.clock.jump(5.0)
        await drive_until(task.done)
        assert task.result().stop is Stop.STOPPED_AFTER_ABANDONMENT
        release.set()
        await drive(rig.clock, 1760.0)
        assert rig.advisor.entries == [OPEN]
        assert len(rig.sink.intents) == 1
    finally:
        release.set()
        await finish(task, rig.clock)
        await idle()


@pytest.mark.asyncio
async def test_late_completion_after_abandonment_writes_nothing(base: Base) -> None:
    """A late completion after abandonment is observed but never appended."""
    release = asyncio.Event()
    rig = make(base, acts=[Act("ignore_cancel", event=release)])
    try:
        await run_all(rig)
        count = len(rig.sink.records)
        release.set()
        await drive_until(lambda: provider_task(rig).done())
        assert provider_cell(rig).outcome is not None
        assert len(rig.sink.records) == count
        settled = rig.sampler.settle_at_phase_end()
        assert (settled.closure, settled.provider_task) == (
            Closure.NO_OPEN_ATTEMPT,
            Fact.COMPLETED,
        )
        assert len(rig.sink.records) == count
    finally:
        release.set()
        await idle()


@pytest.mark.asyncio
async def test_abandonment_cancels_once(base: Base) -> None:
    """Exactly one cancellation request reaches the provider."""
    release = asyncio.Event()
    rig = make(base, acts=[Act("ignore_cancel", event=release)])
    try:
        await run_all(rig)
        rig.sampler.settle_at_phase_end()
        release.set()
        await drive(rig.clock, 1800.0)
        assert rig.advisor.cancels == 1
    finally:
        release.set()
        await idle()


@pytest.mark.parametrize("pid", ["invoked_cancelled", "getter_cancelled"])
@pytest.mark.asyncio
async def test_task_end_without_outcome(base: Base, pid: str) -> None:
    """A task ending with neither outcome nor refusal ends the run; no instant is invented."""
    clock = ManualClock(13.5)
    advisor = DoubleAdvisor(clock, [Act("raise_cancelled")])
    if pid == "getter_cancelled":
        advisor.getter_errors[1] = asyncio.CancelledError()
    rig = make(base, clock=clock, advisor=advisor)
    result = await run_all(rig)
    assert result.stop is Stop.PROVIDER_TASK_ENDED_WITHOUT_OUTCOME
    settled = rig.sampler.settle_at_phase_end()
    assert settled.provider_task is Fact.ENDED_WITHOUT_OUTCOME
    if pid == "invoked_cancelled":
        assert settled.closure is Closure.NOT_RECORDED_COMPLETION_UNKNOWN
        assert rig.sink.resolutions == []
    else:
        assert settled.closure is Closure.RECORDED_UNRESOLVED_NOT_INVOKED
        assert advisor.entries == []


def _not_awaitable(seconds: float) -> None:
    """A clock ``sleep`` that returns nothing awaitable (a port-contract breach)."""


CLOCK_FAULTS: dict[str, Callable[[ManualClock], None]] = {
    "due_raises": lambda c: setattr(c, "fail_next_monotonic", True),
    "due_regresses": lambda c: c.monotonic_plan.update({2: 10.0}),
    "due_bad_utc": lambda c: setattr(c, "bad_next_utc", True),
    "invocation_raises": lambda c: c.monotonic_plan.update({3: "raise"}),
    "completion_raises": lambda c: c.monotonic_plan.update({4: "raise"}),
    "bound_sample_raises": lambda c: c.monotonic_plan.update({4: "raise"}),
    "resolution_sample_raises": lambda c: c.monotonic_plan.update({5: "raise"}),
    "sleep_raises": lambda c: setattr(c, "raise_on_next_sleep", True),
    "sleep_cancelled_itself": lambda c: setattr(c, "cancel_self_on_next_sleep", True),
    "sleep_not_awaitable": lambda c: object.__setattr__(c, "sleep", _not_awaitable),
    "deadline_sleep_raises": lambda c: None,
}
CLOCK_FAULT_STOPS: dict[str, ColdAdvisorySamplerStop] = {
    "invocation_raises": Stop.NOT_INVOKED,
}


@pytest.mark.parametrize("pid", sorted(CLOCK_FAULTS))
@pytest.mark.asyncio
async def test_clock_failures(base: Base, pid: str) -> None:
    """Every clock fault is a closed stop with no raw exception and no live sleeper."""
    event = asyncio.Event()
    blocking = {"bound_sample_raises", "deadline_sleep_raises"}
    acts = [Act("block", event=event)] if pid in blocking else []
    rig = make(base, acts=acts)
    CLOCK_FAULTS[pid](rig.clock)
    if pid == "deadline_sleep_raises":

        def arm_deadline_fault() -> None:
            rig.clock.raise_on_next_sleep = True

        rig.advisor.on_enter = arm_deadline_fault
    try:
        result = await run_all(rig)
        assert result.stop is CLOCK_FAULT_STOPS.get(pid, Stop.CLOCK_INVALID)
        assert rig.clock.live_sleepers() == 0
    finally:
        event.set()
        await idle()


@pytest.mark.asyncio
async def test_waiter_cancellation_not_a_failure(base: Base) -> None:
    """A retired deadline waiter's cancellation is not a run failure."""
    rig = make(base, acts=[Act("latency", seconds=2.5)] * 41)
    task = start(rig)
    await drive(rig.clock, 1800.0)
    assert task.done()
    assert not task.cancelled()
    assert task.exception() is None
    assert task.result() == ColdAdvisorySamplerRun(stop=Stop.WINDOW_EXHAUSTED, attempts_resolved=41)


def _pending_waiters() -> list[str]:
    names: list[str] = []
    for item in asyncio.all_tasks():
        coroutine: typing.Any = item.get_coro()
        name = getattr(coroutine, "__qualname__", "")
        if not item.done() and name in {"Event.wait", "ManualClock.sleep"}:
            names.append(name)
    return names


WAITER_CASES = [
    "exhausted",
    "abandoned",
    "settled",
    "clock_invalid",
    "run_cancelled",
    "run_cancelled_during_retirement",
    "run_cancelled_twice_during_retirement",
]


@pytest.mark.parametrize("pid", WAITER_CASES)
@pytest.mark.asyncio
async def test_waiters_retired(base: Base, pid: str) -> None:
    """Every owned waiter is retired by the time the run finishes, on every exit."""
    event = asyncio.Event()
    if pid == "exhausted":
        acts = [Act("latency", seconds=2.5)] * 41
    elif pid in {"abandoned"} or pid.startswith("run_cancelled"):
        acts = [Act("block", event=event)]
    else:
        acts = []
    rig = make(base, acts=acts)
    if pid == "clock_invalid":
        rig.clock.raise_on_next_sleep = True
    task = start(rig)
    try:
        if pid == "settled":
            await idle()
            rig.sampler.settle_at_phase_end()
            rig.clock.wake_all()
        elif pid == "abandoned":
            await drive(rig.clock, OPEN)
            rig.clock.jump(5.0)
        elif pid == "run_cancelled":
            await drive(rig.clock, OPEN)
            task.cancel()
        elif pid == "run_cancelled_during_retirement":
            await drive(rig.clock, OPEN)
            rig.clock.on_sleep_cancelled = task.cancel
            event.set()
        elif pid == "run_cancelled_twice_during_retirement":
            await drive(rig.clock, OPEN)
            loop = asyncio.get_running_loop()
            rig.clock.cancel_lag = 3

            def cancel_twice() -> None:
                task.cancel()
                loop.call_soon(task.cancel)

            rig.clock.on_sleep_cancelled = cancel_twice
            event.set()
        else:
            await drive(rig.clock, 1800.0)
        await drive_until(task.done)
        assert rig.clock.live_sleepers() == 0
        assert [name for name in _pending_waiters() if name == "Event.wait"] == []
        assert _pending_waiters() == []
        if pid.startswith("run_cancelled"):
            assert task.cancelled()
        else:
            assert task.exception() is None
    finally:
        event.set()
        await finish(task, rig.clock)
        await idle()


@pytest.mark.asyncio
async def test_run_cancellation_retains_provider_task(base: Base) -> None:
    """Cancelling the run re-raises after retirement and never cancels the provider."""
    release = asyncio.Event()
    rig = make(base, acts=[Act("ignore_cancel", event=release)])
    task = start(rig)
    try:
        await drive(rig.clock, OPEN)
        task.cancel()
        await asyncio.wait({task})
        assert task.cancelled()
        assert rig.advisor.cancels == 0
        assert len(rig.sink.records) == 1
        assert rig.clock.live_sleepers() == 0
        assert not provider_task(rig).done()
        settled = rig.sampler.settle_at_phase_end()
        assert (settled.closure, settled.provider_task) == (
            Closure.RECORDED_UNRESOLVED_INVOKED,
            Fact.OUTSTANDING,
        )
    finally:
        release.set()
        await idle()


@pytest.mark.asyncio
async def test_ready_waiter_failure(base: Base) -> None:
    """An owned ready waiter cancelled by someone else is a waiter failure (closed stop)."""
    rig = make(base)

    def cancel_ready_waiter() -> None:
        for item in asyncio.all_tasks():
            coroutine: typing.Any = item.get_coro()
            if getattr(coroutine, "__qualname__", "") == "Event.wait":
                item.cancel()

    loop = asyncio.get_running_loop()
    first_intent(rig, lambda: loop.call_soon(cancel_ready_waiter))
    result = await run_all(rig)
    assert result.stop is Stop.CLOCK_INVALID
    assert rig.clock.live_sleepers() == 0
    assert rig.sampler.settle_at_phase_end().closure is Closure.RECORDED_COMPLETED_CALL


@pytest.mark.asyncio
async def test_run_twice_refused(base: Base) -> None:
    """``run`` executes once; a second call is refused."""
    rig = make(base, configured_dwell_seconds=MAX)
    await run_all(rig)
    with pytest.raises(ColdAdvisorySamplerRefusedError):
        await rig.sampler.run()


@pytest.mark.asyncio
async def test_overlap_guard_white_box(base: Base) -> None:
    """White-box: an outstanding provider task blocks the next intent (unreachable otherwise)."""
    rig = make(base)
    event = asyncio.Event()
    pending = asyncio.create_task(event.wait())
    priv(rig.sampler)._task = pending
    try:
        result = await run_all(rig)
        assert result.stop is Stop.STOPPED_AFTER_ABANDONMENT
        assert rig.sink.calls == []
    finally:
        event.set()
        await pending


@pytest.mark.parametrize("pid", ["run", "abandonment"])
@pytest.mark.asyncio
async def test_resolution_append_refused(base: Base, pid: str) -> None:
    """A refused resolution or abandonment append is a closed stop, never success."""
    event = asyncio.Event()
    acts = [Act("block", event=event)] if pid == "abandonment" else []
    sink = RecordingSink(
        refuse=lambda record: RuntimeError("refused") if type(record) is Resolution else None
    )
    rig = make(base, acts=acts, sink=sink)
    try:
        result = await run_all(rig)
        assert result == ColdAdvisorySamplerRun(
            stop=Stop.RESOLUTION_NOT_APPENDED, attempts_resolved=0
        )
        assert sink.resolutions == []
        await idle()
        settled = rig.sampler.settle_at_phase_end()
        expected = (
            Closure.NOT_RECORDED_SINK_REFUSED
            if pid == "run"
            else Closure.NOT_RECORDED_COMPLETION_UNKNOWN
        )
        assert settled.closure is expected
        assert settled.attempts_resolved == 0
    finally:
        event.set()
        await idle()


# ------------------------------------------------------------------ structure (AST oracles)


def _function(name: str, tree: ast.AST = TREE) -> ast.FunctionDef | ast.AsyncFunctionDef:
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    ]
    assert len(found) == 1, name
    return found[0]


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    found: list[ast.Call] = []
    for item in ast.walk(node):
        if isinstance(item, ast.Call):
            func = item.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if called == name:
                found.append(item)
    return found


def _line(node: ast.AST) -> int:
    located: typing.Any = node
    return typing.cast(int, located.lineno)


def _closed_checks(node: ast.AST) -> list[ast.If]:
    return [
        item
        for item in ast.walk(node)
        if isinstance(item, ast.If)
        and any(isinstance(n, ast.Attribute) and n.attr == "closed" for n in ast.walk(item.test))
    ]


def _assigns(node: ast.AST, target: str) -> list[ast.Assign]:
    return [
        item
        for item in ast.walk(node)
        if isinstance(item, ast.Assign) and any(ast.unparse(t) == target for t in item.targets)
    ]


def _structural(pid: str) -> None:
    attempts = _function("_attempts")
    if pid == "session_len_before_encode":
        init = _function("__init__", _class("ColdAdvisorySampler"))
        lengths = [
            node
            for node in ast.walk(init)
            if isinstance(node, ast.Compare) and ast.unparse(node.left) == "len(session)"
        ]
        assert len(lengths) == 1
        length = lengths[0]
        [admit] = _calls(init, "is_admissible_session_id")
        assert (_line(length), length.col_offset) < (_line(admit), admit.col_offset)
    elif pid == "create_task_after_intent_append":
        [append] = [c for c in _calls(attempts, "_append") if ast.unparse(c.args[2]) == "intent"]
        [create] = _calls(attempts, "create_task")
        assert _line(append) < _line(create)
    elif pid == "open_bookkeeping_before_gate_check":
        [append] = [c for c in _calls(attempts, "_append") if ast.unparse(c.args[2]) == "intent"]
        [assign] = _assigns(attempts, "self._open")
        check = min(_line(i) for i in _closed_checks(attempts) if _line(i) > _line(append))
        assert _line(append) < _line(assign) < check
    elif pid == "consumed_before_gate_check":
        for name in ("_consume", "_abandon"):
            function = _function(name)
            [append] = _calls(function, "_append")
            [assign] = _assigns(function, "cell.consumed")
            check = max(_line(i) for i in _closed_checks(function))
            assert _line(append) < _line(assign) < check
    elif pid == "gate_init_before_descriptor_call":
        init = _function("__init__", _class("ColdAdvisorySampler"))
        [assign] = _assigns(init, "self._gate")
        [call] = _calls(init, "descriptor_for")
        assert _line(assign) < _line(call)
    elif pid == "may_start_called_before_intent_build":
        [check] = _calls(attempts, "_may_start_attempt")
        [build] = _calls(attempts, "build_advisory_intent_record")
        assert _line(check) < _line(build)
    elif pid == "settle_has_no_await":
        for name in ("settle_at_phase_end", "_terminal", "_consume", "_task_fact"):
            function = _function(name)
            assert isinstance(function, ast.FunctionDef)
            assert not any(isinstance(n, ast.Await) for n in ast.walk(function))
    elif pid == "retire_awaited_in_finally":
        waiter = _function("_await_waiter")
        finals = [n.finalbody for n in ast.walk(waiter) if isinstance(n, ast.Try) and n.finalbody]
        assert [[ast.unparse(s) for s in body] for body in finals] == [["await _retire(waiter)"]]
    elif pid == "single_cancelled_error_handler_reraises":
        handlers = [
            n
            for n in ast.walk(TREE)
            if isinstance(n, ast.ExceptHandler)
            and n.type is not None
            and "CancelledError" in ast.unparse(n.type)
        ]
        assert len(handlers) == 1
        retire = _function("_retire")
        assert handlers[0] in list(ast.walk(retire))
        raises = [ast.unparse(n) for n in ast.walk(retire) if isinstance(n, ast.Raise)]
        assert raises == ["raise pending"]
        assert ast.unparse(retire.body[-1]) == "if pending is not None:\n    raise pending"
    elif pid == "no_base_exception_handlers":
        types = [
            "" if n.type is None else ast.unparse(n.type)
            for n in ast.walk(TREE)
            if isinstance(n, ast.ExceptHandler)
        ]
        assert "" not in types
        assert not set(types) & {"BaseException", "KeyboardInterrupt", "SystemExit"}
    elif pid == "single_provider_cancel_call":
        cancels = [ast.unparse(c.func) for c in _calls(TREE, "cancel")]
        assert sorted(cancels) == ["task.cancel", "waiter.cancel"]
        assert [ast.unparse(c.func) for c in _calls(_function("_abandon"), "cancel")] == [
            "task.cancel"
        ]
    elif pid == "provider_call_params":
        provider = _function("_provider_call")
        assert [a.arg for a in provider.args.args] == [
            "advisor",
            "context",
            "floor",
            "gate",
            "close",
            "bound",
            "cell",
        ]
        names = {n.id for n in ast.walk(provider) if isinstance(n, ast.Name)}
        assert not names & {"self", "sink", "sampler", "_append"}
    elif pid == "no_public_owner_parameter":
        signatures = {
            name: list(inspect.signature(getattr(ColdAdvisorySampler, name)).parameters)
            for name in ("__init__", "run", "settle_at_phase_end")
        }
        assert signatures == {
            "__init__": [
                "self",
                "header",
                "established_session_id",
                "scheduled_end_monotonic",
                "spec",
                "configured_call_bound_seconds",
                "configured_dwell_seconds",
                "advisor",
                "evaluator",
                "sink",
                "ticks",
                "clock",
            ],
            "run": ["self"],
            "settle_at_phase_end": ["self"],
        }
        assert "_Owner" not in sampler_module.__all__
        assert not [n for n in dir(ColdAdvisorySampler) if "owner" in n.lower()]
    elif pid == "ready_set_in_finally":
        provider = _function("_provider_call")
        [body] = [n for n in provider.body if not isinstance(n, ast.Expr)]
        assert isinstance(body, ast.Try)
        assert [ast.unparse(s) for s in body.finalbody] == ["cell.ready.set()"]
    else:
        retire_args = [ast.unparse(a) for c in _calls(TREE, "_retire") for a in c.args]
        assert retire_args == ["waiter"]
        waiter = _function("_await_waiter")
        task_uses = [
            n
            for n in ast.walk(waiter)
            if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id == "task"
        ]
        assert task_uses == []
        assert not any(
            isinstance(n, ast.Await)
            and "task" in {x.id for x in ast.walk(n) if isinstance(x, ast.Name)}
            for n in ast.walk(waiter)
        )


def _class(name: str) -> ast.ClassDef:
    return next(n for n in ast.walk(TREE) if isinstance(n, ast.ClassDef) and n.name == name)


STRUCTURAL = [
    "session_len_before_encode",
    "create_task_after_intent_append",
    "open_bookkeeping_before_gate_check",
    "consumed_before_gate_check",
    "gate_init_before_descriptor_call",
    "may_start_called_before_intent_build",
    "settle_has_no_await",
    "retire_awaited_in_finally",
    "single_cancelled_error_handler_reraises",
    "no_base_exception_handlers",
    "single_provider_cancel_call",
    "provider_call_params",
    "no_public_owner_parameter",
    "ready_set_in_finally",
    "retire_never_receives_provider_task",
]


@pytest.mark.parametrize("pid", STRUCTURAL)
def test_structural(pid: str) -> None:
    """Structural (AST) oracles over the sampler source; syntax evidence, not behaviour."""
    _structural(pid)


ALLOWED_IMPORTS: dict[str, set[str]] = {
    "roastpilot_agent.advisor": {
        "AdvisorContext",
        "AdvisorDescriptor",
        "AdvisorMalformedOutputError",
        "AdvisorProviderError",
        "AdvisorUnsafeOutputError",
        "AdvisorUsage",
        "RoastDecision",
    },
    "roastpilot_agent.safety": {"SafetyEvaluation", "SafetyVerdict"},
    "roastpilot_agent.models": {"RoastPhase"},
    "roastpilot_agent.cold_characterisation.advisory_window": {
        "MIN_POST_COMPLETION_DWELL_SECONDS",
        "advisory_window_bounds",
    },
    "roastpilot_agent.cold_characterisation.evidence_advisory": {
        "ColdAdvisoryAttemptRecord",
        "ColdAdvisoryResolution",
        "ColdAdvisoryUsageReading",
        "build_advisory_intent_record",
        "build_advisory_resolution_record",
    },
    "roastpilot_agent.cold_characterisation.evidence_schema": {
        "MAX_TEXT_FIELD_BYTES",
        "ColdRunHeader",
        "ColdSafetyEvaluation",
        "ColdSafetyVerdict",
        "ColdTickRecord",
        "validate_record",
    },
    "roastpilot_agent.cold_characterisation.evidence_lifecycle": {
        "is_admissible_monotonic",
        "is_admissible_session_id",
        "is_admissible_utc_instant",
    },
}
FORBIDDEN_MODULES = {
    "time", "os", "logging", "subprocess", "socket", "signal", "sys", "io", "pathlib", "json",
    "config", "live", "controller", "api", "store", "cli", "mcp", "mcp_client", "engine",
    "engine_policy", "two_phase", "host", "identity", "conformance", "advisory_conformance",
    "pydantic_ai", "control_policy",
}  # fmt: skip
FORBIDDEN_NAMES = {
    "call_tool", "finalise_session", "set_heat", "set_fan", "drop_beans", "start_cooling",
    "stop_cooling", "emergency_stop", "set_targets", "build_advisor", "configure_phase", "active",
    "elapsed_monotonic_seconds", "device_state", "first_crack_status", "exit", "_exit", "kill",
}  # fmt: skip


def _relative(module: str | None, level: int) -> str:
    if level == 0:
        return module or ""
    parent = ["roastpilot_agent", "cold_characterisation"][: 2 - (level - 1)]
    return ".".join([*parent, *([module] if module else [])])


def test_import_and_capability_fence() -> None:
    """Structural fence: the import allowlist, forbidden names and the exact ``__all__``."""
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            assert {alias.name for alias in node.names} <= {
                "asyncio",
                "enum",
                "math",
                "typing",
                "pydantic",
            }
        elif isinstance(node, ast.ImportFrom):
            module = _relative(node.module, node.level)
            assert module in ALLOWED_IMPORTS, module
            assert {alias.name for alias in node.names} <= ALLOWED_IMPORTS[module]
            assert not set(module.split(".")) & FORBIDDEN_MODULES
        if isinstance(node, ast.Name):
            assert node.id not in FORBIDDEN_NAMES, node.id
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"print", "str", "repr", "format"}, node.func.id
        if isinstance(node, ast.Attribute):
            assert node.attr not in FORBIDDEN_NAMES | {"format"}, node.attr
        if isinstance(node, ast.keyword):
            assert node.arg not in FORBIDDEN_NAMES
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            assert node.name not in FORBIDDEN_NAMES
            assert not {a.arg for a in node.args.args + node.args.kwonlyargs} & FORBIDDEN_NAMES
        if isinstance(node, ast.JoinedStr):
            assert all(isinstance(value, ast.Constant) for value in node.values)
    assert sampler_module.__all__ == (
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
    assert all(
        issubclass(member, enum.Enum) and not issubclass(member, str)
        for member in (Stop, Closure, Fact)
    )


# ------------------------------------------------------------------ policy-2 integration

DrivePhase = Callable[
    [schema.ColdPhaseKind, schema.ColdRunHeader, store.ColdEvidenceWriter],
    Awaitable[list[Record]],
]


async def write_v3_sampled(
    tmp_path: Path, run: Plan, drive_phase: DrivePhase
) -> tuple[reader.ColdRetainedRunV3, list[Record]]:
    """Mirror ``write_v3``'s order, with the sampler producing each phase's attempts."""
    writer, root = open_writer(tmp_path)
    recorded: list[Record] = []
    sequence = 0
    for phase in run.phases:
        header = header_of(run.documents[phase], phase, run.headers[phase])
        writer.append(header)
        for tick in run.ticks[phase]:
            writer.append(tick_record(header, tick))
        for position, mono in enumerate(run.hosts[phase]):
            writer.append(
                host_record(header, mono, **run.host_overrides.get((phase, position), {}))
            )
        for payload, mono in run.results[phase]:
            writer.append(finalisation(header, payload, mono))
        for owner, factory in run.extras:
            if owner is phase:
                writer.append(factory(header))
        recorded.extend(await drive_phase(phase, header, writer))
        for entry in run.lifecycle:
            if entry.phase is not phase:
                continue
            fields = dict(entry.fields)
            if entry.event is lifecycle.ColdLifecycleEvent.OBSERVATION_WINDOW_ELAPSED:
                fields.setdefault("tick_count", len(run.ticks[phase]))
            writer.append_lifecycle(
                builders.build_lifecycle_record(
                    header=header,
                    sequence=sequence,
                    event=entry.event,
                    event_utc=T0,
                    event_monotonic_seconds=entry.ev,
                    recorded_at_utc=T0,
                    monotonic_seconds=entry.rec,
                    **fields,
                )
            )
            sequence += 1
    digest = writer.seal().manifest_sha256
    retained = reader.read_retained_run_v3(root, run_id=RUN_ID, expected_manifest_sha256=digest)
    return retained, recorded


#: Per phase: the start instant, the session, the scheduled end, and the tick list index.
PHASES: dict[schema.ColdPhaseKind, tuple[float, str, float]] = {
    OFF: (13.5, S_OFF, 1810.0),
    ON: (1833.5, S_ON, 3630.0),
}


@dataclasses.dataclass
class Shape:
    """One failure shape: per-phase scripts and per-phase settlement behaviour."""

    scripts: dict[schema.ColdPhaseKind, Script] = dataclasses.field(
        default_factory=dict[schema.ColdPhaseKind, Script]
    )
    queued_delay: dict[schema.ColdPhaseKind, float] = dataclasses.field(
        default_factory=dict[schema.ColdPhaseKind, float]
    )
    stop_at: dict[schema.ColdPhaseKind, float] = dataclasses.field(
        default_factory=dict[schema.ColdPhaseKind, float]
    )
    fail_settlement_clock: bool = False


def normal(index: int) -> Sequence[Step]:
    """Each call takes 2.5 s on the manual clock, then replies."""
    return (Latency(2.5), Reply())


def driver(run: Plan, clock: ManualClock, shape: Shape, events: list[asyncio.Event]) -> DrivePhase:
    """Run one sampler per phase against the real writer and the real advisor and policy."""

    async def drive_phase(
        phase: schema.ColdPhaseKind, header: schema.ColdRunHeader, writer: store.ColdEvidenceWriter
    ) -> list[Record]:
        start_at, session, scheduled_end = PHASES[phase]
        clock.jump(start_at - clock.now)
        tick = tick_record(header, run.ticks[phase][2])
        sink = RecordingSink(inner=writer)
        sampler = ColdAdvisorySampler(
            header=header,
            established_session_id=session,
            scheduled_end_monotonic=scheduled_end,
            spec=SPEC,
            configured_call_bound_seconds=5.0,
            configured_dwell_seconds=5.0,
            advisor=real_advisor(clock, shape.scripts.get(phase, normal)),
            evaluator=SafetyPolicy(SafetyLimits()),
            sink=sink,
            ticks=typing.cast(ColdAdvisoryTickPort, TickPort(tick)),
            clock=clock,
        )
        delay = shape.queued_delay.get(phase)
        if delay is not None:
            loop = asyncio.get_running_loop()

            def on_append(record: Record) -> None:
                if type(record) is Intent and record.attempt_index == 0:
                    loop.call_soon(clock.advance, delay)

            sink.on_append = on_append
        task = asyncio.create_task(sampler.run())
        stop_at = shape.stop_at.get(phase)
        await drive(clock, scheduled_end if stop_at is None else stop_at - 1.0)
        if stop_at is not None:
            clock.advance(stop_at - clock.now)
            clock.fail_next_monotonic = shape.fail_settlement_clock
        sampler.settle_at_phase_end()
        if not task.done():
            await cancel_and_wait(task)
        if phase is ON:
            for event in events:
                event.set()
        await idle()
        return sink.records

    return drive_phase


@pytest.mark.asyncio
async def test_sampler_records_conform_policy_2_both_phases(tmp_path: Path) -> None:
    """Real advisor, real policy, real writer: 41 attempts per phase conform to policy 2."""
    run = fixture_run(tmp_path)
    clock = ManualClock(13.5)
    v3, recorded = await write_v3_sampled(tmp_path, run, driver(run, clock, Shape(), []))
    assert v3.advisory_attempts == tuple(recorded)
    for phase in (OFF, ON):
        intents = [r for r in recorded if type(r) is Intent and r.phase is phase]
        assert len(intents) == 41
    result = ac.check_advisory_conformance(v3)
    assert result.outcome is ac.ColdAdvisoryConformanceOutcome.ADVISORY_CONFORMANT
    assert result.findings == ()
    assert result.pre_advisory_findings == ()


def _blocked(event: asyncio.Event, at: int) -> Script:
    return lambda index: (Block(event),) if index == at else normal(index)


FAILURE_SHAPES: dict[str, tuple[Callable[[list[asyncio.Event]], Shape], tuple[F, ...]]] = {
    "provider_error": (
        lambda events: Shape(scripts={OFF: lambda i: (Http(),) if i == 10 else normal(i)}),
        (F.ATTEMPT_RETURNED_FAILURE,),
    ),
    "unresolved_invoked": (
        lambda events: Shape(scripts={ON: _blocked(events[0], 40)}, stop_at={ON: 3571.0}),
        (F.ATTEMPT_UNRESOLVED_AT_PHASE_END,),
    ),
    "first_late": (
        lambda events: Shape(queued_delay={OFF: 1.5}),
        (F.FIRST_INVOCATION_NOT_TIMELY,),
    ),
    "open_tail": (
        lambda events: Shape(
            scripts={ON: _blocked(events[0], 40)},
            stop_at={ON: 3571.0},
            fail_settlement_clock=True,
        ),
        (F.ATTEMPTS_OPEN_TAIL,),
    ),
    "abandoned": (
        lambda events: Shape(scripts={OFF: _blocked(events[0], 10)}),
        (F.ATTEMPT_ABANDONED, F.WINDOW_CALL_MISSING),
    ),
}


@pytest.mark.parametrize("pid", sorted(FAILURE_SHAPES))
@pytest.mark.asyncio
async def test_failure_shapes_never_conform(tmp_path: Path, pid: str) -> None:
    """Failure shapes the sampler produces are retained and never conform."""
    run = fixture_run(tmp_path)
    clock = ManualClock(13.5)
    events = [asyncio.Event()]
    factory, findings = FAILURE_SHAPES[pid]
    try:
        v3, recorded = await write_v3_sampled(
            tmp_path, run, driver(run, clock, factory(events), events)
        )
    finally:
        events[0].set()
    assert v3.advisory_attempts == tuple(recorded)
    result = ac.check_advisory_conformance(v3)
    assert result.outcome is ac.ColdAdvisoryConformanceOutcome.NOT_CONFORMANT
    assert result.findings == findings
    assert result.pre_advisory_findings == ()
