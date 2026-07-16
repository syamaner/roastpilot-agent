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

import dataclasses
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import advisor_bakeoff as bakeoff  # noqa: E402
from bakeoff_replay import (  # noqa: E402
    HEAT_DIRECTION_LABELS,
    DropConfusion,
    DropMetrics,
    HeatDirectionConfusion,
    LeverMetrics,
    PhaseLatency,
    RoastScore,
)

from roastpilot_agent.advisor import (  # noqa: E402
    AdvisorContext,
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


def test_roster_is_the_277_screen_with_baseline_and_prior_winner() -> None:
    """The as-run #277 roster has 11 unique models incl. baseline + prior winner.

    The 10th slug is ``x-ai/grok-4.3``, the recovery candidate added when the
    original ``x-ai/grok-4-fast`` slug 404'd as deprecated (see the
    21 Jun results doc disposition). The 11th is ``openai/gpt-5.6-luna``,
    added 16 Jul after passing the ~5s latency screen (median 1.89s,
    faster than the gpt-4o pin) for the #396 heat-fidelity A/B.
    """
    slugs = [c.slug for c in bakeoff.ROSTER]
    assert len(slugs) == 11
    assert len(slugs) == len(set(slugs)), "roster slugs must be unique"
    # The gpt-4o n8n baseline (D40.4) and the prior winner are both present.
    baselines = [c.slug for c in bakeoff.ROSTER if c.tier is bakeoff.Tier.BASELINE]
    assert baselines == ["openai/gpt-4o"]
    prior = [c.slug for c in bakeoff.ROSTER if c.tier is bakeoff.Tier.PRIOR_WINNER]
    assert prior == ["google/gemini-3.1-flash-lite"]


def test_finalists_are_the_ones_carried_to_the_full_set() -> None:
    """The as-run #277 finalists are flagged + returned by finalist_roster.

    These are the candidates carried to the FULL 17-medium set with 2 seeds.
    Of these, gpt-4o / gemini-3.1-flash-lite / gemini-3-flash-preview produced
    usable full data; gpt-5-nano / gpt-5-mini were attempted but proved
    unreachable on this OpenRouter access (see the 21 Jun results doc).
    grok-4.3 was removed from the finalist set (28 Jun 2026) after it
    produced 6.0 s median FC latency — well outside the 2.5 s FC gate — and
    emitted confidence > 1.0 on some ticks (AdvisorUnsafeOutputError);
    it is kept in the ROSTER for screen coverage only.
    """
    expected = {
        "openai/gpt-4o",
        "openai/gpt-5.6-luna",
        "google/gemini-3.1-flash-lite",
        "google/gemini-3-flash-preview",
        "openai/gpt-5-nano",
        "openai/gpt-5-mini",
    }
    assert {c.slug for c in bakeoff.ROSTER if c.finalist} == expected
    assert {c.slug for c in bakeoff.finalist_roster()} == expected
    assert {c.slug for c in bakeoff.screen_roster()} == {c.slug for c in bakeoff.ROSTER}


def test_reasoning_models_are_capped_not_run_at_high() -> None:
    """Gemini/GPT reasoning is pinned minimal/low; no candidate is pinned high."""
    for cand in bakeoff.ROSTER:
        if cand.reasoning is not None:
            assert cand.reasoning in ("minimal", "low"), (
                f"{cand.slug} reasoning must be minimal/low, never high"
            )
    # The known reasoning-capped models carry an explicit cap.
    capped = {c.slug: c.reasoning for c in bakeoff.ROSTER if c.reasoning is not None}
    assert capped["google/gemini-3.1-flash-lite"] == "minimal"
    assert capped["openai/gpt-5-nano"] == "low"


def test_latency_risk_candidate_is_flagged() -> None:
    """gpt-5-mini (reasons before answering) is flagged latency-risk."""
    by_slug = {c.slug: c for c in bakeoff.ROSTER}
    assert by_slug["openai/gpt-5-mini"].latency_risk is True


def test_resolve_reasoning_prefers_candidate_cap() -> None:
    """A candidate's own reasoning cap overrides the run-wide reasoning."""
    capped = bakeoff.Candidate(
        "x/y", bakeoff.Tier.CONTROL_CANDIDATE, (RoastPhase.DEVELOPMENT,), reasoning="low"
    )
    uncapped = bakeoff.Candidate("a/b", bakeoff.Tier.BASELINE, (RoastPhase.DEVELOPMENT,))
    assert bakeoff.resolve_reasoning(capped, "high") == "low"
    assert bakeoff.resolve_reasoning(uncapped, "high") == "high"
    assert bakeoff.resolve_reasoning(uncapped, None) is None


# --- #277 test sets + AS-BUILT prompt/context wiring ------------------------


def test_full_medium_set_is_the_seventeen_known_good_fixtures() -> None:
    """The full set is exactly the 17 classification-doc known-good mediums."""
    assert len(bakeoff.FULL_MEDIUM_FIXTURE_NAMES) == 17
    assert len(set(bakeoff.FULL_MEDIUM_FIXTURE_NAMES)) == 17
    # The dark/over-dark fixtures are deliberately excluded.
    excluded = {"artisan-10", "artisan-15", "artisan-17", "artisan-20", "artisan-28"}
    assert not (set(bakeoff.FULL_MEDIUM_FIXTURE_NAMES) & excluded)


def test_screen_subset_is_a_subset_of_the_full_set() -> None:
    """The ~6-roast screen subset is drawn from the full medium set."""
    assert 1 <= len(bakeoff.SCREEN_MEDIUM_FIXTURE_NAMES) <= 8
    assert set(bakeoff.SCREEN_MEDIUM_FIXTURE_NAMES) <= set(bakeoff.FULL_MEDIUM_FIXTURE_NAMES)
    assert set(bakeoff.TEST_SETS) == {"screen", "full"}


def test_resolve_test_set_errors_clearly_on_missing_fixture(tmp_path: Path) -> None:
    """A missing local-only fixture yields a clear FileNotFoundError naming it."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bakeoff, "ARTISAN_FIXTURES_DIR", tmp_path)
        with pytest.raises(FileNotFoundError, match="missing local-only Artisan fixtures"):
            bakeoff.resolve_test_set(("artisan-99",))


def test_control_prompts_are_registered_as_selectable() -> None:
    """The active control teaching prompt (c3) is the bake-off default, and c1/c2
    stay resolvable as prompt versions for an A/B comparison."""
    from roastpilot_agent.advisor import control_teaching_prompt, instructions_for

    assert bakeoff.CONTROL_PROMPT_VERSION == "c3"
    assert instructions_for("c3") == control_teaching_prompt("c3")
    assert instructions_for("c2") == control_teaching_prompt("c2")
    assert instructions_for("c1") == control_teaching_prompt("c1")


def test_enrich_ticks_adds_the_273_limits_and_275_context() -> None:
    """Enriched ticks carry the phase-resolved box + the per-tick control context."""
    fixture = bakeoff.REPLAY_ROASTS[0]
    plain, ground = bakeoff.build_ticks(fixture, cadence_seconds=30.0)
    enriched = bakeoff.enrich_ticks_with_control_context(plain, ground)
    assert len(enriched) == len(plain)
    # A post-FC (development) tick carries the full box + the #275 fields.
    dev = next(t for t in enriched if t.context.first_crack_detected)
    ctx = dev.context
    assert ctx.heat_ceiling_percent == 100
    assert ctx.bitter_ceiling_temp_c is not None
    assert ctx.emergency_drop_temp_c is not None
    assert ctx.roast_curve_window, "post-FC tick must carry a curve window"
    assert ctx.development_time_ratio is not None
    assert ctx.first_crack_eta_seconds is None  # post-FC: the detector owns it
    # The real-lever / drop labels and timestamps are untouched by enrichment.
    for a, e in zip(plain, enriched, strict=True):
        assert e.real_heat_percent == a.real_heat_percent
        assert e.real_should_drop == a.real_should_drop
        assert e.monotonic_seconds == a.monotonic_seconds


def test_enrich_ticks_adds_the_499_dtr_window_from_the_shared_margin_default() -> None:
    """#499: enrichment stamps the DTR window around each tick's own
    target_development_percent, using ControllerConfig's OWN default margin
    (never a hardcoded literal in the test or the harness)."""
    from roastpilot_agent.config import ControllerConfig

    margin = ControllerConfig().drop_dev_margin_percent
    fixture = bakeoff.REPLAY_ROASTS[0]
    plain, ground = bakeoff.build_ticks(fixture, cadence_seconds=30.0)
    enriched = bakeoff.enrich_ticks_with_control_context(plain, ground)
    dev = next(t for t in enriched if t.context.first_crack_detected)
    ctx = dev.context
    assert ctx.target_development_percent is not None
    assert ctx.target_development_percent_min == pytest.approx(
        ctx.target_development_percent - margin
    )
    assert ctx.target_development_percent_max == pytest.approx(
        ctx.target_development_percent + margin
    )
    # roast_style stays None: these fixtures carry no RoastProfile (pre-#405).
    assert ctx.roast_style is None


def test_enrich_ticks_dtr_window_accepts_an_explicit_margin_override() -> None:
    """The margin is overridable (mirrors every other config-driven bake-off
    parameter) but defaults to ControllerConfig's own value when omitted."""
    fixture = bakeoff.REPLAY_ROASTS[0]
    plain, ground = bakeoff.build_ticks(fixture, cadence_seconds=30.0)
    enriched = bakeoff.enrich_ticks_with_control_context(plain, ground, drop_dev_margin_percent=1.0)
    dev = next(t for t in enriched if t.context.first_crack_detected)
    ctx = dev.context
    assert ctx.target_development_percent is not None
    assert ctx.target_development_percent_min == pytest.approx(ctx.target_development_percent - 1.0)
    assert ctx.target_development_percent_max == pytest.approx(ctx.target_development_percent + 1.0)


def test_enrich_ticks_preserves_the_actuated_heat_fan_from_build_ticks() -> None:
    """#497: enrichment's ``model_copy(update={...})`` only touches the #273/#275
    fields it lists — the actuated ``current_heat_percent``/``current_fan_percent``
    (and ``post_fc_loop_active``) that :func:`build_ticks` already stamped onto
    every context must survive it unchanged, never reset to null."""
    fixture = bakeoff.REPLAY_ROASTS[0]
    plain, ground = bakeoff.build_ticks(fixture, cadence_seconds=30.0)
    enriched = bakeoff.enrich_ticks_with_control_context(plain, ground)
    for a, e in zip(plain, enriched, strict=True):
        assert e.context.current_heat_percent == a.context.current_heat_percent
        assert e.context.current_fan_percent == a.context.current_fan_percent
        assert e.context.current_heat_percent == a.real_heat_percent
        assert e.context.current_fan_percent == a.real_fan_percent
        assert e.context.post_fc_loop_active is False


def test_build_control_ticks_enriches_by_default_and_can_opt_out() -> None:
    """build_control_ticks enriches by default; enrich=False keeps the drop-only ctx."""
    fixture = bakeoff.REPLAY_ROASTS[0]
    enriched, _ = bakeoff.build_control_ticks(fixture, cadence_seconds=30.0)
    plain, _ = bakeoff.build_control_ticks(fixture, cadence_seconds=30.0, enrich=False)
    dev_e = next(t for t in enriched if t.context.first_crack_detected)
    dev_p = next(t for t in plain if t.context.first_crack_detected)
    assert dev_e.context.bitter_ceiling_temp_c is not None
    assert dev_p.context.bitter_ceiling_temp_c is None
    assert dev_e.context.roast_curve_window
    assert not dev_p.context.roast_curve_window


def test_enriched_pre_fc_tick_has_an_fc_eta_and_narrowed_box() -> None:
    """A pre-FC tick gets an FC-ETA and the pre-FC narrowed heat floor (#273)."""
    fixture = bakeoff.REPLAY_ROASTS[0]
    # include_pre_fc to inspect the (gated-out) pre-FC ticks the enrichment builds.
    ticks, _ground = bakeoff.build_control_ticks(fixture, cadence_seconds=20.0, include_pre_fc=True)
    pre = [t for t in ticks if t.context.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK]
    # Mid-roast pre-FC tick (enough curve to project an ETA).
    mid = pre[len(pre) // 2]
    assert mid.context.first_crack_eta_seconds is not None
    # Pre-FC box pins the heat floor to the deterministic target (#222/#273).
    assert mid.context.heat_floor_percent == 100


def test_fc_milestone_is_sourced_only_from_seen_ticks() -> None:
    """The FC milestone never draws on a future (unseen) tick (#299 review).

    The summary is "as of ``ticks[upto_index]``" — like the live controller
    arming FC on the first post-transition tick, it must only ever draw on ticks
    the model has already seen (``ticks[:upto_index + 1]``). The fix bounds the
    FC nearest-tick search the same way the turning point is bounded; this asserts
    the invariant holds at every index from the FC crossing onward, and that a
    contrived future-nearer FC time still resolves to a seen tick.
    """
    fixture = bakeoff.REPLAY_ROASTS[0]
    ticks, ground = bakeoff.build_ticks(fixture, cadence_seconds=30.0)
    fc_index = next(
        i for i, t in enumerate(ticks) if t.monotonic_seconds >= ground.first_crack_seconds
    )
    # The invariant at every index from the FC crossing onward: whatever tick the
    # FC milestone is sourced from must be one the model has already seen.
    for upto in range(fc_index, len(ticks)):
        seen_temps = [t.context.current_bean_temp_c for t in ticks[: upto + 1]]
        milestones = bakeoff._milestones_for(ticks, ground, upto)  # pyright: ignore[reportPrivateUsage]
        fc = next(m for m in milestones if m.kind is bakeoff.RoastMilestoneKind.FIRST_CRACK)
        assert fc.bean_temp_c in seen_temps

    # Direct guard on the bound: skew the FC time to sit nearer a FUTURE tick than
    # the current one, then evaluate AT that future index. An unbounded search at
    # the seen index would reach for the nearer tick; the bounded search must keep
    # to the seen window. Evaluating at upto = fc_index + 1 keeps now >= FC (so the
    # milestone is emitted) while the future tick fc_index + 1 is the closer one.
    if fc_index + 1 < len(ticks):
        seen_t = ticks[fc_index].monotonic_seconds
        next_t = ticks[fc_index + 1].monotonic_seconds
        fc_seconds = seen_t + 0.6 * (next_t - seen_t)  # nearer the future tick
        skewed = dataclasses.replace(ground, first_crack_seconds=fc_seconds)
        # Evaluate one tick later so the milestone surfaces; the seen window is
        # [: fc_index + 2], and the bounded FC source must be inside it.
        milestones = bakeoff._milestones_for(ticks, skewed, fc_index + 1)  # pyright: ignore[reportPrivateUsage]
        fc = next(m for m in milestones if m.kind is bakeoff.RoastMilestoneKind.FIRST_CRACK)
        assert fc.bean_temp_c in [t.context.current_bean_temp_c for t in ticks[: fc_index + 2]]


def test_build_control_ticks_is_development_only_by_default() -> None:
    """The as-built D35 advisor scope: only post-FC development ticks are consulted."""
    fixture = bakeoff.REPLAY_ROASTS[0]
    dev_only, _ = bakeoff.build_control_ticks(fixture, cadence_seconds=30.0)
    assert dev_only, "there must be development ticks to consult"
    # No preheating / pre-first-crack ticks survive the default scope.
    assert all(t.context.phase is RoastPhase.DEVELOPMENT for t in dev_only)
    assert all(t.context.first_crack_detected for t in dev_only)
    # The drop decision lives at the end of development → the drop tick is kept,
    # so drop timing is still scored.
    assert any(t.real_should_drop for t in dev_only)


def test_development_only_keeps_pre_fc_history_in_the_curve_window() -> None:
    """A kept development tick still sees the pre-FC roast-so-far curve (#275)."""
    fixture = bakeoff.REPLAY_ROASTS[0]
    dev_only, _ = bakeoff.build_control_ticks(fixture, cadence_seconds=30.0)
    first_dev = dev_only[0]
    window = first_dev.context.roast_curve_window
    # The first development tick's curve window reaches back well before this
    # tick's own time — enrichment runs over the whole roast (incl. the pre-FC
    # samples), then the scope filter is applied, so the history is preserved.
    assert len(window) > 1
    earliest = min(s.elapsed_since_charge_seconds for s in window)
    assert earliest < first_dev.context.roast_elapsed_seconds


def test_include_pre_fc_restores_the_gated_out_ticks() -> None:
    """--include-pre-fc keeps preheat + pre-FC ticks; the default drops them."""
    fixture = bakeoff.REPLAY_ROASTS[0]
    dev_only, _ = bakeoff.build_control_ticks(fixture, cadence_seconds=30.0)
    full, _ = bakeoff.build_control_ticks(fixture, cadence_seconds=30.0, include_pre_fc=True)
    assert len(full) > len(dev_only)
    phases = {t.context.phase for t in full}
    assert RoastPhase.ROASTING_PRE_FIRST_CRACK in phases or RoastPhase.PREHEATING in phases
    # development_only() is the pure filter behind the default.
    assert bakeoff.development_only(full) == dev_only


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
        bakeoff.Candidate(_DEAD_SLUG, bakeoff.Tier.CONTROL_CANDIDATE, (RoastPhase.DEVELOPMENT,)),
        bakeoff.Candidate(_OK_SLUG, bakeoff.Tier.BASELINE, bakeoff.PHASE_ORDER),
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
        bakeoff.Candidate(_DEAD_SLUG, bakeoff.Tier.CONTROL_CANDIDATE, (RoastPhase.DEVELOPMENT,)),
        bakeoff.Candidate(_OK_SLUG, bakeoff.Tier.BASELINE, bakeoff.PHASE_ORDER),
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


# --- Sweep retry + drop-reason classification -------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ('400 "not a valid model ID"', bakeoff.DROP_REASON_INVALID_MODEL),
        ("invalid model id supplied", bakeoff.DROP_REASON_INVALID_MODEL),
        ('404 "No endpoints found that support tool use"', bakeoff.DROP_REASON_NO_TOOL_USE),
        ("the model has no endpoints for tool-use", bakeoff.DROP_REASON_NO_TOOL_USE),
        ("401 invalid api key", bakeoff.DROP_REASON_AUTH),
        ("connection reset by peer", bakeoff.DROP_REASON_OTHER),
        (None, bakeoff.DROP_REASON_OTHER),
    ],
)
def test_classify_drop_reason(error: str | None, expected: str) -> None:
    """The healthcheck error text classifies into the right drop reason."""
    assert bakeoff.classify_drop_reason(error) == expected


