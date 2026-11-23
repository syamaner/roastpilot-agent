"""Controller wiring of the D239 predictive finish law (#1131, slice 3c)."""

from __future__ import annotations

import pytest

from roastpilot_agent.advisor import FakeAdvisor
from roastpilot_agent.config import (
    ControllerConfig,
    FcAnchorConfig,
    FinishControlConfig,
    PostFirstCrackControl,
)
from roastpilot_agent.fc_prediction import TwoNodeParameters
from roastpilot_agent.models import (
    RoastEventKind,
    RoastPhase,
    RoastProfile,
    RoastTelemetry,
)
from roastpilot_agent.safety import SafetyVerdict
from tests.sim_two_node_plant import run_closed_loop
from tests.test_controller import NORMAL_PATH, Harness, decision, make_harness

PROFILE = RoastProfile(
    name="winter-finish",
    bean_origin="Brazil",
    bean_weight_grams=250.0,
    initial_heat_percent=100,
    initial_fan_percent=30,
    target_drop_temp_c=195.0,
    target_development_percent=14.0,
)
ON = ControllerConfig(finish_control=FinishControlConfig(enabled=True))
OFF = ControllerConfig()


def _reading(bean: float, *, ror: float | None = 8.0, env: float = 215.0) -> RoastTelemetry:
    return RoastTelemetry.model_validate(
        {
            "bean_temp_c": bean,
            "env_temp_c": env,
            "bean_ror_c_per_min": ror,
            "ambient_temp_c": 19.5,
            "ambient_age_seconds": 1.0,
        }
    )


def _charged(config: ControllerConfig = ON, advisor: FakeAdvisor | None = None) -> Harness:
    harness = make_harness(readings=[_reading(150.0)], config=config, advisor=advisor)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:3]:  # …→ ROASTING_PRE_FIRST_CRACK
        harness.controller.transition_to(step)
    harness.controller._charge_monotonic = harness.clock.now - 500.0  # pyright: ignore[reportPrivateUsage]
    harness.controller._current_heat = 100  # pyright: ignore[reportPrivateUsage]
    harness.controller._current_fan = 30  # pyright: ignore[reportPrivateUsage]
    return harness


def _anchor(harness: Harness, fc_charge_s: float = 600.0) -> None:
    tracker = harness.controller._fc_anchor_tracker  # pyright: ignore[reportPrivateUsage]
    tracker.update(charge_elapsed_seconds=fc_charge_s - 60.0, bean_temp_c=165.0, eta_seconds=60.0)
    assert tracker.anchor.fc_charge_seconds == fc_charge_s


async def _tick(harness: Harness, reading: RoastTelemetry, seconds: float = 5.0) -> None:
    harness.reader.readings = [reading]
    await harness.controller.tick()
    harness.clock.advance(seconds)


# --- closed loop on the 3a plant ---------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_is_byte_identical_to_todays_controller() -> None:
    async def trace(config: ControllerConfig) -> list[tuple[object, ...]]:
        r = await run_closed_loop(
            config=config, profile=PROFILE, plant_parameters=TwoNodeParameters(), ambient_c=19.5
        )
        return [tuple(t) for t in r.ticks]

    off = ControllerConfig(finish_control=FinishControlConfig(enabled=False))
    assert await trace(off) == await trace(ControllerConfig())


@pytest.mark.asyncio
async def test_finish_law_drops_at_target_nearer_the_development_target() -> None:
    """Matched plant, crack at 182 °C, 15 s detector lag: today's controller
    finishes at about 18 % development; the finish law lands nearer 14 %."""
    kwargs = {
        "profile": PROFILE,
        "plant_parameters": TwoNodeParameters(),
        "ambient_c": 19.5,
        "fc_detection_lag_s": 15,
    }
    law = await run_closed_loop(config=ON, **kwargs)  # type: ignore[arg-type]
    today = await run_closed_loop(config=ControllerConfig(), **kwargs)  # type: ignore[arg-type]
    assert (law.end_reason, law.drop_reason) == ("drop", "finish_target")
    assert law.drop_bean_c is not None and 195.0 <= law.drop_bean_c < 196.0
    law_dev, today_dev = (
        law.development_percent_at_bean(195.0),
        today.development_percent_at_bean(195.0),
    )
    assert law_dev is not None and today_dev is not None
    assert abs(law_dev - 14.0) < abs(today_dev - 14.0)


