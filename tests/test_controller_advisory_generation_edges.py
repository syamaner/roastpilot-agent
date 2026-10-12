"""D213/D215 emergency-stop generation fences in post-first-crack advice."""

import asyncio
from typing import cast

import pytest

from roastpilot_agent.advisor import (
    AdvisorContext,
    AdvisorFailureMode,
    AdvisorProviderError,
    FakeAdvisor,
    RoastDecision,
)
from roastpilot_agent.config import SafetyLimits
from roastpilot_agent.controller import AdvisoryTrigger, RoastPhase
from roastpilot_agent.models import AppliedRoasterState, RoastEventKind
from roastpilot_agent.safety import SafetyEvaluation, SafetyVerdict
from tests.conftest import RecordingExecutor
from tests.test_controller import PROFILE, decision, harness_in_development, make_harness, reading


def _executed_commands(events: list[tuple[RoastEventKind, object]]) -> list[str]:
    """Return recorded command names from generic command-executed events."""
    commands: list[str] = []
    for kind, payload in events:
        if kind is not RoastEventKind.COMMAND_EXECUTED or not isinstance(payload, dict):
            continue
        command = cast("dict[str, object]", payload).get("command")
        if isinstance(command, str):
            commands.append(command)
    return commands


@pytest.mark.asyncio
async def test_estop_during_advisor_command_audit_stops_trace_and_lever_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stopped advisory cannot persist its decision or actuate after its command audit."""
    harness = harness_in_development(readings=[reading()], advisor=FakeAdvisor([decision()]))
    entered = asyncio.Event()
    release = asyncio.Event()
    original = harness.sink.persist_evaluation
    evaluations = 0

    async def blocked_evaluation(value: object) -> int | None:
        nonlocal evaluations
        evaluations += 1
        if evaluations == 1:
            entered.set()
            await release.wait()
        return await original(value)  # type: ignore[arg-type]

    monkeypatch.setattr(harness.sink, "persist_evaluation", blocked_evaluation)
    advisory = asyncio.create_task(
        harness.controller._run_advisory(  # pyright: ignore[reportPrivateUsage]
            reading(), AdvisoryTrigger.MANUAL
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    await harness.controller.operator_emergency_stop("advisor command-audit race")
    release.set()
    await asyncio.wait_for(advisory, timeout=1.0)

    assert harness.controller.phase is RoastPhase.FAULTED
    assert len(harness.sink.advisor_decisions) == 0
    assert RoastEventKind.ADVISORY not in harness.events.kinds()
    assert harness.executor.targets == []
    assert harness.executor.heat_writes == []
    assert harness.executor.fan_writes == []


@pytest.mark.asyncio
async def test_estop_during_advisor_decision_persistence_stops_event_and_lever_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted stale decision cannot later emit success or move either lever."""
    harness = harness_in_development(readings=[reading()], advisor=FakeAdvisor([decision()]))
    entered = asyncio.Event()
    release = asyncio.Event()
    original = harness.sink.persist_advisor_decision

    async def blocked_decision(**kwargs: object) -> None:
        entered.set()
        await release.wait()
        await original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(harness.sink, "persist_advisor_decision", blocked_decision)
    advisory = asyncio.create_task(
        harness.controller._run_advisory(  # pyright: ignore[reportPrivateUsage]
            reading(), AdvisoryTrigger.MANUAL
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    await harness.controller.operator_emergency_stop("advisor decision-persistence race")
    release.set()
    await asyncio.wait_for(advisory, timeout=1.0)

    assert harness.controller.phase is RoastPhase.FAULTED
    assert len(harness.sink.advisor_decisions) == 1
    assert RoastEventKind.ADVISORY not in harness.events.kinds()
    assert harness.executor.targets == []
    assert harness.executor.heat_writes == []
    assert harness.executor.fan_writes == []


@pytest.mark.asyncio
async def test_estop_during_advisor_drop_audit_cannot_dispatch_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop during the drop verdict audit prevents the irreversible drop command."""
    harness = harness_in_development(
        readings=[reading()], advisor=FakeAdvisor([decision(drop=True)])
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    original = harness.sink.persist_evaluation
    evaluations = 0

    async def blocked_drop_evaluation(value: object) -> int | None:
        nonlocal evaluations
        evaluations += 1
        if evaluations == 2:
            entered.set()
            await release.wait()
        return await original(value)  # type: ignore[arg-type]

    monkeypatch.setattr(harness.sink, "persist_evaluation", blocked_drop_evaluation)
    advisory = asyncio.create_task(
        harness.controller._run_advisory(  # pyright: ignore[reportPrivateUsage]
            reading(), AdvisoryTrigger.MANUAL
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    await harness.controller.operator_emergency_stop("advisor drop-audit race")
    release.set()
    await asyncio.wait_for(advisory, timeout=1.0)

    assert harness.controller.phase is RoastPhase.FAULTED
    assert harness.executor.commands == []
    assert _executed_commands(harness.events.events) == []


@pytest.mark.asyncio
async def test_estop_during_advisor_drop_coherence_audit_cannot_emit_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stopped incoherent drop cannot add its later rejection trace event."""
    harness = make_harness(readings=[reading()], advisor=FakeAdvisor([decision(drop=True)]))
    controller = harness.controller
    controller.load_profile(PROFILE)
    controller.transition_to(RoastPhase.STARTING)
    controller.transition_to(RoastPhase.PREHEATING)
    controller.transition_to(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    controller._charge_monotonic = harness.clock.now  # pyright: ignore[reportPrivateUsage]
    harness.clock.advance(300.0)
    controller.transition_to(RoastPhase.DEVELOPMENT)
    harness.clock.advance(300.0 * 0.05 / 0.95)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = harness.sink.persist_evaluation
    evaluations = 0

    async def blocked_drop_coherence(value: object) -> int | None:
        nonlocal evaluations
        evaluations += 1
        if evaluations == 2:
            assert isinstance(value, SafetyEvaluation)
            assert value.rule == "advisor_drop_coherence"
            assert value.verdict is SafetyVerdict.REJECT
            entered.set()
            await release.wait()
        return await original(value)  # type: ignore[arg-type]

    monkeypatch.setattr(harness.sink, "persist_evaluation", blocked_drop_coherence)
    advisory = asyncio.create_task(
        controller._run_advisory(  # pyright: ignore[reportPrivateUsage]
            reading(), AdvisoryTrigger.MANUAL
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    await controller.operator_emergency_stop("advisor drop-coherence race")
    release.set()
    await asyncio.wait_for(advisory, timeout=1.0)

    assert controller.phase is RoastPhase.FAULTED
    assert not any(
        kind is RoastEventKind.ADVISORY
        and isinstance(payload, dict)
        and "drop_rejected" in cast("dict[str, object]", payload)
        for kind, payload in harness.events.events
    )
    assert harness.executor.commands == []


@pytest.mark.asyncio
async def test_estop_during_advisor_drop_write_cannot_publish_cooling() -> None:
    """A drop completing after admission cannot emit success or enter cooling."""

    class BlockingDropExecutor(RecordingExecutor):
        """Park the already-admitted drop so the stop can revoke its generation."""

        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def drop_beans(self) -> AppliedRoasterState | None:
            self.entered.set()
            await self.release.wait()
            return await super().drop_beans()

    executor = BlockingDropExecutor()
    harness = harness_in_development(
        readings=[reading()], advisor=FakeAdvisor([decision(drop=True)])
    )
    harness.controller._executor = executor  # pyright: ignore[reportPrivateUsage]
    advisory = asyncio.create_task(
        harness.controller._run_advisory(  # pyright: ignore[reportPrivateUsage]
            reading(), AdvisoryTrigger.MANUAL
        )
    )
    await asyncio.wait_for(executor.entered.wait(), timeout=1.0)

    generation = harness.controller.command_generation
    assert harness.controller.admit_emergency_stop() == generation + 1
    stop = asyncio.create_task(
        harness.controller.operator_emergency_stop("advisor drop-write race", already_admitted=True)
    )
    executor.release.set()
    await asyncio.wait_for(advisory, timeout=1.0)
    await asyncio.wait_for(stop, timeout=1.0)

    assert harness.controller.phase is RoastPhase.FAULTED
    assert executor.commands == ["drop_beans"]
    assert _executed_commands(harness.events.events) == []


@pytest.mark.asyncio
async def test_estop_during_advisor_target_write_cannot_publish_lever_success() -> None:
    """A stale completed target call cannot publish an advisor lever success event."""

    class BlockingTargetExecutor(RecordingExecutor):
        """Park the already-admitted target update for a deterministic interleaving."""

        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
            self.entered.set()
            await self.release.wait()
            await super().set_targets(heat_percent=heat_percent, fan_percent=fan_percent)

    executor = BlockingTargetExecutor()
    harness = harness_in_development(readings=[reading()], advisor=FakeAdvisor([decision()]))
    harness.controller._executor = executor  # pyright: ignore[reportPrivateUsage]
    advisory = asyncio.create_task(
        harness.controller._run_advisory(  # pyright: ignore[reportPrivateUsage]
            reading(), AdvisoryTrigger.MANUAL
        )
    )
    await asyncio.wait_for(executor.entered.wait(), timeout=1.0)

    generation = harness.controller.command_generation
    assert harness.controller.admit_emergency_stop() == generation + 1
    stop = asyncio.create_task(
        harness.controller.operator_emergency_stop(
            "advisor target-write race", already_admitted=True
        )
    )
    executor.release.set()
    await asyncio.wait_for(advisory, timeout=1.0)
    await asyncio.wait_for(stop, timeout=1.0)

    assert harness.controller.phase is RoastPhase.FAULTED
    assert executor.targets == [(65, 50)]
    command_events = [
        kind for kind in harness.events.kinds() if kind is RoastEventKind.COMMAND_EXECUTED
    ]
    assert command_events == []


@pytest.mark.asyncio
async def test_estop_during_failed_advisor_audit_stops_failure_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalidated provider failure cannot create a later failure decision trace."""
    harness = harness_in_development(
        readings=[reading()], advisor=FakeAdvisor([AdvisorFailureMode.PROVIDER_ERROR])
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    original = harness.sink.persist_evaluation
    evaluations = 0

    async def blocked_evaluation(value: object) -> int | None:
        nonlocal evaluations
        evaluations += 1
        if evaluations == 1:
            entered.set()
            await release.wait()
        return await original(value)  # type: ignore[arg-type]

    monkeypatch.setattr(harness.sink, "persist_evaluation", blocked_evaluation)
    advisory = asyncio.create_task(
        harness.controller._run_advisory(  # pyright: ignore[reportPrivateUsage]
            reading(), AdvisoryTrigger.MANUAL
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    await harness.controller.operator_emergency_stop("advisor failure-audit race")
    release.set()
    await asyncio.wait_for(advisory, timeout=1.0)

    assert harness.controller.phase is RoastPhase.FAULTED
    assert harness.sink.advisor_decisions == []
    assert RoastEventKind.ADVISORY not in harness.events.kinds()


@pytest.mark.asyncio
async def test_estop_during_failed_advisor_decision_persistence_stops_failure_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale provider failure cannot emit after its trace row has returned."""
    harness = harness_in_development(
        readings=[reading()], advisor=FakeAdvisor([AdvisorFailureMode.PROVIDER_ERROR])
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    original = harness.sink.persist_advisor_decision

    async def blocked_decision(**kwargs: object) -> None:
        entered.set()
        await release.wait()
        await original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(harness.sink, "persist_advisor_decision", blocked_decision)
    advisory = asyncio.create_task(
        harness.controller._run_advisory(  # pyright: ignore[reportPrivateUsage]
            reading(), AdvisoryTrigger.MANUAL
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    await harness.controller.operator_emergency_stop("advisor failure-decision race")
    release.set()
    await asyncio.wait_for(advisory, timeout=1.0)

    assert harness.controller.phase is RoastPhase.FAULTED
    assert len(harness.sink.advisor_decisions) == 1
    assert RoastEventKind.ADVISORY not in harness.events.kinds()


@pytest.mark.asyncio
async def test_estop_during_provider_failure_skips_failure_accounting() -> None:
    """A provider failure returning after admission cannot consume the outage budget."""

    class BlockingFailureAdvisor(FakeAdvisor):
        """Wait until the test admits a stop, then fail at the provider boundary."""

        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
            del context
            self.entered.set()
            await self.release.wait()
            raise AdvisorProviderError("provider failed after emergency-stop admission")

    advisor = BlockingFailureAdvisor()
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    controller = harness.controller
    threshold = SafetyLimits().max_consecutive_advisor_failures
    controller._consecutive_advisor_failures = threshold - 1  # pyright: ignore[reportPrivateUsage]
    controller.request_advisory()
    tick = asyncio.create_task(controller.tick())
    await asyncio.wait_for(advisor.entered.wait(), timeout=1.0)

    generation = controller.command_generation
    assert controller.admit_emergency_stop() == generation + 1
    await controller.operator_emergency_stop(
        "provider failure after admission", already_admitted=True
    )
    advisor.release.set()
    await asyncio.wait_for(tick, timeout=1.0)

    assert controller.phase is RoastPhase.FAULTED
    assert controller._consecutive_advisor_failures == threshold - 1  # pyright: ignore[reportPrivateUsage]
    assert not any(
        evaluation.rule.startswith("advisor_") for evaluation in harness.sink.evaluations
    )
    assert harness.sink.advisor_decisions == []
    assert RoastEventKind.ADVISORY not in harness.events.kinds()
    assert harness.executor.targets == []
    assert harness.executor.heat_writes == []
    assert harness.executor.fan_writes == []