@pytest.mark.asyncio
async def test_probe_slug_retries_a_transient_failure_then_succeeds() -> None:
    """A first-attempt UNREACHABLE that recovers is kept, recording the retry."""
    calls = {"n": 0}

    async def flaky_healthcheck(self: PydanticAIAdvisor) -> AdvisorHealth:
        calls["n"] += 1
        if calls["n"] == 1:
            return AdvisorHealth(
                status=AdvisorHealthStatus.UNREACHABLE,
                provider="openai_compatible",
                model_slug=self.descriptor.model,
                error="transient blip",
            )
        return AdvisorHealth(
            status=AdvisorHealthStatus.REACHABLE,
            provider="openai_compatible",
            model_slug=self.descriptor.model,
        )

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(PydanticAIAdvisor, "healthcheck", flaky_healthcheck)
        result = await bakeoff.probe_slug(_OK_SLUG, "v2", None, sleep=fake_sleep)

    assert result.available is True
    assert result.attempts == 2
    assert result.reason is None
    assert slept, "a transient failure must back off before the retry"


@pytest.mark.asyncio
async def test_probe_slug_drops_after_exhausting_retries_with_reason() -> None:
    """A genuine 400 fails every attempt and is dropped with its classified reason."""

    async def dead_healthcheck(self: PydanticAIAdvisor) -> AdvisorHealth:
        return AdvisorHealth(
            status=AdvisorHealthStatus.UNREACHABLE,
            provider="openai_compatible",
            model_slug=self.descriptor.model,
            error='400 "not a valid model ID"',
        )

    async def fake_sleep(seconds: float) -> None:
        return None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(PydanticAIAdvisor, "healthcheck", dead_healthcheck)
        result = await bakeoff.probe_slug(_DEAD_SLUG, "v2", None, attempts=2, sleep=fake_sleep)

    assert result.available is False
    assert result.attempts == 2
    assert result.reason == bakeoff.DROP_REASON_INVALID_MODEL
    assert result.error is not None and "not a valid model ID" in result.error


