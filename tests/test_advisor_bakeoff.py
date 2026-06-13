"""Tests for the per-phase advisor bake-off harness (#172/#173).

Hardware- and network-free (the M1 guardrail): the provider transport is
mocked, so no real OpenRouter call is ever made. They assert the harness
behavior the operator relies on — the roster is well-formed, the availability
sweep drops + reports unresolved slugs while keeping resolved ones, the
per-phase contexts are grounded and correctly phase-stamped, and the decision
table renders from synthetic results (latency-weighted FC ordering, full advice
text, no auto-pick).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import advisor_bakeoff as bakeoff  # noqa: E402

from roastpilot_agent.advisor import (  # noqa: E402
    AdvisorProviderError,
    PydanticAIAdvisor,
    RoastDecision,
)
from roastpilot_agent.models import (  # noqa: E402
    AdvisorHealth,
    AdvisorHealthStatus,
    RoastPhase,
)

# A simulated set: one slug resolves, one 404s. Used to drive the sweep without
# touching the network.
_OK_SLUG = "anthropic/claude-opus-4.8"
_DEAD_SLUG = "google/gemini-3.1-flash-lite"


def _decision(heat: int = 60, fan: int = 40) -> RoastDecision:
    """Build a representative :class:`RoastDecision` for synthetic cells."""
    return RoastDecision(
        target_heat=heat,
        target_fan=fan,
        should_drop=False,
        confidence=0.8,
        rationale="hold and develop",
    )


# --- Roster ----------------------------------------------------------------


def test_roster_is_well_formed() -> None:
    """Every roster entry carries a slug, a tier, and at least one phase."""
    assert bakeoff.ROSTER, "roster must not be empty"
    for cand in bakeoff.ROSTER:
        assert cand.slug, "candidate slug must be non-empty"
        assert isinstance(cand.tier, bakeoff.Tier)
        assert cand.primary_phases, f"{cand.slug} must name a primary phase"
        for phase in cand.primary_phases:
            assert isinstance(phase, RoastPhase)


def test_roster_covers_all_tiers_and_keeps_incumbent() -> None:
    """The #173 roster has all four tiers, the incumbent, and no duplicate slugs."""
    tiers = {c.tier for c in bakeoff.ROSTER}
    assert tiers == set(bakeoff.Tier)
    slugs = [c.slug for c in bakeoff.ROSTER]
    assert len(slugs) == len(set(slugs)), "roster slugs must be unique"
    incumbents = [c for c in bakeoff.ROSTER if c.tier is bakeoff.Tier.INCUMBENT]
    assert [c.slug for c in incumbents] == ["anthropic/claude-opus-4.8"]


def test_fast_reasoning_tier_is_flagged_latency_risk() -> None:
    """Fast-reasoning candidates are flagged as latency-risk (issue caution)."""
    for cand in bakeoff.ROSTER:
        if cand.tier is bakeoff.Tier.FAST_REASONING:
            assert cand.latency_risk, f"{cand.slug} (fast-reasoning) must be latency-risk"


# --- Availability sweep -----------------------------------------------------


