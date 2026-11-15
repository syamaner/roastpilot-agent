"""Application shutdown and replacement tests with synthetic child ownership."""

import asyncio
import gc
import warnings
from collections.abc import Generator
from contextlib import contextmanager, suppress
from enum import Enum
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient

import roastpilot_agent.cli as cli
from roastpilot_agent.api import RoastRunConflictError, RoastService, create_app
from roastpilot_agent.config import MCPDeviceConfig
from roastpilot_agent.human_protection import (
    CommandDisposition,
    ContainmentOutcome,
    ContainmentResult,
    ControlAuthority,
    WriteClass,
)
from roastpilot_agent.human_run_ownership import (
    AdmissionCloseReason,
    HumanCallAdmissionFence,
    HumanCallRefusal,
    HumanRunOwnership,
    PendingHumanCall,
    RefusedHumanCall,
)
from roastpilot_agent.mcp_client import MCPConnectionError, MCPServerProcess
from roastpilot_agent.models import HardwareClearAcknowledgementRequest
from roastpilot_agent.store import RoastStore
from tests.test_controller import PROFILE
from tests.test_controller_human_mode import make_human
from tests.test_mcp_client import FakeSession
from tests.test_runner_human_turn import store as shared_store_fixture

store = shared_store_fixture


class _SyntheticProcess(MCPServerProcess):
    """Use the real stop implementation against an attached fake owner."""

    def __init__(self, *, attached: bool = False) -> None:
        super().__init__(session=FakeSession(None) if attached else None)
        self.starts = 0

    async def start(self) -> None:
        self.starts += 1


def test_te21_admission_enums_are_closed_plain_enums() -> None:
    assert PendingHumanCall.__bases__ == (Enum,)
    assert PendingHumanCall.__members__.keys() == {"READ", "CONTAINMENT"}
    assert HumanCallRefusal.__bases__ == (Enum,)
    assert HumanCallRefusal.__members__.keys() == {
        "ADMISSION_CLOSED",
        "CHILD_TEARDOWN_UNCONFIRMED",
        "READ_ALREADY_PENDING",
    }
    assert AdmissionCloseReason.__bases__ == (Enum,)
    assert AdmissionCloseReason.__members__.keys() == {"APPLICATION_TEARDOWN", "DEVICE_RESPAWN"}


@pytest.mark.parametrize(
    ("predicate", "bad"),
    [
        ("child_started", False),
        ("teardown_unconfirmed", True),
        ("lifecycle_blocked", True),
        ("shutdown_started", True),
    ],
)
def test_te9a_respawn_requires_every_predicate(predicate: str, bad: bool) -> None:
    fence = HumanCallAdmissionFence()
    fence.close(AdmissionCloseReason.DEVICE_RESPAWN)
    predicates = {
        "child_started": True,
        "teardown_unconfirmed": False,
        "lifecycle_blocked": False,
        "shutdown_started": False,
    }
    assert not fence.reopen_after_respawn(**{**predicates, predicate: bad})
    assert fence.closed_reason is AdmissionCloseReason.DEVICE_RESPAWN
    assert fence.reopen_after_respawn(**predicates)
    fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
    fence.close(AdmissionCloseReason.DEVICE_RESPAWN)
    assert not fence.reopen_after_respawn(**predicates)
    assert fence.closed_reason is AdmissionCloseReason.APPLICATION_TEARDOWN


