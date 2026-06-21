"""Tests for the bake-off concurrent-replay layer (#281).

Hardware- and network-free (the M1 guardrail): every test drives the replay
pipeline through the canned-recommender seam — the real OpenRouter path and
``OPENROUTER_API_KEY`` are never touched. They assert the #281 behaviour layered
on top of the unchanged #280 scoring + checkpoint + cost-guard path:

- the in-flight concurrency cap is respected (a tracking recommender records the
  peak parallelism, which never exceeds ``--concurrency``);
- a stub returning 429 / 5xx triggers exponential backoff + retry and eventually
  succeeds, honouring ``Retry-After``; a non-retryable error is NOT retried;
- concurrent results are order-independent — a concurrent run's scorecard,
  checkpoint, and capture equal the serial run's;
- the budget stop is concurrency-safe — the reserved/accounted calls never
  overshoot ``--max-spend`` under fan-out.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import advisor_bakeoff as bakeoff  # noqa: E402
from advisor_bakeoff import Candidate, RetryPolicy, Tier  # noqa: E402

from roastpilot_agent.advisor import (  # noqa: E402
    AdvisorContext,
    AdvisorProviderError,
    PydanticAIAdvisor,
    RoastDecision,
)
from roastpilot_agent.models import AdvisorHealth, AdvisorHealthStatus, RoastPhase  # noqa: E402

_SLUG_A = "anthropic/claude-opus-4.8"
_SLUG_B = "google/gemini-3.5-flash"


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


def _canned_factory(_cand: Candidate, _pv: str) -> Any:
    """A (candidate, prompt) -> plain recommender factory (the key-free seam)."""
    return _canned_recommend


@pytest.fixture
def mock_healthcheck(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``healthcheck`` so every roster slug resolves (no network)."""

    async def fake_healthcheck(self: PydanticAIAdvisor) -> AdvisorHealth:
        return AdvisorHealth(status=AdvisorHealthStatus.REACHABLE, model_slug="x", error=None)

    monkeypatch.setattr(PydanticAIAdvisor, "healthcheck", fake_healthcheck)


def _roster() -> tuple[Candidate, ...]:
    """A two-slug roster both measured in every phase."""
    return (
        Candidate(_SLUG_A, Tier.BASELINE, bakeoff.PHASE_ORDER),
        Candidate(_SLUG_B, Tier.CONTROL_CANDIDATE, (RoastPhase.DEVELOPMENT,)),
    )


def _fixed_clock() -> Any:
    """A monotonic clock advancing a fixed step per call (deterministic latency)."""
    state = {"t": 0.0}

    def clock() -> float:
        state["t"] += 0.5
        return state["t"]

    return clock


async def _instant_sleep(_seconds: float) -> None:
    """A no-op async sleep so backoff tests stay instant."""
    return None


# --- Error classification (the backoff gate) --------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "ModelAPIError: status_code: 429 Too Many Requests",
        "status_code: 500 internal error",
        "http 503 service unavailable",
        "connection reset by peer",
        "request timed out",
        "provider overloaded, retry",
    ],
)
def test_is_retryable_error_true_for_transient(message: str) -> None:
    """429 / 5xx / network-transient messages are classified retryable."""
    assert bakeoff.is_retryable_error(message) is True


@pytest.mark.parametrize(
    "message",
    [
        None,
        "",
        "status_code: 400 invalid request",
        "status_code: 401 unauthorized",
        "status_code: 404 model not found",
        "AdvisorMalformedOutputError: could not parse",
    ],
)
def test_is_retryable_error_false_for_permanent(message: str | None) -> None:
    """4xx (non-429) / parse failures are NOT retried (no wasted budget)."""
    assert bakeoff.is_retryable_error(message) is False


def test_parse_retry_after_seconds() -> None:
    """A numeric Retry-After is extracted; absent / non-numeric yields None."""
    assert bakeoff.parse_retry_after_seconds("429, Retry-After: 7") == 7.0
    assert bakeoff.parse_retry_after_seconds("retry-after=2.5 please") == 2.5
    assert bakeoff.parse_retry_after_seconds("status_code: 429") is None
    assert bakeoff.parse_retry_after_seconds(None) is None


