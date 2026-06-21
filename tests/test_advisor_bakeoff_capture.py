"""Tests for the bake-off per-call capture + surfacing (#284).

Hardware- and network-free (the M1 guardrail): every test drives the capture
through the canned reasoning-aware recommender seam
(``reasoning_recommender_factory``) — the real OpenRouter path and
``OPENROUTER_API_KEY`` are never touched. The canned recommender carries a stub
reasoning field so both the present and absent cases are exercised.

They assert the #284 behaviour layered on top of the unchanged #280 scoring +
checkpoint path:

- a run persists the full per-call prompt + response + rationale to the capture
  file, with ``reasoning_available`` true when the provider returned reasoning
  and false when it did not (absence never errors);
- the capture round-trips (write → reload → fields intact) and composes with
  resume (a resumed run's capture is complete and equals the all-at-once one);
- the "most-interesting cells" surfacing selects the right cells (a seeded large
  heat-direction disagreement / pre-FC intervention appears) WITH their prompt +
  reasoning, and a failure is surfaced.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import advisor_bakeoff as bakeoff  # noqa: E402
from advisor_bakeoff import Candidate, InterestKind, Tier  # noqa: E402

from roastpilot_agent.advisor import (  # noqa: E402
    AdvisorContext,
    PydanticAIAdvisor,
    RoastDecision,
)
from roastpilot_agent.models import AdvisorHealth, AdvisorHealthStatus, RoastPhase  # noqa: E402

_SLUG_A = "anthropic/claude-opus-4.8"
_SLUG_B = "google/gemini-3.5-flash"

# A stub reasoning trace the "reasoning model" emits; the "non-reasoning model"
# returns None so both branches are covered.
_STUB_REASONING = "Thinking: bean temp climbing, hold heat to ease into first crack."


# --- Canned reasoning-aware recommender seam (no key, no network) ------------


def _reasoning_recommend(reasoning: str | None) -> bakeoff.ReasoningRecommender:
    """Build a canned reasoning-aware recommender that returns ``reasoning``.

    Pre-FC it HOLDS to mirror the known-good human; this keeps the default
    canned cell free of spurious interventions so a seeded one stands out.
    """

    async def recommend(context: AdvisorContext) -> tuple[RoastDecision, str | None]:
        if context.first_crack_detected:
            decision = RoastDecision(
                target_heat=35,
                target_fan=55,
                should_drop=context.current_bean_temp_c >= 196.0,
                confidence=0.85,
                rationale="development: cut heat, raise fan",
            )
        else:
            decision = RoastDecision(
                target_heat=100,
                target_fan=30,
                should_drop=False,
                confidence=0.8,
                rationale="pre-FC: hold momentum into the crack",
            )
        return decision, reasoning

    return recommend


def _reasoning_factory(reasoning: str | None) -> Any:
    """A ``reasoning_recommender_factory`` returning the canned recommender."""

    def factory(_cand: Candidate, _pv: str) -> bakeoff.ReasoningRecommender:
        return _reasoning_recommend(reasoning)

    return factory


@pytest.fixture
def mock_healthcheck(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``healthcheck`` so every roster slug resolves (no network)."""

    async def fake_healthcheck(self: PydanticAIAdvisor) -> AdvisorHealth:
        return AdvisorHealth(
            status=AdvisorHealthStatus.REACHABLE,
            provider="openai_compatible",
            model_slug=self.descriptor.model,
        )

    monkeypatch.setattr(PydanticAIAdvisor, "healthcheck", fake_healthcheck)


def _roster() -> tuple[Candidate, ...]:
    """A two-slug roster both measured in every phase."""
    return (
        Candidate(_SLUG_A, Tier.BASELINE, bakeoff.PHASE_ORDER),
        Candidate(_SLUG_B, Tier.CONTROL_CANDIDATE, (RoastPhase.DEVELOPMENT,)),
    )