@pytest.mark.asyncio
async def test_te1_cli_closes_before_teardown_task_is_scheduled(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RoastService(store)
    process = _SyntheticProcess()
    observed: list[AdmissionCloseReason | None] = []

    async def teardown(current: RoastService, _mcp: MCPServerProcess, _store: RoastStore) -> None:
        observed.append(current._human_admission.closed_reason)  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr(cli, "_teardown_live", teardown)
    await cli._finish_live_teardown(service, process, store, None)  # pyright: ignore[reportPrivateUsage]
    assert observed == [AdmissionCloseReason.APPLICATION_TEARDOWN]


@pytest.mark.asyncio
async def test_te2_direct_teardown_closes_before_active_run_read(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RoastService(store)
    process = _SyntheticProcess()
    original = store.active_run
    seen: list[AdmissionCloseReason | None] = []

    async def inspect_entry() -> object:
        seen.append(service._human_admission.closed_reason)  # pyright: ignore[reportPrivateUsage]
        return await original()

    monkeypatch.setattr(store, "active_run", inspect_entry)
    await cli._teardown_live(service, process, store)  # pyright: ignore[reportPrivateUsage]
    assert seen == [AdmissionCloseReason.APPLICATION_TEARDOWN]


@pytest.mark.asyncio
async def test_te4_cleanup_failure_cannot_reopen_admission(
    store: RoastStore,
) -> None:
    service = RoastService(store)
    h = await make_human()
    service._human_admission = h.fence  # pyright: ignore[reportPrivateUsage]
    observed: list[AdmissionCloseReason | None] = []

    async def failure_before_loop_stop() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            observed.append(h.fence.closed_reason)
            raise RuntimeError("synthetic cleanup failure") from None

    cleanup = asyncio.create_task(failure_before_loop_stop())
    await asyncio.sleep(0)
    service._safety_record_initialization = (None, cleanup)  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="cleanup failure"):
        await service.shutdown()
    assert observed == [AdmissionCloseReason.APPLICATION_TEARDOWN]
    before = h.actuator.reads
    await h.turn(bean=196, t0=True)
    assert h.actuator.reads == before
    assert h.actuator.containments == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["cli", "direct", "respawn"])
@pytest.mark.parametrize("result", ["cancel", "timeout", "malformed", "commanded"])
async def test_te13_stop_preserves_pending_containment_and_publishes_before_unwind(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, entry: str, result: str
) -> None:
    process = _SyntheticProcess(attached=True)
    service = RoastService(store, mcp=process)
    h = await make_human()
    service._human_admission = h.fence  # pyright: ignore[reportPrivateUsage]
    service._human_ownership = h.ownership  # pyright: ignore[reportPrivateUsage]
    request_entered, release_request = asyncio.Event(), asyncio.Event()
    stop_requested, release_owner = asyncio.Event(), asyncio.Event()
    dispositions_at_scope_exit: list[CommandDisposition | None] = []
    original_hold = h.ownership.hold

    @contextmanager
    def observe_scope(kind: PendingHumanCall) -> Generator[None, None, None]:
        with original_hold(kind):
            try:
                yield
            finally:
                assert h.ownership.pending(kind)
                dispositions_at_scope_exit.append(h.controller.snapshot().human_command_disposition)

    monkeypatch.setattr(h.ownership, "hold", observe_scope)

    async def owner() -> None:
        await stop_requested.wait()
        await release_owner.wait()

    child = asyncio.create_task(owner())
    process._owner_task = child  # pyright: ignore[reportPrivateUsage]
    process._stop_requested = stop_requested  # pyright: ignore[reportPrivateUsage]

    async def contain() -> ContainmentResult:
        request_entered.set()
        await release_request.wait()
        if result == "timeout":
            raise TimeoutError("synthetic local timeout after closure")
        if result == "malformed":
            return cast("ContainmentResult", object())
        return ContainmentResult(
            ContainmentOutcome.COMMAND_STATE_RETURNED, evidence_heat_zero_commanded=True
        )

    async def close_and_stop() -> None:
        if entry == "cli":
            await cli._finish_live_teardown(service, process, store, None)  # pyright: ignore[reportPrivateUsage]
        elif entry == "direct":
            await cli._teardown_live(service, process, store)  # pyright: ignore[reportPrivateUsage]
        else:
            with pytest.raises(RoastRunConflictError, match="ownership"):
                await service._respawn_mcp_for_device_config(MCPDeviceConfig())  # pyright: ignore[reportPrivateUsage]

    h.actuator.contain = contain
    request = asyncio.create_task(
        h.controller._contain_human_heat(generation=h.arbiter.generation)  # pyright: ignore[reportPrivateUsage]
    )
    closing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(request_entered.wait(), 1)
        closing = asyncio.create_task(close_and_stop())
        await asyncio.wait_for(stop_requested.wait(), 1)
        assert h.fence.closed_reason is (
            AdmissionCloseReason.DEVICE_RESPAWN
            if entry == "respawn"
            else AdmissionCloseReason.APPLICATION_TEARDOWN
        )
        assert not process.stop_unconfirmed
        assert not request.done()
        assert h.ownership.pending(PendingHumanCall.CONTAINMENT)
        duplicate = await h.controller._contain_human_heat(generation=h.arbiter.generation)  # pyright: ignore[reportPrivateUsage]
        assert duplicate.outcome is ContainmentOutcome.NOT_ATTEMPTED
        assert h.actuator.containments == 1
        if result == "cancel":
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        else:
            release_request.set()
            assert (await request).outcome is {
                "timeout": ContainmentOutcome.UNKNOWN_TRANSPORT,
                "malformed": ContainmentOutcome.UNKNOWN_MALFORMED_RESPONSE,
                "commanded": ContainmentOutcome.COMMAND_STATE_RETURNED,
            }[result]
        assert not h.ownership.pending(PendingHumanCall.CONTAINMENT)
        disposition = (
            CommandDisposition.SETTLED if result == "commanded" else CommandDisposition.UNKNOWN
        )
        assert dispositions_at_scope_exit == [disposition]
        assert h.controller.snapshot().human_command_disposition is disposition
        release_owner.set()
        await closing
        assert process.starts == 0
        assert not process.stop_unconfirmed
        assert h.ownership.lifecycle_blocked
    finally:
        release_owner.set()
        release_request.set()
        await child
        with suppress(asyncio.CancelledError):
            await request
        if closing is not None:
            await closing