def test_retry_policy_honours_retry_after_over_backoff() -> None:
    """A provider Retry-After wins over the computed exponential delay."""
    policy = RetryPolicy(base_seconds=0.5, max_seconds=30.0, rng=lambda: 1.0)
    assert policy.delay_for(attempt=1, retry_after=9.0) == 9.0
    # No Retry-After → full-jitter exponential (rng=1.0 → the full ceiling).
    assert policy.delay_for(attempt=1, retry_after=None) == 0.5
    assert policy.delay_for(attempt=2, retry_after=None) == 1.0
    # Capped at max_seconds.
    assert policy.delay_for(attempt=20, retry_after=None) == 30.0


# --- with_retry: backoff + retry → eventual success -------------------------


@pytest.mark.asyncio
async def test_with_retry_recovers_after_429_then_succeeds() -> None:
    """A stub failing twice with 429 is retried with backoff and then succeeds."""
    attempts = {"n": 0}
    slept: list[float] = []

    async def flaky(_context: AdvisorContext) -> tuple[RoastDecision, str | None]:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise AdvisorProviderError("status_code: 429 Too Many Requests, Retry-After: 4")
        return (
            RoastDecision(
                target_heat=50,
                target_fan=40,
                should_drop=False,
                confidence=0.7,
                rationale="ok",
            ),
            None,
        )

    async def recording_sleep(seconds: float) -> None:
        slept.append(seconds)

    policy = RetryPolicy(attempts=4, rng=lambda: 0.0)
    wrapped = bakeoff.with_retry(flaky, policy, sleep=recording_sleep)
    context = bakeoff.build_phase_context(bakeoff.REPLAY_ROASTS[0], RoastPhase.DEVELOPMENT)[0]

    decision, reasoning = await wrapped(context)

    assert attempts["n"] == 3, "two 429s then success = three attempts"
    assert decision.target_heat == 50
    assert reasoning is None
    # Both backoffs honoured the Retry-After of 4 s (not the exponential value).
    assert slept == [4.0, 4.0]


@pytest.mark.asyncio
async def test_with_retry_reraises_non_retryable_immediately() -> None:
    """A 400 (non-retryable) is raised on the first attempt — never retried."""
    attempts = {"n": 0}

    async def bad_request(_context: AdvisorContext) -> tuple[RoastDecision, str | None]:
        attempts["n"] += 1
        raise AdvisorProviderError("status_code: 400 invalid request")

    wrapped = bakeoff.with_retry(bad_request, RetryPolicy(attempts=4), sleep=_instant_sleep)
    context = bakeoff.build_phase_context(bakeoff.REPLAY_ROASTS[0], RoastPhase.DEVELOPMENT)[0]

    with pytest.raises(AdvisorProviderError, match="400"):
        await wrapped(context)
    assert attempts["n"] == 1, "a permanent error must not be retried"


@pytest.mark.asyncio
async def test_with_retry_exhausts_attempts_then_reraises() -> None:
    """A persistently-429 call re-raises after the attempt budget is spent."""
    attempts = {"n": 0}

    async def always_429(_context: AdvisorContext) -> tuple[RoastDecision, str | None]:
        attempts["n"] += 1
        raise AdvisorProviderError("status_code: 429 rate limit")

    wrapped = bakeoff.with_retry(always_429, RetryPolicy(attempts=3), sleep=_instant_sleep)
    context = bakeoff.build_phase_context(bakeoff.REPLAY_ROASTS[0], RoastPhase.DEVELOPMENT)[0]

    with pytest.raises(AdvisorProviderError, match="429"):
        await wrapped(context)
    assert attempts["n"] == 3, "all attempts spent before the final re-raise"


# --- Concurrency cap respected ----------------------------------------------