# --- engagement and bypasses ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_before_the_anchor_heat_holds_flat_with_no_trim(
    enabled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner, 23 Nov: with the law enabled the late-Maillard trim never arms.
    The anchor opens late here (175 °C) and the FC ETA sits inside the trim
    window, so today's controller trims; the law-enabled one holds 100 %."""
    late_anchor = ControllerConfig(
        finish_control=FinishControlConfig(enabled=enabled),
        fc_anchor=FcAnchorConfig(open_min_bean_temp_c=175.0),
    )
    harness = _charged(late_anchor)

    def eta() -> float:
        return 30.0

    monkeypatch.setattr(harness.controller, "_first_crack_eta_seconds", eta)
    for bean in (160.0, 165.0, 170.0):
        await _tick(harness, _reading(bean, ror=20.0))
    assert not harness.controller._finish_owns_levers()  # pyright: ignore[reportPrivateUsage]
    trimmed = harness.controller._trim_latched  # pyright: ignore[reportPrivateUsage]
    heats = {h for h, _ in harness.executor.targets}
    if enabled:
        assert not trimmed and heats <= {100}
    else:  # the control: the same state does trim today
        assert trimmed and any(h < 100 for h in heats)


@pytest.mark.asyncio
async def test_law_moves_heat_one_step_through_safety_and_never_the_fan() -> None:
    harness = _charged()
    _anchor(harness)
    await _tick(harness, _reading(185.0, ror=12.0))  # running hot: cut
    assert harness.executor.targets[-1] == (95, 30)
    rules = [e.rule for e in harness.sink.evaluations]
    assert "command_phase_validity" in rules
    last = harness.controller._finish_last_decision  # pyright: ignore[reportPrivateUsage]
    assert last is not None and last.heat_percent == 95


@pytest.mark.asyncio
async def test_law_decides_only_at_its_cadence() -> None:
    harness = _charged()
    _anchor(harness)
    await _tick(harness, _reading(185.0, ror=12.0), seconds=1.0)
    writes = len(harness.executor.targets)
    await _tick(harness, _reading(185.5, ror=12.0), seconds=1.0)
    assert len(harness.executor.targets) == writes  # 1 s later: no new decision


@pytest.mark.asyncio
async def test_advisor_levers_are_locked_while_the_law_owns_heat() -> None:
    advisor = FakeAdvisor([decision(heat=0, fan=90)])
    harness = _charged(advisor=advisor)
    _anchor(harness)
    harness.controller.transition_to(RoastPhase.DEVELOPMENT)
    harness.controller.request_advisory()
    await _tick(harness, _reading(190.0, ror=6.0))
    assert advisor.contexts  # the advisor was consulted
    assert (0, 90) not in harness.executor.targets
    assert all(fan == 30 for _, fan in harness.executor.targets)