@pytest.mark.asyncio
async def test_te15a_unknown_survives_refusal_discard_stop_and_failed_respawn(
    store: RoastStore,
) -> None:
    process = _SyntheticProcess()
    service = RoastService(store, mcp=process)
    h = await make_human()
    service._human_admission = h.fence  # pyright: ignore[reportPrivateUsage]
    service._human_ownership = h.ownership  # pyright: ignore[reportPrivateUsage]
    run = h.controller._human_run  # pyright: ignore[reportPrivateUsage]
    assert run is not None
    run.command = CommandDisposition.UNKNOWN
    assert (
        await h.controller._contain_human_heat(generation=h.arbiter.generation)  # pyright: ignore[reportPrivateUsage]
    ).outcome is ContainmentOutcome.NOT_ATTEMPTED  # pyright: ignore[reportPrivateUsage]
    assert run.command is CommandDisposition.UNKNOWN
    calls: list[str] = []

    async def dispatch() -> None:
        calls.append("write")

    assert await h.controller._run_routine_write(  # pyright: ignore[reportPrivateUsage]
        h.arbiter.generation, dispatch, write_class=WriteClass.HEAT
    ) == (False, None)
    assert calls == []
    assert run.command is CommandDisposition.UNKNOWN

    requested = asyncio.Event()
    process._stop_requested = requested  # pyright: ignore[reportPrivateUsage]
    process._owner_task = asyncio.create_task(requested.wait())  # type: ignore[assignment]
    with pytest.raises(RoastRunConflictError):
        await service._respawn_mcp_for_device_config(MCPDeviceConfig())  # pyright: ignore[reportPrivateUsage]
    assert requested.is_set()
    assert not process.stop_unconfirmed
    assert process.starts == 0
    assert h.fence.closed_reason is AdmissionCloseReason.DEVICE_RESPAWN
    assert run.command is CommandDisposition.UNKNOWN
    assert not h.fence.reopen_after_respawn(
        child_started=True,
        teardown_unconfirmed=False,
        lifecycle_blocked=h.ownership.lifecycle_blocked,
        shutdown_started=False,
    )
    with pytest.raises(RoastRunConflictError):
        await service.start_roast(PROFILE)
    async with AsyncClient(
        transport=ASGITransport(app=create_app(service)), base_url="http://test"
    ) as http:
        assert (
            await http.post("/api/roasts", json=PROFILE.model_dump(mode="json"))
        ).status_code == 409
    assert run.command is CommandDisposition.UNKNOWN
    assert process.starts == 0