def _tracking_factory(peak: dict[str, int], inflight: dict[str, int]) -> Any:
    """Build a factory whose recommender records the peak in-flight parallelism."""

    async def recommend(context: AdvisorContext) -> RoastDecision:
        inflight["n"] += 1
        peak["max"] = max(peak["max"], inflight["n"])
        # Yield so other scheduled cells can interleave (exercises real overlap).
        await asyncio.sleep(0)
        inflight["n"] -= 1
        return await _canned_recommend(context)

    def factory(_cand: Candidate, _pv: str) -> Any:
        return recommend

    return factory


@pytest.mark.asyncio
async def test_concurrency_cap_is_respected(mock_healthcheck: None, tmp_path: Path) -> None:
    """Peak in-flight cells never exceeds --concurrency, and fan-out happens."""
    peak = {"max": 0}
    inflight = {"n": 0}
    result = await bakeoff.run_replay_bakeoff_observable(
        _roster(),
        bakeoff.REPLAY_ROASTS,
        ["v2"],
        None,
        60.0,
        out=tmp_path / "bakeoff.json",
        concurrency=2,
        recommender_factory=_tracking_factory(peak, inflight),
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
    )
    assert result.fresh_cells == len(_roster()) * len(bakeoff.REPLAY_ROASTS)
    assert peak["max"] >= 2, "cells actually ran concurrently (fan-out happened)"
    assert peak["max"] <= 2, "in-flight cells never exceeded the cap"


@pytest.mark.asyncio
async def test_concurrency_below_one_rejected() -> None:
    """A concurrency below 1 is rejected (the bound must be sane)."""
    with pytest.raises(ValueError, match="concurrency must be >= 1"):
        await bakeoff.run_replay_bakeoff_observable(
            _roster(),
            bakeoff.REPLAY_ROASTS,
            ["v2"],
            None,
            60.0,
            out=Path("/tmp/unused.json"),
            concurrency=0,
            recommender_factory=_canned_factory,
        )


# --- Order independence: concurrent == serial -------------------------------


@pytest.mark.asyncio
async def test_concurrent_matches_serial(mock_healthcheck: None, tmp_path: Path) -> None:
    """A concurrent run's scorecard + capture equal the serial run's (#281)."""
    roster = _roster()
    roasts = bakeoff.REPLAY_ROASTS

    serial = await bakeoff.run_replay_bakeoff_observable(
        roster,
        roasts,
        ["v2", "v3"],
        None,
        60.0,
        out=tmp_path / "serial.json",
        concurrency=1,
        recommender_factory=_canned_factory,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
    )
    concurrent = await bakeoff.run_replay_bakeoff_observable(
        roster,
        roasts,
        ["v2", "v3"],
        None,
        60.0,
        out=tmp_path / "concurrent.json",
        concurrency=8,
        recommender_factory=_canned_factory,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
    )

    assert concurrent.fresh_cells == serial.fresh_cells
    # The assembled scorecard is identical (cells are assembled in grid order
    # regardless of completion order).
    assert bakeoff.replay_cells_to_json(concurrent.cells) == bakeoff.replay_cells_to_json(
        serial.cells
    )

    # The capture set is the same (order-independent comparison by cell + tick).
    assert sorted(
        (bakeoff.captured_call_to_json(c) for c in concurrent.captured_calls),
        key=lambda r: (r["model_slug"], r["prompt_version"], r["roast_id"], r["tick_index"]),
    ) == sorted(
        (bakeoff.captured_call_to_json(c) for c in serial.captured_calls),
        key=lambda r: (r["model_slug"], r["prompt_version"], r["roast_id"], r["tick_index"]),
    )
    # The checkpoint sidecars carry the same set of cell keys.
    assert _sidecar_keys(tmp_path / "concurrent.json") == _sidecar_keys(tmp_path / "serial.json")


def _sidecar_keys(out: Path) -> set[tuple[str, str, str]]:
    """The set of (slug, prompt, roast) keys written to a run's sidecar."""
    import json

    path = bakeoff.sidecar_path(out)
    keys: set[tuple[str, str, str]] = set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        keys.add((rec["model_slug"], rec["prompt_version"], rec["roast_id"]))
    return keys