def _fixed_clock() -> Any:
    """A monotonic clock that advances a fixed step per call (deterministic latency)."""
    state = {"t": 0.0}

    def clock() -> float:
        state["t"] += 0.5
        return state["t"]

    return clock


# --- CapturedCall serialisation round-trip ----------------------------------


def test_captured_call_round_trips_with_reasoning() -> None:
    """A captured call (reasoning present) reloads field-for-field identical."""
    fixture = bakeoff.REPLAY_ROASTS[0]
    rid = bakeoff.roast_id_for(fixture)
    ticks, _ground = bakeoff.build_ticks(fixture, cadence_seconds=60.0)
    outcomes = [
        bakeoff.TickOutcome(
            tick=t,
            decision=RoastDecision(
                target_heat=80,
                target_fan=30,
                should_drop=False,
                confidence=0.7,
                rationale="hold",
            ),
            latency_seconds=0.5,
        )
        for t in ticks
    ]
    reasonings: list[str | None] = [_STUB_REASONING for _ in ticks]
    calls = bakeoff.build_captured_calls(
        _SLUG_A, "v2", rid, ticks, outcomes, reasonings, cost_per_call=0.02
    )
    assert calls, "the fixture must yield at least one tick"
    assert all(c.reasoning_available for c in calls)

    for original in calls:
        reloaded = bakeoff.captured_call_from_json(bakeoff.captured_call_to_json(original))
        assert reloaded.context == original.context
        assert reloaded.decision == original.decision
        assert reloaded.reasoning == original.reasoning == _STUB_REASONING
        assert reloaded.reasoning_available is True
        assert reloaded.real_heat_percent == original.real_heat_percent
        assert reloaded.prev_real_heat_percent == original.prev_real_heat_percent
        assert reloaded.real_should_drop == original.real_should_drop


def test_captured_call_without_reasoning_flags_absent_and_does_not_error() -> None:
    """No reasoning → ``reasoning_available`` False; round-trip stays clean."""
    fixture = bakeoff.REPLAY_ROASTS[0]
    rid = bakeoff.roast_id_for(fixture)
    ticks, _ground = bakeoff.build_ticks(fixture, cadence_seconds=60.0)
    outcomes = [
        bakeoff.TickOutcome(tick=t, decision=None, latency_seconds=None, error="boom")
        for t in ticks
    ]
    reasonings: list[str | None] = [None for _ in ticks]
    calls = bakeoff.build_captured_calls(
        _SLUG_A, "v2", rid, ticks, outcomes, reasonings, cost_per_call=0.02
    )
    for original in calls:
        assert original.reasoning is None
        assert original.reasoning_available is False
        reloaded = bakeoff.captured_call_from_json(bakeoff.captured_call_to_json(original))
        assert reloaded.reasoning is None
        assert reloaded.reasoning_available is False
        assert reloaded.decision is None
        assert reloaded.error == "boom"


# --- A run persists prompt + response + rationale (+ reasoning) --------------


@pytest.mark.asyncio
async def test_run_persists_full_capture_with_reasoning(
    mock_healthcheck: None, tmp_path: Path
) -> None:
    """A run writes per-call prompt + response + rationale + reasoning to disk."""
    out = tmp_path / "bakeoff.json"
    roster = _roster()
    result = await bakeoff.run_replay_bakeoff_observable(
        roster,
        bakeoff.REPLAY_ROASTS,
        ["v2"],
        None,
        60.0,
        out=out,
        reasoning_recommender_factory=_reasoning_factory(_STUB_REASONING),
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
    )

    assert result.captured_calls, "the run must capture per-call records"
    # Every captured call carries the full prompt (context) + structured response.
    for call in result.captured_calls:
        assert isinstance(call.context, AdvisorContext)
        assert call.decision is not None
        assert call.decision.rationale  # the rationale is part of the response
        assert call.reasoning == _STUB_REASONING
        assert call.reasoning_available is True

    # The capture is on its own gitignored *.capture.jsonl path, not the sidecar.
    cap = bakeoff.capture_path(out)
    assert cap.name.endswith(".capture.jsonl")
    on_disk = [ln for ln in cap.read_text().splitlines() if ln.strip()]
    assert len(on_disk) == len(result.captured_calls)