def test_render_availability_shows_reason_and_tool_use_note() -> None:
    """The dropped section names the reason; a no-tool-use drop adds the filter note."""
    results = [
        bakeoff.AvailabilityResult(slug=_OK_SLUG, available=True, attempts=1),
        bakeoff.AvailabilityResult(
            slug="dead/model",
            available=False,
            error='400 "not a valid model ID"',
            attempts=2,
            reason=bakeoff.DROP_REASON_INVALID_MODEL,
        ),
        bakeoff.AvailabilityResult(
            slug="exists/no-tools",
            available=False,
            error='404 "No endpoints found that support tool use"',
            attempts=2,
            reason=bakeoff.DROP_REASON_NO_TOOL_USE,
        ),
    ]
    report = bakeoff.render_availability(results)
    assert "DROPPED (2)" in report
    assert "invalid-model-id" in report
    assert "no-tool-use-endpoint" in report
    # The tool-use filter is documented as a real candidate filter.
    assert "Tool-use requirement" in report
    assert "fast-reasoning tier" in report


def test_render_availability_flags_recovered_transient() -> None:
    """A slug kept only after a retry is annotated as a recovered transient."""
    results = [
        bakeoff.AvailabilityResult(slug=_OK_SLUG, available=True, attempts=2),
    ]
    report = bakeoff.render_availability(results)
    assert "resolved only on attempt 2" in report


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
    # #497: every per-phase context carries the real roast's actuated heat/fan
    # (never null); this fixture predates the deterministic post-FC RoR-taper
    # loop (#405/D88), so post_fc_loop_active is False in every phase.
    assert context.current_heat_percent is not None
    assert 0 <= context.current_heat_percent <= 100
    assert context.current_fan_percent is not None
    assert 0 <= context.current_fan_percent <= 100
    assert context.post_fc_loop_active is False