@pytest.mark.asyncio
async def test_recovery_disables_the_law_for_the_rest_of_the_run() -> None:
    harness = _charged()
    _anchor(harness)
    assert harness.controller._finish_owns_levers()  # pyright: ignore[reportPrivateUsage]
    harness.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    harness.controller.operator_resume(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    assert not harness.controller._finish_owns_levers()  # pyright: ignore[reportPrivateUsage]
    for step in (
        RoastPhase.DEVELOPMENT,
        RoastPhase.COOLING,
        RoastPhase.COMPLETE,
        RoastPhase.IDLE,
        RoastPhase.STARTING,
        RoastPhase.PREHEATING,
        RoastPhase.ROASTING_PRE_FIRST_CRACK,
    ):
        harness.controller.transition_to(step)
    _anchor(harness)
    rearmed = harness.controller._finish_owns_levers()  # pyright: ignore[reportPrivateUsage]
    assert rearmed  # a new run re-arms it


def test_a_target_at_or_above_the_guard_never_engages() -> None:
    harness = _charged()
    harness.controller.load_profile(PROFILE.model_copy(update={"target_drop_temp_c": 196.0}))
    _anchor(harness)
    assert not harness.controller._finish_owns_levers()  # pyright: ignore[reportPrivateUsage]


def test_human_route_never_engages() -> None:
    harness = _charged()
    _anchor(harness)
    harness.controller._human_run = object()  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    assert not harness.controller._finish_owns_levers()  # pyright: ignore[reportPrivateUsage]


# --- protection and raise blocks ---------------------------------------------------------


@pytest.mark.asyncio
async def test_guard_is_armed_before_first_crack_whenever_the_law_is_enabled() -> None:
    """Safety review: the law runs pre-FC, so the 196 °C guard must be armed
    there in the default first-crack source too."""
    harness = _charged()
    await _tick(harness, _reading(196.5, ror=8.0))
    assert "drop_beans" in harness.executor.commands
    executed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_EXECUTED]
    assert {"command": "drop_beans", "source": "policy", "reason": "ceiling_guard"} in executed


@pytest.mark.asyncio
async def test_target_drop_fires_pre_fc_with_a_low_development_alert() -> None:
    harness = _charged()
    _anchor(harness)
    await _tick(harness, _reading(195.2, ror=8.0))
    executed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_EXECUTED]
    assert {"command": "drop_beans", "source": "policy", "reason": "finish_target"} in executed
    alerts = [p for k, p in harness.events.events if k is RoastEventKind.SAFETY_ALERT]
    assert alerts and alerts[0]["alert"] == "low_development"  # type: ignore[index]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reading", "elapsed_s", "why"),
    [
        (_reading(180.0, ror=0.3), 720.0, "stall"),
        (_reading(180.0, ror=6.0, env=232.0), 720.0, "env_cap"),
        (_reading(180.0, ror=6.0), 900.0, "overrun"),
    ],
)
async def test_raises_are_blocked(reading: RoastTelemetry, elapsed_s: float, why: str) -> None:
    """A late, cold finish would raise heat; each block holds it instead."""
    harness = _charged()
    harness.controller._current_heat = 60  # pyright: ignore[reportPrivateUsage]
    harness.controller._charge_monotonic = harness.clock.now - elapsed_s  # pyright: ignore[reportPrivateUsage]
    _anchor(harness, fc_charge_s=600.0)  # planned finish 698 s
    await _tick(harness, reading)
    assert all(h <= 60 for h, _ in harness.executor.targets), why
    if why == "overrun":
        alerts = [p for k, p in harness.events.events if k is RoastEventKind.SAFETY_ALERT]
        assert [a["alert"] for a in alerts] == ["finish_overrun"]  # type: ignore[index]


@pytest.mark.asyncio
async def test_unblocked_late_cold_finish_does_raise_heat() -> None:
    """Control for the blocks above: the same late, cold state raises heat."""
    harness = _charged()
    harness.controller._current_heat = 60  # pyright: ignore[reportPrivateUsage]
    harness.controller._charge_monotonic = harness.clock.now - 720.0  # pyright: ignore[reportPrivateUsage]
    _anchor(harness, fc_charge_s=600.0)
    await _tick(harness, _reading(180.0, ror=6.0))
    assert harness.executor.targets and harness.executor.targets[-1][0] == 65


# --- coverage of the remaining guards -------------------------------------------------