@pytest.mark.asyncio
async def test_run_without_reasoning_records_absence(
    mock_healthcheck: None, tmp_path: Path
) -> None:
    """A provider that returns no reasoning → all calls flag absence, no error."""
    out = tmp_path / "bakeoff.json"
    result = await bakeoff.run_replay_bakeoff_observable(
        _roster(),
        bakeoff.REPLAY_ROASTS,
        ["v2"],
        None,
        60.0,
        out=out,
        reasoning_recommender_factory=_reasoning_factory(None),
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
    )
    assert result.captured_calls
    assert all(c.reasoning is None for c in result.captured_calls)
    assert all(c.reasoning_available is False for c in result.captured_calls)


# --- Capture composes with resume -------------------------------------------


@pytest.mark.asyncio
async def test_capture_is_complete_across_resume(mock_healthcheck: None, tmp_path: Path) -> None:
    """Kill mid-run, resume: the resumed run's capture equals the all-at-once one."""
    roster = _roster()
    roasts = bakeoff.REPLAY_ROASTS

    # Reference: a clean all-at-once capture. Same cost_per_call as the killed
    # run below so the captured ``cost_estimate_usd`` (a per-run config value)
    # matches and the assertion isolates resume-completeness.
    ref_out = tmp_path / "ref.json"
    reference = await bakeoff.run_replay_bakeoff_observable(
        roster,
        roasts,
        ["v2"],
        None,
        60.0,
        out=ref_out,
        reasoning_recommender_factory=_reasoning_factory(_STUB_REASONING),
        cost_per_call=1.0,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
    )

    # Kill after K cells via a budget that only affords some of them.
    out = tmp_path / "bakeoff.json"
    killed = await bakeoff.run_replay_bakeoff_observable(
        roster,
        roasts,
        ["v2"],
        None,
        60.0,
        out=out,
        reasoning_recommender_factory=_reasoning_factory(_STUB_REASONING),
        cost_per_call=1.0,
        max_spend=30.0,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
    )
    assert killed.stopped_for_budget is True
    assert 0 < killed.fresh_cells < len(roster) * len(roasts)

    # Resume with no budget → the capture file is appended to, and the final
    # capture is complete and equal to the all-at-once reference.
    resumed = await bakeoff.run_replay_bakeoff_observable(
        roster,
        roasts,
        ["v2"],
        None,
        60.0,
        out=out,
        reasoning_recommender_factory=_reasoning_factory(_STUB_REASONING),
        cost_per_call=1.0,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
    )
    assert resumed.resumed_cells == killed.fresh_cells

    def _key(c: bakeoff.CapturedCall) -> tuple[str, str, str, int]:
        return (c.model_slug, c.prompt_version, c.roast_id, c.tick_index)

    resumed_json = sorted(
        (bakeoff.captured_call_to_json(c) for c in resumed.captured_calls),
        key=lambda r: (r["model_slug"], r["prompt_version"], r["roast_id"], r["tick_index"]),
    )
    reference_json = sorted(
        (bakeoff.captured_call_to_json(c) for c in reference.captured_calls),
        key=lambda r: (r["model_slug"], r["prompt_version"], r["roast_id"], r["tick_index"]),
    )
    assert len(resumed.captured_calls) == len(reference.captured_calls)
    assert {_key(c) for c in resumed.captured_calls} == {_key(c) for c in reference.captured_calls}
    assert resumed_json == reference_json


