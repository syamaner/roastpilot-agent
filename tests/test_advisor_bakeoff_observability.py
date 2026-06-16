"""Tests for the bake-off observability / checkpoint / cost-guard layer (#280).

Hardware- and network-free (the M1 guardrail): every test drives the replay
pipeline through the canned-recommender seam (``recommender_factory``) — the
real OpenRouter path and ``OPENROUTER_API_KEY`` are never touched. They assert
the operator-facing behaviour #280 adds on top of the unchanged scoring math:

- a completed cell is persisted to the sidecar JSONL immediately (incremental
  output) keyed by ``(model_slug, prompt_version, roast_id)``;
- a re-run RESUMES — completed cells are skipped, only the remainder runs, and
  the final scorecard equals the all-at-once result;
- a simulated mid-run kill leaves a VALID PARTIAL scorecard on disk;
- ``--max-spend`` stops GRACEFULLY before the budget with partials flushed, and
  a budget above the projected total runs fully.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import advisor_bakeoff as bakeoff  # noqa: E402
from advisor_bakeoff import Candidate, Tier  # noqa: E402

from roastpilot_agent.advisor import AdvisorContext, PydanticAIAdvisor, RoastDecision  # noqa: E402
from roastpilot_agent.models import AdvisorHealth, AdvisorHealthStatus, RoastPhase  # noqa: E402

_SLUG_A = "anthropic/claude-opus-4.8"
_SLUG_B = "google/gemini-3.5-flash"


# --- Canned recommender seam (no key, no network) ---------------------------


async def _canned_recommend(context: AdvisorContext) -> RoastDecision:
    """Deterministic recommender: cut heat post-FC, drop near the real drop temp."""
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


def _canned_factory(_cand: Candidate, _prompt_version: str) -> Any:
    """A ``recommender_factory`` that returns the canned recommender for any cell."""
    return _canned_recommend


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
        Candidate(_SLUG_A, Tier.INCUMBENT, bakeoff.PHASE_ORDER),
        Candidate(_SLUG_B, Tier.ULTRA_FLASH, (RoastPhase.DEVELOPMENT,)),
    )


def _fixed_clock() -> Any:
    """A monotonic clock that advances a fixed step per call (deterministic latency)."""
    state = {"t": 0.0}

    def clock() -> float:
        state["t"] += 0.5
        return state["t"]

    return clock


# --- Sidecar serialisation round-trip ---------------------------------------


def test_roast_replay_record_round_trips_through_the_scorers() -> None:
    """A persisted replay reloads to identical derived metrics (no model calls)."""
    fixture = bakeoff.REPLAY_ROASTS[0]
    rid = bakeoff.roast_id_for(fixture)
    ticks, ground = bakeoff.build_ticks(fixture, cadence_seconds=60.0)
    outcomes = [
        bakeoff.TickOutcome(
            tick=t,
            decision=RoastDecision(
                target_heat=50,
                target_fan=40,
                should_drop=False,
                confidence=0.7,
                rationale="hold",
            ),
            latency_seconds=0.5,
        )
        for t in ticks
    ]
    original = bakeoff.build_roast_replay(_SLUG_A, "v2", rid, outcomes, ground)

    record = bakeoff.roast_replay_to_record(original)
    reloaded = bakeoff.roast_replay_from_record(record, ticks, ground)

    assert reloaded.score == original.score
    assert reloaded.trajectory == original.trajectory
    assert reloaded.call_count == original.call_count == len(ticks)


def test_roast_replay_from_record_rejects_tick_count_mismatch() -> None:
    """A checkpoint whose outcome count drifts from the rebuilt ticks is rejected."""
    fixture = bakeoff.REPLAY_ROASTS[0]
    rid = bakeoff.roast_id_for(fixture)
    ticks, ground = bakeoff.build_ticks(fixture, cadence_seconds=60.0)
    record = {
        "model_slug": _SLUG_A,
        "prompt_version": "v2",
        "roast_id": rid,
        "call_count": 1,
        "outcomes": [{"decision": None, "latency_seconds": None, "error": "x"}],
    }
    with pytest.raises(ValueError, match="re-run from scratch"):
        bakeoff.roast_replay_from_record(record, ticks, ground)


def test_checkpoint_appends_and_reloads(tmp_path: Path) -> None:
    """A completed replay is written to the sidecar and reloaded last-write-wins."""
    sidecar = tmp_path / "bakeoff.json.cells.jsonl"
    fixture = bakeoff.REPLAY_ROASTS[0]
    rid = bakeoff.roast_id_for(fixture)
    ticks, ground = bakeoff.build_ticks(fixture, cadence_seconds=60.0)
    outcomes = [
        bakeoff.TickOutcome(tick=t, decision=None, latency_seconds=0.1, error="boom") for t in ticks
    ]
    replay = bakeoff.build_roast_replay(_SLUG_A, "v2", rid, outcomes, ground)

    cp = bakeoff.Checkpoint(sidecar, resume=True)
    assert cp.completed_count() == 0
    cp.append(replay)

    # The line is on disk immediately (kill-safe).
    lines = [ln for ln in sidecar.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1

    reopened = bakeoff.Checkpoint(sidecar, resume=True)
    key = bakeoff.cell_key(_SLUG_A, "v2", rid)
    assert reopened.has(key)
    assert reopened.completed_count() == 1


def test_checkpoint_load_tolerates_blank_lines(tmp_path: Path) -> None:
    """A sidecar with stray blank lines (e.g. a partial flush) still loads."""
    sidecar = tmp_path / "bakeoff.json.cells.jsonl"
    record = json.dumps(
        {
            "model_slug": _SLUG_A,
            "prompt_version": "v2",
            "roast_id": "live-roast-2026-06-07/session-1",
            "call_count": 3,
            "outcomes": [],
        }
    )
    sidecar.write_text(f"\n{record}\n\n")
    cp = bakeoff.Checkpoint(sidecar, resume=True)
    assert cp.completed_count() == 1
    assert cp.has(bakeoff.cell_key(_SLUG_A, "v2", "live-roast-2026-06-07/session-1"))


def test_checkpoint_no_resume_truncates(tmp_path: Path) -> None:
    """``resume=False`` discards a pre-existing sidecar so the run starts clean."""
    sidecar = tmp_path / "bakeoff.json.cells.jsonl"
    sidecar.write_text(
        json.dumps(
            {
                "model_slug": _SLUG_A,
                "prompt_version": "v2",
                "roast_id": "x/y",
                "call_count": 1,
                "outcomes": [],
            }
        )
        + "\n"
    )
    cp = bakeoff.Checkpoint(sidecar, resume=False)
    assert cp.completed_count() == 0
    assert not sidecar.exists()


# --- Checkpoint round-trip: kill → resume → equals all-at-once ---------------


@pytest.mark.asyncio
async def test_checkpoint_resume_skips_completed_and_matches_all_at_once(
    mock_healthcheck: None, tmp_path: Path
) -> None:
    """Kill after K cells, re-run: skips the done cells; final == all-at-once."""
    out = tmp_path / "bakeoff.json"
    roster = _roster()
    roasts = bakeoff.REPLAY_ROASTS

    # 1) A clean all-at-once run for the reference scorecard.
    ref_out = tmp_path / "ref.json"
    reference = await bakeoff.run_replay_bakeoff_observable(
        roster,
        roasts,
        ["v2"],
        None,
        60.0,
        out=ref_out,
        recommender_factory=_canned_factory,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
    )
    assert reference.fresh_cells == len(roster) * len(roasts)
    assert reference.resumed_cells == 0

    # 2) Simulate a kill after K cells via a budget that only affords K of them.
    #    Each cell costs (ticks * cost_per_call); the first roast cell is ~22
    #    calls, so a $30 budget at $1/call affords the first cell but stops before
    #    the second (which would project past $30) — a deterministic mid-run kill.
    k_budget = await bakeoff.run_replay_bakeoff_observable(
        roster,
        roasts,
        ["v2"],
        None,
        60.0,
        out=out,
        recommender_factory=_canned_factory,
        cost_per_call=1.0,
        max_spend=30.0,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
    )
    assert k_budget.stopped_for_budget is True
    completed_after_kill = k_budget.fresh_cells
    assert 0 < completed_after_kill < len(roster) * len(roasts)
    sidecar = bakeoff.sidecar_path(out)
    on_disk = [ln for ln in sidecar.read_text().splitlines() if ln.strip()]
    assert len(on_disk) == completed_after_kill

    # 3) Re-run with NO budget → resumes: the already-done cells are skipped,
    #    only the remainder is freshly computed, and nothing re-pays.
    resumed = await bakeoff.run_replay_bakeoff_observable(
        roster,
        roasts,
        ["v2"],
        None,
        60.0,
        out=out,
        recommender_factory=_canned_factory,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
    )
    assert resumed.resumed_cells == completed_after_kill
    assert resumed.fresh_cells == len(roster) * len(roasts) - completed_after_kill
    assert resumed.stopped_for_budget is False

    # The resumed final scorecard equals the all-at-once reference (the scoring
    # math is unchanged — derived identically from the same outcomes).
    assert bakeoff.replay_cells_to_json(resumed.cells) == bakeoff.replay_cells_to_json(
        reference.cells
    )


@pytest.mark.asyncio
async def test_resume_does_not_repay_for_finished_cells(
    mock_healthcheck: None, tmp_path: Path
) -> None:
    """A second full re-run skips every cell — the recommender is never called."""
    out = tmp_path / "bakeoff.json"
    roster = _roster()

    await bakeoff.run_replay_bakeoff_observable(
        roster,
        bakeoff.REPLAY_ROASTS,
        ["v2"],
        None,
        60.0,
        out=out,
        recommender_factory=_canned_factory,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
    )

    calls = {"n": 0}

    async def counting_recommend(context: AdvisorContext) -> RoastDecision:
        calls["n"] += 1
        return await _canned_recommend(context)

    def counting_factory(_cand: Candidate, _pv: str) -> Any:
        return counting_recommend

    second = await bakeoff.run_replay_bakeoff_observable(
        roster,
        bakeoff.REPLAY_ROASTS,
        ["v2"],
        None,
        60.0,
        out=out,
        recommender_factory=counting_factory,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
    )
    assert second.fresh_cells == 0
    assert second.resumed_cells == len(roster) * len(bakeoff.REPLAY_ROASTS)
    assert calls["n"] == 0, "a fully-resumed run must not re-pay for any cell"


# --- Partial-on-kill: a valid partial scorecard renders ----------------------


@pytest.mark.asyncio
async def test_partial_scorecard_renders_after_simulated_kill(
    mock_healthcheck: None, tmp_path: Path
) -> None:
    """A mid-run stop leaves cells that render a valid (partial) markdown report."""
    out = tmp_path / "bakeoff.json"
    result = await bakeoff.run_replay_bakeoff_observable(
        _roster(),
        bakeoff.REPLAY_ROASTS,
        ["v2"],
        None,
        60.0,
        out=out,
        recommender_factory=_canned_factory,
        cost_per_call=1.0,
        max_spend=30.0,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
    )
    assert result.stopped_for_budget is True
    assert result.cells, "a partial run still yields reportable cells"

    report = bakeoff.render_replay_report(result.cells, bakeoff.REPLAY_ROASTS)
    assert "real-roast replay scorecard" in report
    assert "agreement with a known-good roast" in report
    # The JSON artifact serialises the partial cells without error.
    rows = bakeoff.replay_cells_to_json(result.cells)
    assert rows and all("scores" in r for r in rows)


# --- Cost guard --------------------------------------------------------------


def test_cost_guard_accounts_and_trips_before_budget() -> None:
    """The guard estimates spend and refuses a cell that would breach the budget."""
    guard = bakeoff.CostGuard(cost_per_call=0.02, max_spend=0.10)
    assert guard.spend == 0.0
    assert guard.would_exceed(5) is False  # 5 * 0.02 = 0.10, exactly at budget
    guard.add_calls(5)
    assert guard.spend == pytest.approx(0.10)
    # One more call (0.12) would breach 0.10.
    assert guard.would_exceed(1) is True


def test_cost_guard_unbounded_never_trips() -> None:
    """With no budget the guard never stops a cell."""
    guard = bakeoff.CostGuard(cost_per_call=99.0, max_spend=None)
    guard.add_calls(1000)
    assert guard.would_exceed(1000) is False


@pytest.mark.asyncio
async def test_max_spend_above_total_runs_fully(mock_healthcheck: None, tmp_path: Path) -> None:
    """A budget above the projected total completes every cell (no early stop)."""
    out = tmp_path / "bakeoff.json"
    roster = _roster()
    result = await bakeoff.run_replay_bakeoff_observable(
        roster,
        bakeoff.REPLAY_ROASTS,
        ["v2"],
        None,
        60.0,
        out=out,
        recommender_factory=_canned_factory,
        cost_per_call=0.001,
        max_spend=1_000.0,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
    )
    assert result.stopped_for_budget is False
    assert result.fresh_cells == len(roster) * len(bakeoff.REPLAY_ROASTS)


# --- Heartbeat ---------------------------------------------------------------


def test_heartbeat_emits_on_interval_and_force() -> None:
    """The heartbeat respects its interval and the forced (start/end) beats."""
    ticks = {"t": 0.0}

    def clock() -> float:
        return ticks["t"]

    lines: list[str] = []
    hb = bakeoff.Heartbeat(total_cells=4, interval_seconds=30.0, clock=clock, emit=lines.append)
    guard = bakeoff.CostGuard(cost_per_call=0.02, max_spend=0.50)
    guard.add_calls(3)

    hb.maybe_beat(done=1, guard=guard, force=True)  # forced start beat
    hb.maybe_beat(done=2, guard=guard)  # too soon — suppressed
    ticks["t"] = 31.0
    hb.maybe_beat(done=3, guard=guard)  # interval elapsed — emits

    assert len(lines) == 2
    assert "cells=1/4" in lines[0]
    assert "spend=$0.06/0.50" in lines[0]
    assert "cells=3/4" in lines[1]