def test_preheat_context_supplies_charge_band() -> None:
    """The preheat context carries the charge guidance band for v3's section."""
    context, _ = bakeoff.build_phase_context(bakeoff.DEFAULT_FIXTURE, RoastPhase.PREHEATING)
    assert context.charge_guidance_min_c is not None
    assert context.charge_guidance_max_c is not None
    assert context.charge_guidance_min_c < context.charge_guidance_max_c


def test_pre_fc_context_carries_the_499_dtr_window_from_shared_margin_default() -> None:
    """#499: the pre-FC bake-off context's synthetic 20.0 % target gets a
    window built from ControllerConfig's own default margin — never a
    hardcoded literal duplicated in this builder."""
    from roastpilot_agent.config import ControllerConfig

    margin = ControllerConfig().drop_dev_margin_percent
    context, _ = bakeoff.build_phase_context(
        bakeoff.DEFAULT_FIXTURE, RoastPhase.ROASTING_PRE_FIRST_CRACK
    )
    assert context.target_development_percent_min == pytest.approx(20.0 - margin)
    assert context.target_development_percent_max == pytest.approx(20.0 + margin)


def test_phase_contexts_warm_through_the_roast() -> None:
    """Bean temperature rises preheat < pre-FC < development (sane grounding)."""
    preheat, _ = bakeoff.build_phase_context(bakeoff.DEFAULT_FIXTURE, RoastPhase.PREHEATING)
    pre_fc, _ = bakeoff.build_phase_context(
        bakeoff.DEFAULT_FIXTURE, RoastPhase.ROASTING_PRE_FIRST_CRACK
    )
    dev, _ = bakeoff.build_phase_context(bakeoff.DEFAULT_FIXTURE, RoastPhase.DEVELOPMENT)
    assert preheat.current_bean_temp_c < pre_fc.current_bean_temp_c < dev.current_bean_temp_c