@pytest.mark.asyncio
async def test_concurrent_resume_matches_all_at_once(
    mock_healthcheck: None, tmp_path: Path
) -> None:
    """A concurrent run resumes from a partial sidecar and matches all-at-once."""
    roster = _roster()
    roasts = bakeoff.REPLAY_ROASTS
    out = tmp_path / "bakeoff.json"

    # Pre-FC-inclusive so the budget trips mid-run (the denser tick set the budget
    # math assumes); all three runs share the scope so resume stays consistent.
    reference = await bakeoff.run_replay_bakeoff_observable(
        roster,
        roasts,
        ["v2"],
        None,
        60.0,
        out=tmp_path / "ref.json",
        recommender_factory=_canned_factory,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
        include_pre_fc=True,
    )

    # Partial run capped by budget, concurrent.
    partial = await bakeoff.run_replay_bakeoff_observable(
        roster,
        roasts,
        ["v2"],
        None,
        60.0,
        out=out,
        concurrency=4,
        cost_per_call=1.0,
        max_spend=30.0,
        recommender_factory=_canned_factory,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
        include_pre_fc=True,
    )
    assert partial.stopped_for_budget is True
    assert 0 < partial.fresh_cells < len(roster) * len(roasts)

    # Resume concurrently → equals the all-at-once reference.
    resumed = await bakeoff.run_replay_bakeoff_observable(
        roster,
        roasts,
        ["v2"],
        None,
        60.0,
        out=out,
        concurrency=4,
        recommender_factory=_canned_factory,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
        include_pre_fc=True,
    )
    assert resumed.resumed_cells == partial.fresh_cells
    assert resumed.stopped_for_budget is False
    assert bakeoff.replay_cells_to_json(resumed.cells) == bakeoff.replay_cells_to_json(
        reference.cells
    )


# --- Concurrency-safe budget stop -------------------------------------------


@pytest.mark.asyncio
async def test_budget_stop_is_concurrency_safe(mock_healthcheck: None, tmp_path: Path) -> None:
    """Under fan-out the accounted spend never overshoots --max-spend.

    The reservation is atomic: cells reserve their call budget before running, so
    once the cap is reached no further cell is scheduled. The total reserved /
    accounted calls can exceed the budget only by the cells already in flight when
    the cap was hit — and here every cell costs the same, so a budget that affords
    only some cells must stop with the persisted cells' total within the cap.
    """
    roster = _roster()
    roasts = bakeoff.REPLAY_ROASTS
    out = tmp_path / "bakeoff.json"

    # Pre-FC-inclusive so each cell is ~22 calls and the $30 budget trips mid-run
    # (the development-only default is too cheap to exercise the overshoot bound).
    result = await bakeoff.run_replay_bakeoff_observable(
        roster,
        roasts,
        ["v2"],
        None,
        60.0,
        out=out,
        concurrency=8,
        cost_per_call=1.0,
        max_spend=30.0,
        recommender_factory=_canned_factory,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
        include_pre_fc=True,
    )

    assert result.stopped_for_budget is True
    # Sum the persisted cells' call counts: the cost guard must not have scheduled
    # past the budget. Each cell's reservation was made while there was headroom,
    # so the running total never exceeds the cap.
    import json

    total_calls = 0
    for line in bakeoff.sidecar_path(out).read_text().splitlines():
        if not line.strip():
            continue
        total_calls += int(json.loads(line)["call_count"])
    assert total_calls <= 30, "the concurrency-safe budget never overshot --max-spend"
    assert result.fresh_cells > 0, "the run made progress before stopping"