@pytest.mark.asyncio
async def test_te15b_reopening_does_not_mutate_existing_closed_ownership(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _SyntheticProcess()
    service = RoastService(store, mcp=process, authority=ControlAuthority.HUMAN)
    ownership = service._human_ownership  # pyright: ignore[reportPrivateUsage]
    run = ownership.claim_run("previous-run")
    run.durably_closed = True
    original = vars(run).copy()

    def unexpected_claim(_: HumanRunOwnership, __: str) -> None:
        pytest.fail("respawn tried to create a run owner")

    monkeypatch.setattr(HumanRunOwnership, "claim_run", unexpected_claim)
    await service._respawn_mcp_for_device_config(MCPDeviceConfig())  # pyright: ignore[reportPrivateUsage]
    assert process.starts == 1
    assert service._human_admission.closed_reason is None  # pyright: ignore[reportPrivateUsage]
    assert ownership._run is run  # pyright: ignore[reportPrivateUsage]
    assert vars(run) == original


@pytest.mark.asyncio
async def test_te3_direct_heat_off_closes_even_when_no_runner(store: RoastStore) -> None:
    service = RoastService(store)
    await service.safe_shutdown_heat_off()
    assert service._human_admission.closed_reason is AdmissionCloseReason.APPLICATION_TEARDOWN  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_te7_respawn_closes_during_healthy_stop_before_reopening(
    store: RoastStore,
) -> None:
    process = _SyntheticProcess()
    service = RoastService(store, mcp=process)
    requested = asyncio.Event()
    allow = asyncio.Event()

    async def owner() -> None:
        await requested.wait()
        await allow.wait()

    process._owner_task = asyncio.create_task(owner())  # pyright: ignore[reportPrivateUsage]
    process._stop_requested = requested  # pyright: ignore[reportPrivateUsage]
    respawn = asyncio.create_task(
        service._respawn_mcp_for_device_config(MCPDeviceConfig())  # pyright: ignore[reportPrivateUsage]
    )
    try:
        await asyncio.wait_for(requested.wait(), 1)
        assert service._human_admission.closed_reason is AdmissionCloseReason.DEVICE_RESPAWN  # pyright: ignore[reportPrivateUsage]
        assert not process.stop_unconfirmed
        assert process.starts == 0
    finally:
        allow.set()
        await respawn
    assert service._human_admission.closed_reason is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_te8_raising_start_keeps_fence_closed_until_a_valid_respawn(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _SyntheticProcess()
    service = RoastService(store, mcp=process)
    original = process.start

    async def fail() -> None:
        raise RuntimeError("synthetic child start failure")

    monkeypatch.setattr(process, "start", fail)
    with pytest.raises(RuntimeError, match="child start failure"):
        await service._respawn_mcp_for_device_config(MCPDeviceConfig())  # pyright: ignore[reportPrivateUsage]
    assert service._human_admission.closed_reason is AdmissionCloseReason.DEVICE_RESPAWN  # pyright: ignore[reportPrivateUsage]
    assert service._spawned_mcp_device is None  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(process, "start", original)
    await service._respawn_mcp_for_device_config(MCPDeviceConfig())  # pyright: ignore[reportPrivateUsage]
    assert service._human_admission.closed_reason is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_te9b_containment_consumed_then_discarded_before_adapter_creation() -> None:
    h = await make_human()
    condition = h.arbiter._condition  # pyright: ignore[reportPrivateUsage]
    async with condition:
        pending = asyncio.create_task(
            h.controller._contain_human_heat(generation=h.arbiter.generation)  # pyright: ignore[reportPrivateUsage]
        )
        await asyncio.sleep(0)
        run = h.controller._human_run  # pyright: ignore[reportPrivateUsage]
        assert run is not None
        from roastpilot_agent.human_protection import ContainmentAttempt

        assert run.containment is ContainmentAttempt.CONSUMED
        h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
    assert (await pending).outcome is ContainmentOutcome.NOT_ATTEMPTED
    assert run.containment is ContainmentAttempt.CONSUMED
    assert run.command is CommandDisposition.SETTLED
    assert not h.ownership.pending(PendingHumanCall.CONTAINMENT)
    assert h.actuator.containments == 0


@pytest.mark.asyncio
async def test_te18_only_human_runner_is_refused_before_reset(
    store: RoastStore,
) -> None:
    from tests.conftest import FakeClock
    from tests.test_runner_human_turn import _RunnerRoaster  # pyright: ignore[reportPrivateUsage]

    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    human = RoastService(store, roaster=actuator, run_loop=False, authority=ControlAuthority.HUMAN)
    automatic = RoastService(store, roaster=actuator, run_loop=False)
    for service in (human, automatic):
        service.close_human_call_admission(AdmissionCloseReason.APPLICATION_TEARDOWN)
    with pytest.raises(RoastRunConflictError, match="HUMAN runner admission is closed"):
        await human._build_runner("human", PROFILE)  # pyright: ignore[reportPrivateUsage]
    assert actuator.resets == 0
    assert await automatic._build_runner("automatic", PROFILE) is not None  # pyright: ignore[reportPrivateUsage]
    assert actuator.resets == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_heat_off", [False, True])
@pytest.mark.parametrize(
    "failed_cleanup", ["_safety_record_initialization", "_stranded_fault_retirement"]
)
async def test_te5_cli_cleanup_failure_keeps_surviving_tick_fenced(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, failed_cleanup: str, failed_heat_off: bool
) -> None:
    process = _SyntheticProcess(attached=True)
    service = RoastService(store, mcp=process)
    h = await make_human()
    service._human_admission = h.fence  # pyright: ignore[reportPrivateUsage]
    service._human_ownership = h.ownership  # pyright: ignore[reportPrivateUsage]
    requested = asyncio.Event()
    release_owner = asyncio.Event()
    run_tick = asyncio.Event()

    async def owner() -> None:
        await requested.wait()
        await release_owner.wait()

    async def old_loop() -> None:
        await run_tick.wait()
        await h.turn(bean=196, t0=True)

    async def broken_cleanup() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("synthetic pre-cancellation teardown failure") from None

    heat_off_calls = 0

    async def broken_heat_off() -> None:
        nonlocal heat_off_calls
        heat_off_calls += 1
        assert h.fence.closed_reason is AdmissionCloseReason.APPLICATION_TEARDOWN
        raise RuntimeError("synthetic heat-off failure")

    if failed_heat_off:
        monkeypatch.setattr(service, "safe_shutdown_heat_off", broken_heat_off)

    child = asyncio.create_task(owner())
    old_tick = asyncio.create_task(old_loop())
    cleanup = asyncio.create_task(broken_cleanup())
    setattr(service, failed_cleanup, (None, cleanup))
    service._loop_task = old_tick  # pyright: ignore[reportPrivateUsage]
    process._owner_task = child  # pyright: ignore[reportPrivateUsage]
    process._stop_requested = requested  # pyright: ignore[reportPrivateUsage]
    await asyncio.sleep(0)
    teardown = asyncio.create_task(
        cli._finish_live_teardown(service, process, store, None)  # pyright: ignore[reportPrivateUsage]
    )
    try:
        await asyncio.wait_for(requested.wait(), 1)
        assert process._session is not None  # pyright: ignore[reportPrivateUsage]
        assert not process.stop_unconfirmed
        assert not old_tick.done()
        run_tick.set()
        await old_tick
        attempt = await h.controller._contain_human_heat(  # pyright: ignore[reportPrivateUsage]
            generation=h.arbiter.generation
        )
        assert attempt.outcome is ContainmentOutcome.NOT_ATTEMPTED
        assert h.actuator.reads == h.actuator.containments == 0
        assert not h.ownership.has_pending_call
    finally:
        run_tick.set()
        release_owner.set()
        await teardown
        await old_tick
    assert h.fence.closed_reason is AdmissionCloseReason.APPLICATION_TEARDOWN
    assert heat_off_calls == int(failed_heat_off)


@pytest.mark.asyncio
async def test_te6_fence_refuses_containment_during_healthy_attached_e6_stop(
    store: RoastStore,
) -> None:
    process = _SyntheticProcess(attached=True)
    service = RoastService(store, mcp=process)
    h = await make_human(teardown_unconfirmed=lambda: process.stop_unconfirmed)
    service._human_admission = h.fence  # pyright: ignore[reportPrivateUsage]
    service._human_ownership = h.ownership  # pyright: ignore[reportPrivateUsage]
    requested, release = asyncio.Event(), asyncio.Event()

    async def owner() -> None:
        await requested.wait()
        await release.wait()

    process._owner_task = asyncio.create_task(owner())  # pyright: ignore[reportPrivateUsage]
    process._stop_requested = requested  # pyright: ignore[reportPrivateUsage]
    stopping = asyncio.create_task(
        service._respawn_mcp_for_device_config(MCPDeviceConfig())  # pyright: ignore[reportPrivateUsage]
    )
    try:
        await asyncio.wait_for(requested.wait(), 1)
        assert process._session is not None  # pyright: ignore[reportPrivateUsage]
        assert not process.stop_unconfirmed
        result = await h.controller._contain_human_heat(  # pyright: ignore[reportPrivateUsage]
            generation=h.arbiter.generation
        )
        assert result.outcome is ContainmentOutcome.NOT_ATTEMPTED
        assert h.actuator.containments == 0
        assert not h.ownership.has_pending_call
    finally:
        release.set()
        with pytest.raises(RoastRunConflictError):
            await stopping
    assert process.starts == 0


@pytest.mark.asyncio
async def test_te10_refusal_does_not_invoke_registration_or_construct_adapter_awaitable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = await make_human()
    h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)

    def forbidden(_: PendingHumanCall) -> None:
        pytest.fail("refused HUMAN call reached pending-scope registration")

    monkeypatch.setattr(h.ownership, "hold", forbidden)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        refusal = await h.controller._read_human_call()  # pyright: ignore[reportPrivateUsage]
        containment = await h.controller._contain_human_heat(  # pyright: ignore[reportPrivateUsage]
            generation=h.arbiter.generation
        )
        gc.collect()
    assert refusal == RefusedHumanCall(PendingHumanCall.READ, HumanCallRefusal.ADMISSION_CLOSED)
    assert containment.outcome is ContainmentOutcome.NOT_ATTEMPTED
    assert h.actuator.reads == h.actuator.containments == 0
    assert not h.ownership.has_pending_call
    assert all("never awaited" not in str(item.message) for item in caught)


@pytest.mark.asyncio
async def test_te4_lifespan_closes_before_waiting_for_cleanup(store: RoastStore) -> None:
    service = RoastService(store)
    app = create_app(service)
    observed: list[AdmissionCloseReason | None] = []

    async def cleanup() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            observed.append(service._human_admission.closed_reason)  # pyright: ignore[reportPrivateUsage]
            raise

    async with app.router.lifespan_context(app):
        task = asyncio.create_task(cleanup())
        service._safety_record_initialization = (None, task)  # type: ignore[assignment]
        await asyncio.sleep(0)
    assert observed == [AdmissionCloseReason.APPLICATION_TEARDOWN]


@pytest.mark.asyncio
async def test_te8_unconfirmed_respawn_and_hardware_clear_keep_admission_closed(
    store: RoastStore,
) -> None:
    class UnconfirmedOnce(_SyntheticProcess):
        stops = 0

        async def stop(self) -> None:
            self.stops += 1
            if self.stops == 1:
                self._fail_closed_teardown("synthetic detached child stop unconfirmed")  # pyright: ignore[reportPrivateUsage]
                return
            await super().stop()

    process = UnconfirmedOnce()
    service = RoastService(store, mcp=process, authority=ControlAuthority.HUMAN)
    with pytest.raises(MCPConnectionError, match="teardown was unconfirmed"):
        await service._respawn_mcp_for_device_config(MCPDeviceConfig())  # pyright: ignore[reportPrivateUsage]
    fence = service._human_admission  # pyright: ignore[reportPrivateUsage]
    assert fence.closed_reason is AdmissionCloseReason.DEVICE_RESPAWN
    incident = process.teardown_incident_id
    assert process.stop_unconfirmed and incident is not None
    assert process.starts == 0
    await service.acknowledge_hardware_clear(
        HardwareClearAcknowledgementRequest(
            hardware_clear=True,
            teardown_incident_id=incident,
            reason="synthetic detached child acknowledgement",
        )
    )
    assert not process.stop_unconfirmed
    assert process.starts == 0
    assert fence.closed_reason is AdmissionCloseReason.DEVICE_RESPAWN
    await service._respawn_mcp_for_device_config(MCPDeviceConfig())  # pyright: ignore[reportPrivateUsage]
    assert process.starts == 1
    assert fence.closed_reason is None