@pytest.mark.asyncio
async def test_advisor_lock_holds_with_the_post_fc_loop_off() -> None:
    """With D88's loop off, the loop branch does not hold the advisor; the finish
    lock must, so the advisor's heat 0 / fan 90 is never written."""
    config = ControllerConfig(
        finish_control=FinishControlConfig(enabled=True),
        post_first_crack_control=PostFirstCrackControl(enabled=False),
    )
    advisor = FakeAdvisor([decision(heat=0, fan=90)])
    harness = _charged(config, advisor=advisor)
    _anchor(harness)
    harness.controller.transition_to(RoastPhase.DEVELOPMENT)
    # Hold the finish law off its cadence this tick, so the advisor's command is
    # not merely rate-limited behind a finish write: it must be ALLOWed by safety
    # and still never actuated.
    harness.controller._finish_last_decision_monotonic = harness.clock.now  # pyright: ignore[reportPrivateUsage]
    harness.controller.request_advisory()
    await _tick(harness, _reading(190.0, ror=6.0))
    assert advisor.contexts
    allowed = [
        e
        for e in harness.sink.evaluations
        if e.verdict is SafetyVerdict.ALLOW and e.adjusted_heat == 0 and e.adjusted_fan == 90
    ]
    assert allowed
    assert harness.executor.targets == []