@pytest.mark.asyncio
async def test_capture_no_resume_truncates(mock_healthcheck: None, tmp_path: Path) -> None:
    """``--no-resume`` discards a pre-existing capture so it never double-counts."""
    out = tmp_path / "bakeoff.json"
    roster = _roster()
    first = await bakeoff.run_replay_bakeoff_observable(
        roster,
        bakeoff.REPLAY_ROASTS,
        ["v2"],
        None,
        60.0,
        out=out,
        reasoning_recommender_factory=_reasoning_factory(_STUB_REASONING),
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
    )
    second = await bakeoff.run_replay_bakeoff_observable(
        roster,
        bakeoff.REPLAY_ROASTS,
        ["v2"],
        None,
        60.0,
        out=out,
        resume=False,
        reasoning_recommender_factory=_reasoning_factory(_STUB_REASONING),
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
    )
    cap = bakeoff.capture_path(out)
    on_disk = [ln for ln in cap.read_text().splitlines() if ln.strip()]
    # A clean re-run leaves exactly one run's worth on disk (not two).
    assert len(on_disk) == len(second.captured_calls) == len(first.captured_calls)


# --- "Most-interesting cells" surfacing -------------------------------------


def _capture_at(
    *,
    phase: RoastPhase,
    real_heat: int,
    prev_real_heat: int | None,
    real_fan: int,
    model_heat: int,
    model_fan: int,
    reasoning: str | None,
    tick_index: int = 0,
    decision: RoastDecision | None = None,
    error: str | None = None,
) -> bakeoff.CapturedCall:
    """Build a synthetic :class:`CapturedCall` for the surfacing tests."""
    if decision is None and error is None:
        decision = RoastDecision(
            target_heat=model_heat,
            target_fan=model_fan,
            should_drop=False,
            confidence=0.7,
            rationale="synthetic",
        )
    context = AdvisorContext(
        phase=phase,
        roast_elapsed_seconds=float(tick_index * 30),
        development_elapsed_seconds=None,
        current_bean_temp_c=168.0,
        current_env_temp_c=190.0,
        bean_ror_c_per_min=10.7,
        env_ror_c_per_min=5.0,
        target_drop_temp_c=196.0,
        target_development_percent=20.0,
        profile_name="synthetic/roast",
        recent_telemetry_samples=[],
        first_crack_detected=phase is RoastPhase.DEVELOPMENT,
        first_crack_timestamp_seconds=None,
    )
    return bakeoff.CapturedCall(
        model_slug="openai/gpt-5-mini",
        prompt_version="v4",
        roast_id="synthetic/roast",
        tick_index=tick_index,
        monotonic_seconds=float(tick_index * 30),
        roast_elapsed_seconds=float(tick_index * 30),
        phase=phase.value,
        context=context,
        decision=decision,
        reasoning=reasoning,
        reasoning_available=reasoning is not None,
        latency_seconds=1.2,
        error=error,
        cost_estimate_usd=0.02,
        real_heat_percent=real_heat,
        real_fan_percent=real_fan,
        prev_real_heat_percent=prev_real_heat,
        real_should_drop=False,
    )


def test_surfacing_catches_pre_fc_fan_into_crack() -> None:
    """The 16-Jun pre-FC heat-cut + fan-into-crack case is surfaced with reasoning.

    Mirrors the worked negative case: pre-FC, the human held 100/30 while the
    model recommended heat 100→60 + fan 30→50. It must land in BOTH the pre-FC
    intervention list and the heat-direction-disagreement list, with the prompt +
    rationale + reasoning available for the lookup.
    """
    pre_fc_bake = _capture_at(
        phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
        real_heat=100,
        prev_real_heat=100,  # human held
        real_fan=30,
        model_heat=60,  # model cut heat
        model_fan=50,  # model raised fan into the crack
        reasoning="Pre-first-crack: I will cut heat to 60 and open the fan to 50.",
    )
    # A benign held cell that must NOT be surfaced.
    benign = _capture_at(
        phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
        real_heat=100,
        prev_real_heat=100,
        real_fan=30,
        model_heat=100,
        model_fan=30,
        reasoning=None,
        tick_index=1,
    )

    selected = bakeoff.select_interesting_calls([benign, pre_fc_bake], top_n=5)

    pre_fc = selected[InterestKind.PRE_FC_INTERVENTION]
    assert len(pre_fc) == 1
    assert pre_fc[0].call is pre_fc_bake

    heat_dis = selected[InterestKind.HEAT_DIRECTION_DISAGREEMENT]
    assert any(i.call is pre_fc_bake for i in heat_dis)

    # The surfaced report carries the prompt, the rationale, and the reasoning.
    report = bakeoff.render_interesting_calls(selected, top_n=5)
    assert "Pre-first-crack" in report  # the reasoning trace
    assert "heat 100→60%" in report
    assert "fan 30→50%" in report
    assert "rationale=" in report
    assert "openai/gpt-5-mini" in report