@pytest.fixture
def mock_healthcheck(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``healthcheck`` to resolve ``_OK_SLUG`` and 404 ``_DEAD_SLUG``.

    Decided purely from the advisor's configured base slug — no network.
    """

    async def fake_healthcheck(self: PydanticAIAdvisor) -> AdvisorHealth:
        slug = self.descriptor.model
        if slug == _DEAD_SLUG:
            return AdvisorHealth(
                status=AdvisorHealthStatus.UNREACHABLE,
                provider="openai_compatible",
                model_slug=slug,
                error="404 model not found",
            )
        return AdvisorHealth(
            status=AdvisorHealthStatus.REACHABLE,
            provider="openai_compatible",
            model_slug=slug,
        )

    monkeypatch.setattr(PydanticAIAdvisor, "healthcheck", fake_healthcheck)


@pytest.mark.asyncio
async def test_availability_sweep_drops_and_reports_unavailable(
    mock_healthcheck: None,
) -> None:
    """A simulated-404 slug is dropped; a simulated-OK slug is kept."""
    roster = (
        bakeoff.Candidate(_DEAD_SLUG, bakeoff.Tier.ULTRA_FLASH, (RoastPhase.DEVELOPMENT,)),
        bakeoff.Candidate(_OK_SLUG, bakeoff.Tier.INCUMBENT, bakeoff.PHASE_ORDER),
    )
    survivors, results = await bakeoff.availability_sweep(roster, "v2", None)

    survivor_slugs = [c.slug for c in survivors]
    assert survivor_slugs == [_OK_SLUG]

    by_slug = {r.slug: r for r in results}
    assert by_slug[_OK_SLUG].available is True
    assert by_slug[_OK_SLUG].error is None
    assert by_slug[_DEAD_SLUG].available is False
    assert by_slug[_DEAD_SLUG].error == "404 model not found"


@pytest.mark.asyncio
async def test_availability_sweep_report_lists_dropped_with_error(
    mock_healthcheck: None,
) -> None:
    """The rendered availability section names the dropped slug and its error."""
    roster = (
        bakeoff.Candidate(_DEAD_SLUG, bakeoff.Tier.ULTRA_FLASH, (RoastPhase.DEVELOPMENT,)),
        bakeoff.Candidate(_OK_SLUG, bakeoff.Tier.INCUMBENT, bakeoff.PHASE_ORDER),
    )
    _, results = await bakeoff.availability_sweep(roster, "v2", None)
    report = bakeoff.render_availability(results)

    assert "DROPPED (1)" in report
    assert _DEAD_SLUG in report
    assert "404 model not found" in report
    assert f"kept (1): {_OK_SLUG}" in report


def test_render_availability_all_resolved() -> None:
    """With nothing dropped the report says so explicitly."""
    results = [bakeoff.AvailabilityResult(slug=_OK_SLUG, available=True)]
    report = bakeoff.render_availability(results)
    assert "dropped (0)" in report
    assert "DROPPED" not in report


# --- Per-phase contexts -----------------------------------------------------


@pytest.mark.parametrize(
    ("phase", "fc_detected", "has_dev"),
    [
        (RoastPhase.PREHEATING, False, False),
        (RoastPhase.ROASTING_PRE_FIRST_CRACK, False, False),
        (RoastPhase.DEVELOPMENT, True, True),
    ],
)
def test_phase_contexts_are_grounded_and_phase_stamped(
    phase: RoastPhase, fc_detected: bool, has_dev: bool
) -> None:
    """Each phase builds a grounded context with the right phase + FC state."""
    context, mono = bakeoff.build_phase_context(bakeoff.DEFAULT_FIXTURE, phase)
    assert context.phase is phase
    assert context.first_crack_detected is fc_detected
    assert (context.development_elapsed_seconds is not None) is has_dev
    # Grounded in real telemetry: a plausible Celsius bean temperature.
    assert 0.0 < context.current_bean_temp_c < 230.0
    assert mono > 0.0
    assert context.recent_telemetry_samples, "context must carry recent samples"


def test_preheat_context_supplies_charge_band() -> None:
    """The preheat context carries the charge guidance band for v3's section."""
    context, _ = bakeoff.build_phase_context(bakeoff.DEFAULT_FIXTURE, RoastPhase.PREHEATING)
    assert context.charge_guidance_min_c is not None
    assert context.charge_guidance_max_c is not None
    assert context.charge_guidance_min_c < context.charge_guidance_max_c


def test_phase_contexts_warm_through_the_roast() -> None:
    """Bean temperature rises preheat < pre-FC < development (sane grounding)."""
    preheat, _ = bakeoff.build_phase_context(bakeoff.DEFAULT_FIXTURE, RoastPhase.PREHEATING)
    pre_fc, _ = bakeoff.build_phase_context(
        bakeoff.DEFAULT_FIXTURE, RoastPhase.ROASTING_PRE_FIRST_CRACK
    )
    dev, _ = bakeoff.build_phase_context(bakeoff.DEFAULT_FIXTURE, RoastPhase.DEVELOPMENT)
    assert preheat.current_bean_temp_c < pre_fc.current_bean_temp_c < dev.current_bean_temp_c


# --- Cell summarisation + decision table ------------------------------------


def _cell(
    slug: str,
    phase: RoastPhase,
    median: float | None,
    *,
    tier: bakeoff.Tier = bakeoff.Tier.ULTRA_FLASH,
    prompt_version: str = "v2",
    decision: RoastDecision | None = None,
    latency_risk: bool = False,
) -> bakeoff.CellResult:
    """Build a synthetic :class:`CellResult` via the pure summariser."""
    cand = bakeoff.Candidate(slug, tier, (phase,), latency_risk=latency_risk)
    latencies = [median] if median is not None else []
    return bakeoff.summarize_cell(
        cand,
        phase,
        prompt_version,
        latencies,
        decision if decision is not None or median is None else _decision(),
        None if median is not None else "AdvisorProviderError: boom",
    )


def test_summarize_cell_gates() -> None:
    """The summariser sets the 10 s gate and the tighter FC gate correctly."""
    fast = _cell(_OK_SLUG, RoastPhase.DEVELOPMENT, 1.5)
    assert fast.passes_gate is True
    assert fast.passes_fc_gate is True

    mid = _cell(_OK_SLUG, RoastPhase.DEVELOPMENT, 6.0)
    assert mid.passes_gate is True
    assert mid.passes_fc_gate is False

    slow = _cell(_OK_SLUG, RoastPhase.DEVELOPMENT, 12.0)
    assert slow.passes_gate is False
    assert slow.passes_fc_gate is False


def test_summarize_cell_failed_call() -> None:
    """A cell with no successful call carries the error and fails both gates."""
    failed = _cell(_DEAD_SLUG, RoastPhase.DEVELOPMENT, None)
    assert failed.ok_count == 0
    assert failed.latency_median is None
    assert failed.passes_gate is False
    assert failed.passes_fc_gate is False
    assert failed.decision is None
    assert failed.error is not None


def test_decision_table_renders_from_synthetic_cells() -> None:
    """The table renders phases, both prompt versions, advice text — no auto-pick."""
    cells = [
        _cell("fast/flash", RoastPhase.DEVELOPMENT, 1.2, prompt_version="v2"),
        _cell("slow/opus", RoastPhase.DEVELOPMENT, 8.0, prompt_version="v2"),
        _cell("cap/model", RoastPhase.ROASTING_PRE_FIRST_CRACK, 4.0, prompt_version="v2"),
        _cell("pre/heat", RoastPhase.PREHEATING, 3.0, prompt_version="v3"),
    ]
    table = bakeoff.render_decision_table(cells)

    assert "prompt_version = v2" in table
    assert "prompt_version = v3" in table
    assert "FIRST CRACK / development" in table
    # Advice text is surfaced for operator judgement.
    assert "hold and develop" in table
    # No auto-pick: the report never declares a winner.
    assert "winner" not in table.lower()
    assert "auto" in table.lower()  # the "NO model is auto-selected" note


def test_decision_table_fc_is_latency_weighted() -> None:
    """In the development section the faster model is listed before the slower."""
    cells = [
        _cell("slow/opus", RoastPhase.DEVELOPMENT, 8.0),
        _cell("fast/flash", RoastPhase.DEVELOPMENT, 1.2),
    ]
    table = bakeoff.render_decision_table(cells)
    assert table.index("fast/flash") < table.index("slow/opus")


def test_decision_table_flags_latency_risk() -> None:
    """A latency-risk candidate is annotated in its row."""
    cells = [
        _cell(
            "deepseek/r1",
            RoastPhase.ROASTING_PRE_FIRST_CRACK,
            5.0,
            tier=bakeoff.Tier.FAST_REASONING,
            latency_risk=True,
        )
    ]
    table = bakeoff.render_decision_table(cells)
    assert "latency-risk" in table


# --- run_cell against a mocked advisor (no network) -------------------------


@pytest.mark.asyncio
async def test_run_cell_uses_mocked_recommendation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_cell`` measures N mocked calls and surfaces the returned advice."""
    calls = {"n": 0}

    async def fake_reco(self: PydanticAIAdvisor, context: object) -> RoastDecision:
        calls["n"] += 1
        return _decision(heat=55, fan=35)

    monkeypatch.setattr(PydanticAIAdvisor, "get_recommendation", fake_reco)
    context, _ = bakeoff.build_phase_context(bakeoff.DEFAULT_FIXTURE, RoastPhase.DEVELOPMENT)
    cand = bakeoff.Candidate(_OK_SLUG, bakeoff.Tier.INCUMBENT, bakeoff.PHASE_ORDER)

    cell = await bakeoff.run_cell(cand, RoastPhase.DEVELOPMENT, context, 3, "v3", None)

    assert calls["n"] == 3
    assert cell.ok_count == 3
    assert cell.decision is not None
    assert cell.decision.target_heat == 55
    assert cell.prompt_version == "v3"
    assert cell.latency_median is not None


@pytest.mark.asyncio
async def test_run_cell_records_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing provider call yields a no-decision cell carrying the error."""

    async def fake_reco(self: PydanticAIAdvisor, context: object) -> RoastDecision:
        raise AdvisorProviderError("provider down")

    monkeypatch.setattr(PydanticAIAdvisor, "get_recommendation", fake_reco)
    context, _ = bakeoff.build_phase_context(bakeoff.DEFAULT_FIXTURE, RoastPhase.DEVELOPMENT)
    cand = bakeoff.Candidate(_OK_SLUG, bakeoff.Tier.INCUMBENT, bakeoff.PHASE_ORDER)

    cell = await bakeoff.run_cell(cand, RoastPhase.DEVELOPMENT, context, 2, "v2", None)

    assert cell.ok_count == 0
    assert cell.decision is None
    assert cell.error is not None
    assert "provider down" in cell.error


@pytest.mark.asyncio
async def test_run_bakeoff_end_to_end_mocked(
    mock_healthcheck: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full run sweeps, samples survivors per phase, and serialises (no net)."""

    async def fake_reco(self: PydanticAIAdvisor, context: object) -> RoastDecision:
        return _decision()

    monkeypatch.setattr(PydanticAIAdvisor, "get_recommendation", fake_reco)
    roster = (
        bakeoff.Candidate(_DEAD_SLUG, bakeoff.Tier.ULTRA_FLASH, (RoastPhase.DEVELOPMENT,)),
        bakeoff.Candidate(_OK_SLUG, bakeoff.Tier.INCUMBENT, bakeoff.PHASE_ORDER),
    )
    availability, cells = await bakeoff.run_bakeoff(
        roster, bakeoff.DEFAULT_FIXTURE, 1, ["v2"], None
    )

    # The dead slug was dropped; only the survivor was sampled, across 3 phases.
    assert {a.slug for a in availability} == {_DEAD_SLUG, _OK_SLUG}
    sampled = {c.slug for c in cells}
    assert sampled == {_OK_SLUG}
    assert len(cells) == len(bakeoff.PHASE_ORDER)

    # The JSON artifact round-trips the decision.
    rows = bakeoff.cells_to_json(cells)
    assert all(r["decision"] is not None for r in rows)