def test_preheat_context_raises_on_fixture_without_pre_charge_rows(
    tmp_path: Path,
) -> None:
    """A fixture that starts at charge yields a clear error, not an IndexError."""
    fixture = tmp_path / "roast.jsonl"
    # Telemetry only at/after T0, plus the three required events: no warm-up rows.
    lines = [
        '{"type": "event", "kind": "beans_added", "monotonic_seconds": 0.0}',
        '{"type": "event", "kind": "first_crack_detected", "monotonic_seconds": 500.0}',
        '{"type": "event", "kind": "beans_dropped", "monotonic_seconds": 600.0}',
        '{"type": "telemetry", "monotonic_seconds": 10.0, "bean_temp_c": 100.0, '
        '"env_temp_c": 150.0, "heat_level_percent": 80, "fan_level_percent": 20}',
    ]
    fixture.write_text("\n".join(lines))
    with pytest.raises(ValueError, match="no telemetry rows before charge"):
        bakeoff.build_phase_context(fixture, RoastPhase.PREHEATING)


def test_pre_fc_context_raises_on_fixture_without_pre_fc_rows(tmp_path: Path) -> None:
    """A fixture with no charge→FC telemetry yields a clear error, not a crash."""
    fixture = tmp_path / "roast.jsonl"
    # charge and first crack coincide → no pre-first-crack telemetry window.
    lines = [
        '{"type": "event", "kind": "beans_added", "monotonic_seconds": 500.0}',
        '{"type": "event", "kind": "first_crack_detected", "monotonic_seconds": 500.0}',
        '{"type": "event", "kind": "beans_dropped", "monotonic_seconds": 600.0}',
        '{"type": "telemetry", "monotonic_seconds": 550.0, "bean_temp_c": 185.0, '
        '"env_temp_c": 200.0, "heat_level_percent": 60, "fan_level_percent": 40}',
    ]
    fixture.write_text("\n".join(lines))
    with pytest.raises(ValueError, match="no telemetry rows between charge"):
        bakeoff.build_phase_context(fixture, RoastPhase.ROASTING_PRE_FIRST_CRACK)