@pytest.mark.asyncio
async def test_concurrent_run_applies_retry_under_fan_out(
    mock_healthcheck: None, tmp_path: Path
) -> None:
    """A flaky recommender under concurrency recovers via backoff (no failed cells)."""
    state = {"first": True}
    lock = asyncio.Lock()

    async def flaky(context: AdvisorContext) -> tuple[RoastDecision, str | None]:
        # Fail exactly once across the whole run, then always succeed — proves the
        # retry path is wired through the concurrent orchestrator.
        async with lock:
            if state["first"]:
                state["first"] = False
                raise AdvisorProviderError("status_code: 503 service unavailable")
        return await _canned_recommend(context), None

    def factory(_cand: Candidate, _pv: str) -> Any:
        return flaky

    result = await bakeoff.run_replay_bakeoff_observable(
        _roster(),
        bakeoff.REPLAY_ROASTS,
        ["v2"],
        None,
        60.0,
        out=tmp_path / "bakeoff.json",
        concurrency=4,
        retry_policy=RetryPolicy(attempts=4, rng=lambda: 0.0),
        retry_sleep=_instant_sleep,
        reasoning_recommender_factory=factory,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
    )

    # Every tick produced a decision — the single transient 503 was retried away.
    assert all(c.decision is not None for c in result.captured_calls)
    assert result.fresh_cells == len(_roster()) * len(bakeoff.REPLAY_ROASTS)


# --- Within-cell roast/tick ordering under concurrency (issue 1) ------------


def _ordered_cell_signature(cells: list[bakeoff.ReplayCell]) -> list[tuple[str, str, list[str]]]:
    """For each cell, the (slug, prompt, [roast_name in scores order]) signature.

    Surfaces the WITHIN-CELL roast ordering specifically: a cell whose per-roast
    scores were assembled in completion order rather than roast order would show
    a reordered ``roast_name`` list here.
    """
    return [(c.slug, c.prompt_version, [s.roast_name for s in c.scores]) for c in cells]


@pytest.mark.asyncio
async def test_within_cell_roast_order_is_deterministic_under_concurrency(
    mock_healthcheck: None, tmp_path: Path
) -> None:
    """A cell's per-roast scores stay in ROAST order even when roasts finish out of order.

    The first roast is made the SLOWER one (its recommender yields the event loop
    repeatedly) so that, under ``--concurrency > 1``, the second roast's cell
    completes first. If assembly used completion order the scores would be
    reordered; slotting by roast index keeps them in fixture order, byte-identical
    to the serial run.
    """
    roster = _roster()
    roasts = bakeoff.REPLAY_ROASTS
    first_rid = bakeoff.roast_id_for(roasts[0])

    def skewing_factory(_cand: Candidate, _pv: str) -> Any:
        async def recommend(context: AdvisorContext) -> RoastDecision:
            # The first roast's profile_name carries its id; slow it down so the
            # SECOND roast completes first under concurrency (forces out-of-order
            # completion — the exact condition that would corrupt append-order).
            if context.profile_name == first_rid:
                for _ in range(5):
                    await asyncio.sleep(0)
            return await _canned_recommend(context)

        return recommend

    serial = await bakeoff.run_replay_bakeoff_observable(
        roster,
        roasts,
        ["v2"],
        None,
        60.0,
        out=tmp_path / "serial.json",
        concurrency=1,
        recommender_factory=skewing_factory,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
    )
    concurrent = await bakeoff.run_replay_bakeoff_observable(
        roster,
        roasts,
        ["v2"],
        None,
        60.0,
        out=tmp_path / "concurrent.json",
        concurrency=8,
        recommender_factory=skewing_factory,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
    )

    serial_sig = _ordered_cell_signature(serial.cells)
    concurrent_sig = _ordered_cell_signature(concurrent.cells)
    # Every cell's scores are in fixture (roast 0, roast 1) order in BOTH runs.
    expected_order = [bakeoff.roast_id_for(r) for r in roasts]
    for _slug, _pv, names in concurrent_sig:
        assert names == expected_order, "per-cell scores must stay in roast order"
    # And the concurrent within-cell ordering is byte-identical to serial.
    assert concurrent_sig == serial_sig


# --- Budget counts ACTUAL provider requests, including retries (issue 2) ----


