"""Per-bean expected first-crack temperature (#1135).

A profile's ``first_crack_bean_temp_c`` re-aims the D239 anchor, its freeze
point and the advisor's first-crack ETA; the pre-FC late-Maillard trim stays on
the config target. ``None`` keeps the config default.
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from roastpilot_agent.config import ControllerConfig, FcAnchorConfig
from roastpilot_agent.fc_prediction import AnchorState
from roastpilot_agent.models import BeanProfile, RoastPhase, RoastProfile
from tests.test_controller import PROFILE, Harness, make_harness
from tests.test_fc_anchor_wiring import (
    _charged,  # pyright: ignore[reportPrivateUsage]
    _ramp,  # pyright: ignore[reportPrivateUsage]
    _reading,  # pyright: ignore[reportPrivateUsage]
)

PER_BEAN = PROFILE.model_copy(update={"first_crack_bean_temp_c": 184.0})


def _freeze_temp(harness: Harness) -> float:
    tracker = harness.controller._fc_anchor_tracker  # pyright: ignore[reportPrivateUsage]
    return tracker._settings.freeze_bean_temp_c  # pyright: ignore[reportPrivateUsage]


def _profile(**update: object) -> RoastProfile:
    return RoastProfile.model_validate({**PROFILE.model_dump(), **update})


# --- the model field ---


def test_field_defaults_to_none_and_round_trips() -> None:
    assert PROFILE.first_crack_bean_temp_c is None
    stored = _profile(first_crack_bean_temp_c=179.5).model_dump_json()
    assert RoastProfile.model_validate_json(stored).first_crack_bean_temp_c == 179.5


@pytest.mark.parametrize("value", [160.0, 195.0])
def test_field_accepts_its_bounds(value: float) -> None:
    assert _profile(first_crack_bean_temp_c=value).first_crack_bean_temp_c == value


@pytest.mark.parametrize("value", [159.9, 195.1, math.nan, math.inf])
def test_field_rejects_out_of_range_and_non_finite(value: float) -> None:
    with pytest.raises(ValidationError):
        _profile(first_crack_bean_temp_c=value)


@pytest.mark.parametrize(("fc", "drop"), [(190.0, 190.0), (190.0, 189.0)])
def test_first_crack_must_sit_below_the_drop_target(fc: float, drop: float) -> None:
    with pytest.raises(ValidationError, match="first_crack_bean_temp_c must be below"):
        _profile(first_crack_bean_temp_c=fc, target_drop_temp_c=drop)


def test_just_below_the_drop_target_is_valid() -> None:
    assert _profile(first_crack_bean_temp_c=189.9, target_drop_temp_c=190.0)


def test_saved_template_carries_the_field_into_the_roast_profile() -> None:
    template = BeanProfile.model_validate(
        {
            **PROFILE.model_dump(exclude={"bean_weight_grams"}),
            "first_crack_bean_temp_c": 172.0,
            "id": "p1",
            "created_at": "2026-11-24T00:00:00Z",
            "updated_at": "2026-11-24T00:00:00Z",
            "default_bean_weight_grams": 200.0,
        }
    )
    assert template.to_roast_profile(200.0).first_crack_bean_temp_c == 172.0


# --- the controller ---


def test_target_resolves_to_the_profile_value_else_the_config_default() -> None:
    harness = make_harness(readings=[])
    ctrl = harness.controller
    assert ctrl._fc_target_bean_temp_c() == 180.0  # pyright: ignore[reportPrivateUsage]
    ctrl.load_profile(PROFILE)
    assert ctrl._fc_target_bean_temp_c() == 180.0  # pyright: ignore[reportPrivateUsage]
    ctrl.load_profile(PER_BEAN)
    assert ctrl._fc_target_bean_temp_c() == 184.0  # pyright: ignore[reportPrivateUsage]


def test_config_default_is_honoured_when_the_profile_is_silent() -> None:
    harness = make_harness(
        readings=[],
        config=ControllerConfig(fc_anchor=FcAnchorConfig(first_crack_bean_temp_c=182.0)),
    )
    harness.controller.load_profile(PROFILE)
    assert harness.controller._fc_target_bean_temp_c() == 182.0  # pyright: ignore[reportPrivateUsage]
    assert _freeze_temp(harness) == 175.0  # freeze is configured, not derived


def test_the_trim_trigger_no_longer_moves_the_anchor() -> None:
    """#1138: the 176 °C trim trigger and the anchor target are separate."""
    harness = make_harness(
        readings=[], config=ControllerConfig(first_crack_target_bean_temp_c=170.0)
    )
    harness.controller.load_profile(PROFILE)
    assert harness.controller._fc_target_bean_temp_c() == 180.0  # pyright: ignore[reportPrivateUsage]
    assert _freeze_temp(harness) == 175.0