# --- Cell summarisation + decision table ------------------------------------


def _cell(
    slug: str,
    phase: RoastPhase,
    median: float | None,
    *,
    tier: bakeoff.Tier = bakeoff.Tier.CONTROL_CANDIDATE,
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
            tier=bakeoff.Tier.FRONTIER_CEILING,
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
    cand = bakeoff.Candidate(_OK_SLUG, bakeoff.Tier.BASELINE, bakeoff.PHASE_ORDER)

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
    cand = bakeoff.Candidate(_OK_SLUG, bakeoff.Tier.BASELINE, bakeoff.PHASE_ORDER)

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
        bakeoff.Candidate(_DEAD_SLUG, bakeoff.Tier.CONTROL_CANDIDATE, (RoastPhase.DEVELOPMENT,)),
        bakeoff.Candidate(_OK_SLUG, bakeoff.Tier.BASELINE, bakeoff.PHASE_ORDER),
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


# --- Replay-mode orchestration (real-roast scoring, mocked recommender) -----


async def _canned_recommend(context: AdvisorContext) -> RoastDecision:
    """Canned recommender: cut heat post-FC, drop near the real drop temp."""
    if context.first_crack_detected:
        return RoastDecision(
            target_heat=35,
            target_fan=55,
            should_drop=context.current_bean_temp_c >= 196.0,
            confidence=0.85,
            rationale="development: cut heat, raise fan",
        )
    return RoastDecision(
        target_heat=80,
        target_fan=30,
        should_drop=False,
        confidence=0.8,
        rationale="drying: ease RoR toward FC",
    )


@pytest.mark.asyncio
async def test_score_candidate_over_both_roasts() -> None:
    """A candidate is scored across both replay roasts with an injected clock."""
    clock_state = {"t": 0.0}

    def clock() -> float:
        clock_state["t"] += 0.7
        return clock_state["t"]

    cand = bakeoff.Candidate(_OK_SLUG, bakeoff.Tier.BASELINE, bakeoff.PHASE_ORDER)
    cell = await bakeoff.score_candidate(
        cand, "v3", _canned_recommend, bakeoff.REPLAY_ROASTS, 30.0, clock=clock
    )
    assert cell.prompt_version == "v3"
    assert len(cell.scores) == len(bakeoff.REPLAY_ROASTS)
    # Each roast contributes advice samples at the four key moments.
    for picks in cell.samples.values():
        assert [label for label, _ in picks] == [
            "charge",
            "maillard",
            "first-crack",
            "development",
        ]


def test_render_replay_report_carries_honest_framing_and_no_autopick() -> None:
    """The report leads with the agreement-not-correctness caveat; no winner."""
    drop = DropMetrics(
        precision=1.0,
        recall=1.0,
        f1=1.0,
        true_positives=1,
        false_positives=0,
        false_negatives=0,
        first_drop_seconds=1200.0,
        timing_error_seconds=0.0,
        timing_error_c=0.0,
    )
    lever = LeverMetrics(mae=8.0, directional_agreement=0.6, directional_samples=5)
    score = RoastScore(
        roast_name="live-roast-2026-06-07/session-1",
        tick_count=10,
        ok_count=10,
        drop=drop,
        drop_confusion=DropConfusion(
            true_positives=1, false_positives=0, true_negatives=9, false_negatives=0
        ),
        heat_direction_confusion=HeatDirectionConfusion(
            labels=HEAT_DIRECTION_LABELS,
            matrix=((1, 0, 0), (0, 3, 0), (1, 0, 0)),
            samples=5,
        ),
        heat=lever,
        fan=lever,
        phase_latency=[PhaseLatency("development", 3, 1.2, 1.4)],
        development_time_ratio_truth=15.0,
    )
    cell = bakeoff.ReplayCell(
        slug=_OK_SLUG,
        tier="baseline-n8n",
        prompt_version="v3",
        latency_risk=False,
        scores=[score],
        samples={"live-roast-2026-06-07/session-1": []},
    )
    report = bakeoff.render_replay_report([cell], bakeoff.REPLAY_ROASTS)

    assert "agreement with a known-good roast" in report
    assert "not* a provably optimal" in report or "not a provably optimal" in report
    assert "drop F1=1.0" in report
    assert "winner" not in report.lower()
    assert "NO auto-pick" in report


@pytest.mark.asyncio
async def test_run_replay_bakeoff_drops_unavailable_then_scores(
    mock_healthcheck: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay run sweeps first (dropping a 404), then scores only survivors."""

    async def fake_reco(self: PydanticAIAdvisor, context: AdvisorContext) -> RoastDecision:
        return await _canned_recommend(context)

    monkeypatch.setattr(PydanticAIAdvisor, "get_recommendation", fake_reco)
    roster = (
        bakeoff.Candidate(_DEAD_SLUG, bakeoff.Tier.CONTROL_CANDIDATE, (RoastPhase.DEVELOPMENT,)),
        bakeoff.Candidate(_OK_SLUG, bakeoff.Tier.BASELINE, bakeoff.PHASE_ORDER),
    )
    availability, cells = await bakeoff.run_replay_bakeoff(
        roster, bakeoff.REPLAY_ROASTS, ["v2"], None, 60.0
    )

    assert {a.slug for a in availability} == {_DEAD_SLUG, _OK_SLUG}
    assert [c.slug for c in cells] == [_OK_SLUG]
    # The artifact serialises with scores + samples.
    rows = bakeoff.replay_cells_to_json(cells)
    assert rows[0]["slug"] == _OK_SLUG
    assert rows[0]["scores"]
    assert rows[0]["samples"]