def test_surfacing_ranks_largest_heat_disagreement_first() -> None:
    """The biggest heat-direction swing ranks ahead of a smaller one."""
    small = _capture_at(
        phase=RoastPhase.DEVELOPMENT,
        real_heat=50,
        prev_real_heat=40,  # human raised
        real_fan=40,
        model_heat=35,  # model cut (opposite direction), small swing |35-50|=15
        model_fan=40,
        reasoning=None,
        tick_index=0,
    )
    large = _capture_at(
        phase=RoastPhase.DEVELOPMENT,
        real_heat=80,
        prev_real_heat=40,  # human raised hard
        real_fan=40,
        model_heat=20,  # model cut hard (opposite, big swing)
        model_fan=40,
        reasoning="big swing",
        tick_index=1,
    )
    selected = bakeoff.select_interesting_calls([small, large], top_n=5)
    heat_dis = selected[InterestKind.HEAT_DIRECTION_DISAGREEMENT]
    assert [i.call for i in heat_dis] == [large, small]
    assert heat_dis[0].score > heat_dis[1].score


def test_surfacing_lists_failures() -> None:
    """A call with no decision is surfaced under the failure category."""
    failure = _capture_at(
        phase=RoastPhase.DEVELOPMENT,
        real_heat=50,
        prev_real_heat=50,
        real_fan=40,
        model_heat=0,
        model_fan=0,
        reasoning=None,
        decision=None,
        error="AdvisorProviderError: 503 upstream",
    )
    selected = bakeoff.select_interesting_calls([failure], top_n=5)
    failures = selected[InterestKind.FAILURE]
    assert len(failures) == 1
    assert "503 upstream" in failures[0].reason
    report = bakeoff.render_interesting_calls(selected, top_n=5)
    assert "no decision" in report
    assert "503 upstream" in report


def test_surfacing_respects_top_n() -> None:
    """Only ``top_n`` calls are kept per category."""
    calls = [
        _capture_at(
            phase=RoastPhase.DEVELOPMENT,
            real_heat=80,
            prev_real_heat=40,
            real_fan=40,
            model_heat=20 - i,  # increasing swing → distinct scores
            model_fan=40,
            reasoning=None,
            tick_index=i,
        )
        for i in range(5)
    ]
    selected = bakeoff.select_interesting_calls(calls, top_n=2)
    assert len(selected[InterestKind.HEAT_DIRECTION_DISAGREEMENT]) == 2


def test_interesting_cells_to_json_carries_reasoning() -> None:
    """The JSON surfacing entry includes the reasoning + availability flag."""
    call = _capture_at(
        phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
        real_heat=100,
        prev_real_heat=100,
        real_fan=30,
        model_heat=60,
        model_fan=50,
        reasoning="trace text",
    )
    selected = bakeoff.select_interesting_calls([call], top_n=5)
    payload = bakeoff.interesting_cells_to_json(selected)
    entries = payload[InterestKind.PRE_FC_INTERVENTION.value]
    assert entries[0]["reasoning"] == "trace text"
    assert entries[0]["reasoning_available"] is True
    assert entries[0]["model_slug"] == "openai/gpt-5-mini"