@pytest.mark.asyncio
async def test_per_bean_anchor_is_taken_against_the_per_bean_target() -> None:
    harness = make_harness(readings=[])
    harness.controller.load_profile(PER_BEAN)
    for step in (RoastPhase.STARTING, RoastPhase.PREHEATING, RoastPhase.ROASTING_PRE_FIRST_CRACK):
        harness.controller.transition_to(step)
    harness.controller._charge_monotonic = harness.clock.now  # pyright: ignore[reportPrivateUsage]
    await _ramp(harness, 160.0, 0.2, 40)
    snap = harness.controller.snapshot().fc_prediction
    assert snap is not None
    assert snap.slope_eta_seconds == pytest.approx((184.0 - 167.8) / 0.2, rel=1e-6)
    assert snap.anchor_state is AnchorState.PROVISIONAL
    assert snap.anchor_fc_charge_seconds == pytest.approx(39.0 + 81.0, rel=1e-6)
    # The finish law's planned finish is F / (1 - p) of the per-bean anchor.
    assert snap.planned_finish_charge_seconds == pytest.approx(120.0 / 0.8, rel=1e-6)
    # A crack observed well before the late per-bean anchor re-anchors it once.
    harness.reader.readings = [_reading(168.0, fc=True)]
    await harness.controller.tick()
    snap = harness.controller.snapshot().fc_prediction
    assert snap is not None
    assert snap.anchor_state is AnchorState.REANCHORED
    assert snap.anchor_fc_charge_seconds == pytest.approx(40.0)


@pytest.mark.parametrize(
    ("fc", "open_min", "expected"),
    [
        (None, 155.0, 175.0),  # the config default: 5 °C before 180
        (184.0, 155.0, 179.0),  # 8 °C later crack: freeze 8 °C later
        (170.0, 155.0, 165.0),  # 6 °C earlier crack: freeze 6 °C earlier
        (160.0, 158.0, 158.0),  # never below the opening temperature
        (165.0, 170.0, 165.0),  # ...and never above the expected crack
    ],
)
def test_freeze_temperature_moves_with_the_per_bean_first_crack(
    fc: float | None, open_min: float, expected: float
) -> None:
    config = ControllerConfig(fc_anchor=FcAnchorConfig(open_min_bean_temp_c=open_min))
    harness = make_harness(readings=[], config=config)
    harness.controller.load_profile(_profile(first_crack_bean_temp_c=fc))
    assert _freeze_temp(harness) == expected


@pytest.mark.asyncio
async def test_a_later_crack_keeps_the_anchor_provisional_past_the_default_freeze() -> None:
    """At 172.8 °C the default (171) would freeze; a 184 °C bean stays provisional."""
    default = _charged()
    await _ramp(default, 165.0, 0.2, 40)
    snap = default.controller.snapshot().fc_prediction
    assert snap is not None and snap.anchor_state is AnchorState.FROZEN

    harness = make_harness(readings=[])
    harness.controller.load_profile(PER_BEAN)
    for step in (RoastPhase.STARTING, RoastPhase.PREHEATING, RoastPhase.ROASTING_PRE_FIRST_CRACK):
        harness.controller.transition_to(step)
    harness.controller._charge_monotonic = harness.clock.now  # pyright: ignore[reportPrivateUsage]
    await _ramp(harness, 165.0, 0.2, 40)
    snap = harness.controller.snapshot().fc_prediction
    assert snap is not None and snap.anchor_state is AnchorState.PROVISIONAL


@pytest.mark.asyncio
async def test_start_run_rebuilds_the_anchor_for_the_new_profile() -> None:
    """start_run sets the profile, then the PREHEATING phase reset rebuilds the
    anchor, so a per-bean freeze applies from the first tick."""
    harness = make_harness(readings=[])
    assert _freeze_temp(harness) == 175.0
    await harness.controller.start_run(PER_BEAN)
    assert _freeze_temp(harness) == 179.0