@pytest.mark.asyncio
async def test_budget_counts_each_retry_attempt(mock_healthcheck: None, tmp_path: Path) -> None:
    """A run whose every call retries twice accounts 3 requests per tick, not 1 (#281).

    The cost guard must account ACTUAL provider requests (every attempt), not
    one-per-tick: ``with_retry``'s ``on_attempt`` hook increments the guard per
    request and the cell's per-tick reservation is released, so each request is
    counted exactly once. A single-roast, single-candidate run makes the count
    exact — ``accounted_calls == 3 * ticks`` when every tick fails twice then
    succeeds, versus ``ticks`` if retries were (wrongly) uncounted.
    """
    roster = (Candidate(_SLUG_A, Tier.BASELINE, bakeoff.PHASE_ORDER),)
    roasts = (bakeoff.REPLAY_ROASTS[0],)
    # Count ALL ticks: this retry-accounting mechanics test runs pre-FC-inclusive
    # so the per-tick request count is exact over the full tick set.
    ticks, _ground = bakeoff.build_ticks(roasts[0], cadence_seconds=60.0)
    n_ticks = len(ticks)

    counter = {"n": 0}

    async def two_429s_then_ok(context: AdvisorContext) -> tuple[RoastDecision, str | None]:
        # Each tick fails twice then succeeds → exactly 3 provider requests/tick.
        counter["n"] += 1
        if counter["n"] % 3 != 0:
            raise AdvisorProviderError("status_code: 429 rate limit")
        return await _canned_recommend(context), None

    def factory(_cand: Candidate, _pv: str) -> Any:
        return two_429s_then_ok

    result = await bakeoff.run_replay_bakeoff_observable(
        roster,
        roasts,
        ["v2"],
        None,
        60.0,
        out=tmp_path / "bakeoff.json",
        concurrency=1,
        cost_per_call=1.0,
        retry_policy=RetryPolicy(attempts=4, rng=lambda: 0.0),
        retry_sleep=_instant_sleep,
        reasoning_recommender_factory=factory,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
        include_pre_fc=True,
    )

    assert result.fresh_cells == 1
    assert counter["n"] == 3 * n_ticks, "every tick issued three provider requests"
    # The budget counted every request — NOT one-per-tick. This is the issue-2 fix.
    assert result.accounted_calls == 3 * n_ticks
    assert result.accounted_calls != n_ticks


@pytest.mark.asyncio
async def test_run_budget_stops_accounting_actual_retried_requests(
    mock_healthcheck: None, tmp_path: Path
) -> None:
    """An always-retrying run under a budget stops, having counted actual requests.

    Each tick costs the full retry attempt count, so spend climbs faster than
    one-per-tick. The run must still stop gracefully on ``--max-spend`` (it never
    silently keeps spending past the cap because retries were uncounted), and the
    accounted requests reflect the retries.
    """
    fail_until = {"n": 0}

    async def two_429s_then_ok(context: AdvisorContext) -> tuple[RoastDecision, str | None]:
        # Each call fails twice then succeeds → 3 provider requests per tick.
        fail_until["n"] += 1
        if fail_until["n"] % 3 != 0:
            raise AdvisorProviderError("status_code: 429 rate limit")
        return await _canned_recommend(context), None

    def factory(_cand: Candidate, _pv: str) -> Any:
        return two_429s_then_ok

    # Pre-FC-inclusive so the retry-inflated spend trips the $30 budget mid-run.
    result = await bakeoff.run_replay_bakeoff_observable(
        _roster(),
        bakeoff.REPLAY_ROASTS,
        ["v2"],
        None,
        60.0,
        out=tmp_path / "bakeoff.json",
        concurrency=4,
        cost_per_call=1.0,
        max_spend=30.0,
        retry_policy=RetryPolicy(attempts=4, rng=lambda: 0.0),
        retry_sleep=_instant_sleep,
        reasoning_recommender_factory=factory,
        clock=_fixed_clock(),
        heartbeat_clock=_fixed_clock(),
        include_pre_fc=True,
    )

    assert result.stopped_for_budget is True
    assert result.fresh_cells > 0
    # Retried requests were counted (each completed tick cost ~3 requests), so the
    # accounted total is well above the completed-tick count — the bound is honest.
    completed_ticks = sum(1 for c in result.captured_calls if c.decision is not None)
    assert result.accounted_calls >= 3 * completed_ticks