@pytest.mark.asyncio
async def test_finish_write_skips_when_the_profile_target_yields_no_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roastpilot_agent.controller as controller_module

    harness = _charged()
    _anchor(harness)

    def no_plan(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(controller_module, "planned_finish_charge_seconds", no_plan)
    await harness.controller._apply_finish_control(  # pyright: ignore[reportPrivateUsage]
        _reading(185.0, ror=12.0), generation=harness.controller.command_generation
    )
    assert harness.executor.targets == []


@pytest.mark.asyncio
@pytest.mark.parametrize("owns", [True, False])
async def test_an_estop_during_the_lever_step_ends_the_tick(
    owns: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale generation after the lever step returns before the guard or drop."""
    harness = _charged(ON if owns else OFF)
    _anchor(harness)
    name = "_apply_finish_control" if owns else "_apply_deterministic_pre_fc_levers"

    async def stale(*_a: object, **_k: object) -> None:
        harness.controller.admit_emergency_stop()

    monkeypatch.setattr(harness.controller, name, stale)
    called: list[str] = []

    async def guard(*_a: object, **_k: object) -> None:
        called.append("guard")

    monkeypatch.setattr(harness.controller, "_maybe_finish_drop", guard)
    monkeypatch.setattr(harness.controller, "_apply_deterministic_post_fc_levers", guard)
    await _tick(harness, _reading(185.0, ror=12.0))
    assert called == []


@pytest.mark.asyncio
async def test_no_raise_on_a_tick_the_target_drop_is_due_and_alerts_are_once() -> None:
    harness = _charged()
    harness.controller._current_heat = 60  # pyright: ignore[reportPrivateUsage]
    harness.controller._charge_monotonic = harness.clock.now - 720.0  # pyright: ignore[reportPrivateUsage]
    _anchor(harness, fc_charge_s=600.0)
    reading = _reading(195.0, ror=6.0)
    blocked = harness.controller._finish_raise_blocked(  # pyright: ignore[reportPrivateUsage]
        reading, 720.0, 698.0
    )
    assert blocked
    harness.controller._finish_alert("finish_overrun")  # pyright: ignore[reportPrivateUsage]
    harness.controller._finish_alert("finish_overrun")  # pyright: ignore[reportPrivateUsage]
    alerts = [p for k, p in harness.events.events if k is RoastEventKind.SAFETY_ALERT]
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_a_failed_target_drop_raises_no_alert_and_holds_the_phase() -> None:
    from tests.test_controller import FailingCommandExecutor

    harness = make_harness(
        readings=[_reading(195.2)], config=ON, executor=FailingCommandExecutor({"drop_beans"})
    )
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:3]:
        harness.controller.transition_to(step)
    harness.controller._charge_monotonic = harness.clock.now - 500.0  # pyright: ignore[reportPrivateUsage]
    _anchor(harness)
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    assert not [p for k, p in harness.events.events if k is RoastEventKind.SAFETY_ALERT]


@pytest.mark.asyncio
async def test_finish_write_skips_without_a_reading_or_a_charge_clock() -> None:
    harness = _charged()
    _anchor(harness)
    await harness.controller._apply_finish_control(None, generation=0)  # pyright: ignore[reportPrivateUsage]
    harness.controller._charge_monotonic = None  # pyright: ignore[reportPrivateUsage]
    await harness.controller._apply_finish_control(  # pyright: ignore[reportPrivateUsage]
        _reading(185.0, ror=12.0), generation=harness.controller.command_generation
    )
    assert harness.executor.targets == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_after", ["command_phase_validity", "all_clear"])
async def test_an_estop_during_the_finish_write_sends_nothing(
    stale_after: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _charged()
    _anchor(harness)
    original = harness.sink.persist_evaluation

    async def persist(evaluation: object) -> int | None:
        result = await original(evaluation)  # type: ignore[arg-type]
        if getattr(evaluation, "rule", None) == stale_after:
            harness.controller.admit_emergency_stop()
        return result

    monkeypatch.setattr(harness.sink, "persist_evaluation", persist)
    gen = harness.controller.command_generation
    await harness.controller._apply_finish_control(  # pyright: ignore[reportPrivateUsage]
        _reading(185.0, ror=12.0), generation=gen
    )
    assert harness.executor.targets == []


@pytest.mark.asyncio
async def test_an_estop_during_the_post_fc_step_ends_the_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _charged(OFF)
    _anchor(harness)

    async def stale(*_a: object, **_k: object) -> None:
        harness.controller.admit_emergency_stop()

    called: list[str] = []

    async def drop(*_a: object, **_k: object) -> None:
        called.append("drop")

    monkeypatch.setattr(harness.controller, "_apply_deterministic_post_fc_levers", stale)
    monkeypatch.setattr(harness.controller, "_maybe_finish_drop", drop)
    await _tick(harness, _reading(185.0, ror=12.0))
    assert called == []


@pytest.mark.asyncio
async def test_the_finish_law_keeps_a_fan_outside_the_pre_fc_box() -> None:
    harness = _charged()
    harness.controller._current_fan = 60  # pyright: ignore[reportPrivateUsage]
    _anchor(harness)
    await _tick(harness, _reading(185.0, ror=12.0))
    assert harness.executor.targets == [(95, 60)]


@pytest.mark.asyncio
async def test_the_human_route_never_takes_the_target_drop() -> None:
    harness = _charged()
    _anchor(harness)
    harness.controller._human_run = object()  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    await harness.controller._maybe_finish_drop(  # pyright: ignore[reportPrivateUsage]
        _reading(195.5), generation=harness.controller.command_generation
    )
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    assert not harness.executor.commands


@pytest.mark.asyncio
@pytest.mark.parametrize(("fc_ago_s", "alerted"), [(30.0, True), (90.0, False)])
async def test_low_development_alert_only_below_the_floor(fc_ago_s: float, alerted: bool) -> None:
    """Charge 700 s ago: FC 30 s ago is 4.3 % (< 10 % floor), 90 s ago is 12.9 %."""
    harness = _charged()
    harness.controller._charge_monotonic = harness.clock.now - 700.0  # pyright: ignore[reportPrivateUsage]
    _anchor(harness)
    harness.controller.transition_to(RoastPhase.DEVELOPMENT)
    harness.controller._first_crack_monotonic = harness.clock.now - fc_ago_s  # pyright: ignore[reportPrivateUsage]
    await harness.controller._maybe_finish_drop(  # pyright: ignore[reportPrivateUsage]
        _reading(195.5), generation=harness.controller.command_generation
    )
    alerts = [p for k, p in harness.events.events if k is RoastEventKind.SAFETY_ALERT]
    assert bool(alerts) is alerted


# --- failed drops fail closed (safety review on #1131) ---------------------------


def _failing_drop(readings: list[RoastTelemetry], heat: int = 55) -> Harness:
    from tests.test_controller import FailingCommandExecutor

    first: list[RoastTelemetry | Exception | None] = [readings[0]]
    harness = make_harness(
        readings=first, config=ON, executor=FailingCommandExecutor({"drop_beans"})
    )
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:3]:
        harness.controller.transition_to(step)
    harness.controller._charge_monotonic = harness.clock.now - 650.0  # pyright: ignore[reportPrivateUsage]
    harness.controller._current_heat = heat  # pyright: ignore[reportPrivateUsage]
    harness.controller._current_fan = 30  # pyright: ignore[reportPrivateUsage]
    _anchor(harness)
    return harness


def _drop_failures(harness: Harness) -> int:
    return sum(
        1
        for k, p in harness.events.events
        if k is RoastEventKind.COMMAND_FAILED and p.get("command") == "drop_beans"  # type: ignore[union-attr]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("first_bean", [195.2, 196.5])  # finish target, ceiling guard
async def test_after_a_failed_pre_fc_drop_heat_never_returns(first_bean: float) -> None:
    harness = _failing_drop([_reading(first_bean)])
    await _tick(harness, _reading(first_bean, ror=6.0))
    assert harness.executor.targets[-1][0] == 0
    assert _drop_failures(harness) == 1  # one drop attempt in the tick, never two
    for bean in (194.6, 194.2, 193.8, 193.5):  # cooling below target: a raise would follow
        await _tick(harness, _reading(bean, ror=6.0))
    assert {h for h, _ in harness.executor.targets[1:]} <= {0}
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK


@pytest.mark.asyncio
async def test_after_a_failed_development_drop_heat_cuts_to_zero_at_once() -> None:
    harness = _failing_drop([_reading(195.5)], heat=95)
    harness.controller.transition_to(RoastPhase.DEVELOPMENT)
    harness.controller._first_crack_monotonic = harness.clock.now - 90.0  # pyright: ignore[reportPrivateUsage]
    await _tick(harness, _reading(195.5, ror=6.0))
    # The tick's ordinary step (95 -> 90) precedes the failed drop; the cut follows it.
    assert harness.executor.targets[-1][0] == 0
    cut = len(harness.executor.targets)
    for bean in (195.0, 194.5):
        await _tick(harness, _reading(bean, ror=6.0))
    assert {h for h, _ in harness.executor.targets[cut:]} <= {0}


@pytest.mark.asyncio
async def test_below_floor_heat_walks_up_one_step_not_to_the_floor() -> None:
    harness = _charged()
    harness.controller._current_heat = 0  # pyright: ignore[reportPrivateUsage]
    harness.controller._charge_monotonic = harness.clock.now - 720.0  # pyright: ignore[reportPrivateUsage]
    _anchor(harness)
    await _tick(harness, _reading(180.0, ror=6.0))
    assert harness.executor.targets == [(5, 30)]


def test_a_recovery_resume_keeps_the_law_off_and_only_a_new_run_re_arms() -> None:
    harness = _charged()
    _anchor(harness)
    harness.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    harness.controller.transition_to(RoastPhase.PREHEATING)
    assert harness.controller._finish_disabled_for_run  # pyright: ignore[reportPrivateUsage]
    harness.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    harness.controller.transition_to(RoastPhase.IDLE)
    harness.controller.transition_to(RoastPhase.STARTING)
    assert not harness.controller._finish_disabled_for_run  # pyright: ignore[reportPrivateUsage]


def test_the_env_raise_cap_must_sit_below_the_env_fault() -> None:
    from pydantic import ValidationError

    from roastpilot_agent.config import AppConfig, SafetyLimits

    with pytest.raises(ValidationError, match="env_raise_cap_c must be below"):
        AppConfig(controller=ON, safety=SafetyLimits(max_env_temp_c=230.0))
    AppConfig(controller=ON, safety=SafetyLimits(max_env_temp_c=231.0))


@pytest.mark.asyncio
async def test_an_estop_during_the_finish_drop_ends_the_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _charged()
    _anchor(harness)

    async def stale(*_a: object, **_k: object) -> None:
        harness.controller.admit_emergency_stop()

    called: list[str] = []

    async def d84(*_a: object, **_k: object) -> None:
        called.append("d84")

    monkeypatch.setattr(harness.controller, "_maybe_finish_drop", stale)
    monkeypatch.setattr(harness.controller, "_maybe_deterministic_drop", d84)
    await _tick(harness, _reading(185.0, ror=12.0))
    assert called == []