@pytest.mark.asyncio
async def test_load_profile_never_resets_a_live_anchor() -> None:
    harness = make_harness(readings=[])
    harness.controller.load_profile(PROFILE)
    for step in (RoastPhase.STARTING, RoastPhase.PREHEATING, RoastPhase.ROASTING_PRE_FIRST_CRACK):
        harness.controller.transition_to(step)
    harness.controller._charge_monotonic = harness.clock.now  # pyright: ignore[reportPrivateUsage]
    await _ramp(harness, 150.0, 0.2, 40)
    before = harness.controller._fc_anchor_tracker  # pyright: ignore[reportPrivateUsage]
    assert before.anchor.state is AnchorState.PROVISIONAL
    harness.controller.load_profile(PER_BEAN)
    assert harness.controller._fc_anchor_tracker is before  # pyright: ignore[reportPrivateUsage]
    assert _freeze_temp(harness) == 175.0


@pytest.mark.asyncio
async def test_advisor_first_crack_eta_uses_the_per_bean_target() -> None:
    harness = make_harness(readings=[])
    harness.controller.load_profile(PER_BEAN)
    for step in (RoastPhase.STARTING, RoastPhase.PREHEATING, RoastPhase.ROASTING_PRE_FIRST_CRACK):
        harness.controller.transition_to(step)
    harness.controller._charge_monotonic = harness.clock.now  # pyright: ignore[reportPrivateUsage]
    await _ramp(harness, 160.0, 0.2, 40)
    ctrl = harness.controller
    per_bean = ctrl._advisor_first_crack_eta_seconds()  # pyright: ignore[reportPrivateUsage]

    default = _charged()
    await _ramp(default, 160.0, 0.2, 40)
    dctrl = default.controller
    base = dctrl._advisor_first_crack_eta_seconds()  # pyright: ignore[reportPrivateUsage]
    assert per_bean is not None and base is not None
    # The same curve, 4 °C further (184 against the 180 default) at about
    # 0.2 °C/s: about 20 s longer.
    assert per_bean - base == pytest.approx(4.0 / 0.2, rel=0.05)
    # The trim's ETA stays on the 176 °C config target, identical to the
    # default run and 4 °C (about 20 s) short of the default anchor target.
    trim_eta = ctrl._first_crack_eta_seconds()  # pyright: ignore[reportPrivateUsage]
    assert trim_eta == dctrl._first_crack_eta_seconds()  # pyright: ignore[reportPrivateUsage]
    assert trim_eta is not None and base - trim_eta == pytest.approx(4.0 / 0.2, rel=0.05)
    signal = ctrl._trim_signal(None)  # pyright: ignore[reportPrivateUsage]
    assert signal is not None and signal.first_crack_eta_seconds == trim_eta
    # The advisor's context carries the per-bean ETA.
    limits = ctrl._control_limits()  # pyright: ignore[reportPrivateUsage]
    ctx = ctrl._build_advisor_context(_reading(167.8), limits)  # pyright: ignore[reportPrivateUsage]
    assert ctx.first_crack_eta_seconds == per_bean


def test_new_run_leaving_starting_through_recovery_is_not_aimed_at_the_last_bean() -> None:
    """STARTING builds the anchor before the new profile is set; a run that then
    goes STARTING -> recovery -> pre-FC (no PREHEATING reset) must still aim at
    its own profile, not the previous run's per-bean crack."""
    harness = make_harness(readings=[])
    ctrl = harness.controller
    ctrl.load_profile(PER_BEAN)
    ctrl.transition_to(RoastPhase.STARTING)
    assert _freeze_temp(harness) == 179.0  # built against the previous profile
    ctrl._prepare_run_bookkeeping(PROFILE)  # pyright: ignore[reportPrivateUsage]
    ctrl.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    ctrl.transition_to(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    assert ctrl._fc_target_bean_temp_c() == 180.0  # pyright: ignore[reportPrivateUsage]
    assert _freeze_temp(harness) == 175.0


@pytest.mark.parametrize(("freeze", "crack", "ok"), [(175.0, 180.0, True), (180.0, 180.0, False)])
def test_anchor_freeze_must_precede_its_crack(freeze: float, crack: float, ok: bool) -> None:
    """D242: overriding one anchor temperature cannot put the freeze at the crack."""
    if ok:
        FcAnchorConfig(freeze_bean_temp_c=freeze, first_crack_bean_temp_c=crack)
    else:
        with pytest.raises(ValidationError, match="freeze_bean_temp_c must be below"):
            FcAnchorConfig(freeze_bean_temp_c=freeze, first_crack_bean_temp_c=crack)
