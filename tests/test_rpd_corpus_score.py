"""Tests for the RP-D joint-objective offline corpus scorer (#711, D124, PR-D2).

Hardware-free: every run is built directly in a temp SQLite
:class:`~roastpilot_agent.store.RoastStore` (the same real write paths
``test_store.py`` uses), then scored with the real, pure
:func:`bakeoff_replay.joint_window_score`. No LLM, no network, no operator
store is ever touched.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Literal

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import rpd_corpus_score as scorer  # noqa: E402

from roastpilot_agent.config import AppConfig  # noqa: E402
from roastpilot_agent.models import (  # noqa: E402
    DropReason,
    RoastCommand,
    RoastEventKind,
    RoastEventSource,
    RoastPhase,
    RoastProfile,
    RoastTelemetry,
)
from roastpilot_agent.safety import SafetyEvaluation, SafetyVerdict  # noqa: E402
from roastpilot_agent.store import RoastStore  # noqa: E402


def _profile(
    *,
    name: str = "Guatemala Conebosque",
    target_drop_temp_c: float = 195.0,
    target_development_percent: float = 16.0,
) -> RoastProfile:
    return RoastProfile(
        name=name,
        bean_origin="Guatemala",
        bean_weight_grams=250.0,
        initial_heat_percent=70,
        initial_fan_percent=40,
        target_drop_temp_c=target_drop_temp_c,
        target_development_percent=target_development_percent,
    )


async def _create_run(
    store: RoastStore,
    run_id: str,
    *,
    profile: RoastProfile,
    agent_phase: RoastPhase = RoastPhase.STARTING,
) -> None:
    await store.create_run(
        run_id=run_id, profile=profile, config=AppConfig(), agent_phase=agent_phase
    )


async def _record_row(
    store: RoastStore,
    run_id: str,
    tick: int,
    *,
    phase: RoastPhase,
    bean_temp: float | None,
    dev_pct: float | None = None,
) -> None:
    """Insert one telemetry row via the real write path (mirrors test_store.py's
    ``_record_row``: ``interval_seconds=0.0`` so every call writes)."""
    await store.record_telemetry(
        run_id=run_id,
        tick=tick,
        agent_phase=phase,
        elapsed_seconds=float(tick),
        interval_seconds=0.0,
        telemetry=None
        if bean_temp is None
        else RoastTelemetry(
            bean_temp_c=bean_temp, env_temp_c=bean_temp + 15.0, bean_ror_c_per_min=6.0
        ),
        development_percent=dev_pct,
    )


async def _record_drop_event(
    store: RoastStore, run_id: str, *, recorded_at_utc: str | None = None
) -> None:
    """Persist the executed ``drop_beans`` command event the drop-gate and the
    event-anchored drop reading both require
    (:func:`rpd_corpus_score._drop_event_recorded_at`)."""
    await store.record_event(
        run_id=run_id,
        kind=RoastEventKind.COMMAND_EXECUTED,
        source=RoastEventSource.CONTROLLER,
        payload={"command": RoastCommand.DROP_BEANS.value, "source": "operator"},
        recorded_at_utc=recorded_at_utc,
    )


async def _insert_telemetry_row_at(
    store: RoastStore,
    run_id: str,
    tick: int,
    *,
    phase: RoastPhase,
    bean_temp: float | None,
    dev_pct: float | None,
    recorded_at_utc: str,
) -> None:
    """Insert one telemetry row with an EXPLICIT ``recorded_at_utc`` (raw SQL).

    :meth:`RoastStore.record_telemetry` always stamps the real wall-clock
    ``_utc_now()``, so this is the only way to construct a controlled,
    deterministic time gap between telemetry rows and a drop event — needed
    to test the event-anchored drop reading (fix #2) without depending on
    real-clock timing between successive `await`s.
    """
    await store.connection.execute(
        "INSERT INTO telemetry_snapshots"
        " (run_id, tick, recorded_at_utc, agent_phase, bean_temp_c, development_percent)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, tick, recorded_at_utc, phase.value, bean_temp, dev_pct),
    )
    await store.connection.commit()


async def _seed_scoreable_run(
    store: RoastStore,
    run_id: str,
    *,
    profile: RoastProfile,
    drop_temp_c: float,
    dtr_percent: float,
    outcome: Literal["completed", "aborted", "faulted"] = "completed",
    rating: Literal[1, 2, 3, 4, 5] | None = 4,
    add_cooling_tail: bool = False,
    record_drop: bool = True,
) -> None:
    """Build a run with one development row (the drop) via the real write path.

    ``add_cooling_tail=True`` appends a post-drop COOLING row with a lower
    (physically falling) bean temperature and no ``development_percent`` — the
    scorer must ignore it and still read the drop off the LAST
    ``development``-phase row. ``record_drop=False`` omits the executed
    ``drop_beans`` command event (fix #1's drop-gate), simulating a run that
    reached DEVELOPMENT and was cooled/ended WITHOUT ever dropping (e.g. an
    operator ``start_cooling`` recovery).
    """
    await _create_run(store, run_id, profile=profile)
    await _record_row(store, run_id, 1, phase=RoastPhase.ROASTING_PRE_FIRST_CRACK, bean_temp=180.0)
    await _record_row(
        store,
        run_id,
        2,
        phase=RoastPhase.DEVELOPMENT,
        bean_temp=drop_temp_c,
        dev_pct=dtr_percent,
    )
    if record_drop:
        await _record_drop_event(store, run_id)
    if add_cooling_tail:
        await _record_row(store, run_id, 3, phase=RoastPhase.COOLING, bean_temp=drop_temp_c - 20.0)
    await store.complete_run(run_id=run_id, outcome=outcome, agent_phase=RoastPhase.COMPLETE)
    if rating is not None:
        await store.set_operator_rating(run_id, rating=rating)


async def _insert_legacy_run_missing_targets(store: RoastStore, run_id: str) -> None:
    """A pre-D7-shaped frozen profile missing the required drop/DTR targets.

    Bypasses :meth:`RoastStore.create_run` (which always serializes a
    validated :class:`RoastProfile`, so it can never omit a required field) —
    this simulates a legacy/corrupt ``profile_json`` directly via raw SQL, the
    only way to construct that state.
    """
    legacy_profile_json = json.dumps(
        {
            "name": "Legacy Bean",
            "bean_origin": "Legacy",
            "bean_weight_grams": 250.0,
            "initial_heat_percent": 70,
            "initial_fan_percent": 40,
            # target_drop_temp_c / target_development_percent deliberately omitted.
        }
    )
    await store.connection.execute(
        "INSERT INTO roast_runs (id, agent_phase, profile_json, config_json,"
        " started_at_utc, created_at_utc, updated_at_utc, outcome, completed_at_utc)"
        " VALUES (?, 'complete', ?, '{}', '2026-01-01T00:00:00+00:00',"
        " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 'completed',"
        " '2026-01-01T00:05:00+00:00')",
        (run_id, legacy_profile_json),
    )
    await store.connection.commit()


# --- score_run: the five required scenarios ----------------------------------


@pytest.mark.asyncio
async def test_score_run_hit(tmp_store: RoastStore) -> None:
    await tmp_store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _seed_scoreable_run(
            tmp_store, "hit-run", profile=profile, drop_temp_c=195.0, dtr_percent=16.0
        )
        result = await scorer.score_run(tmp_store, "hit-run")
        assert isinstance(result, scorer.ScoredRun)
        assert result.score.hit is True
        assert result.score.scalar == pytest.approx(1.0)
        assert result.bean_name == "Guatemala Conebosque"
        assert result.rating == 4
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_score_run_miss_temp_short_and_dtr_over(tmp_store: RoastStore) -> None:
    """The Conebosque-baseline shape: dropped short on temp, over on DTR."""
    await tmp_store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _seed_scoreable_run(
            tmp_store, "miss-run", profile=profile, drop_temp_c=188.0, dtr_percent=21.0
        )
        result = await scorer.score_run(tmp_store, "miss-run")
        assert isinstance(result, scorer.ScoredRun)
        assert result.score.hit is False
        assert result.score.scalar == pytest.approx(0.0)
        assert result.score.drop_temp_error_c == pytest.approx(-7.0)
        assert result.score.dtr_error_pp == pytest.approx(5.0)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_score_run_faulted_is_terminated_abnormally(tmp_store: RoastStore) -> None:
    """A faulted run is never a HIT and scores 0, even with in-window numbers."""
    await tmp_store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _seed_scoreable_run(
            tmp_store,
            "faulted-run",
            profile=profile,
            drop_temp_c=195.0,
            dtr_percent=16.0,
            outcome="faulted",
        )
        result = await scorer.score_run(tmp_store, "faulted-run")
        assert isinstance(result, scorer.ScoredRun)
        assert result.score.terminated_abnormally is True
        assert result.score.hit is False
        assert result.score.scalar == pytest.approx(0.0)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_score_run_aborted_is_terminated_abnormally(tmp_store: RoastStore) -> None:
    await tmp_store.initialize()
    try:
        profile = _profile()
        await _seed_scoreable_run(
            tmp_store,
            "aborted-run",
            profile=profile,
            drop_temp_c=195.0,
            dtr_percent=16.0,
            outcome="aborted",
        )
        result = await scorer.score_run(tmp_store, "aborted-run")
        assert isinstance(result, scorer.ScoredRun)
        assert result.score.terminated_abnormally is True
        assert result.score.hit is False
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_score_run_ignores_a_genuine_later_cooling_tail_row(
    tmp_store: RoastStore,
) -> None:
    """A LATER, genuinely-cooled-down COOLING row (falling bean temp, no fresh
    ``development_percent`` — a failed/incomplete telemetry read) must never
    be read as the drop: it falls back to the last ``development``-phase
    row's own reading, exactly like ``RoastStore._build_reference_roast``'s
    drop landmark (design note §6.4a)."""
    await tmp_store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _seed_scoreable_run(
            tmp_store,
            "cooling-tail-run",
            profile=profile,
            drop_temp_c=195.0,
            dtr_percent=16.0,
            add_cooling_tail=True,
        )
        result = await scorer.score_run(tmp_store, "cooling-tail-run")
        assert isinstance(result, scorer.ScoredRun)
        # If the cooling-tail row (bean_temp 175.0) had leaked in, this would
        # be a MISS (7 C short) rather than a perfect HIT.
        assert result.score.drop_temp_c == pytest.approx(195.0)
        assert result.score.hit is True
        assert result.score.scalar == pytest.approx(1.0)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_score_run_prefers_the_transition_tick_row_over_the_last_development_row(
    tmp_store: RoastStore,
) -> None:
    """Regression test for the real-store finding behind the #711 A/B validation.

    The live controller flips ``agent_phase`` to ``cooling`` SYNCHRONOUSLY
    within the same tick it executes the drop
    (``api._publish_and_persist_telemetry`` persists ``snapshot.phase`` AFTER
    the tick's transition) — so the telemetry row immediately following the
    last ``development`` row carries the TRUE drop-instant ``bean_temp_c``
    (one control-loop tick fresher) and the just-frozen
    ``development_percent``, not the last ``development``-tagged row itself.
    The drop event's ``recorded_at_utc`` is set (deterministically, via
    explicit timestamps) to land almost exactly on the transition row, so the
    fix #2 event-anchored read picks it, not the earlier development row.

    Reproduces the shape confirmed against the real store's ratified #559
    Conebosque baseline (``55f6a034…``): the last ``development`` row alone
    reads 188.0 °C / ~19.97 %, but the immediately-following ``cooling`` row
    (same/higher temp, frozen percent) reads 188.0 °C / ~21.0 % — the ratified
    number. A naive "always use the last development row" implementation
    would regress this test back to the pre-fix (short) reading.
    """
    await tmp_store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _create_run(tmp_store, "transition-run", profile=profile)
        await _insert_telemetry_row_at(
            tmp_store,
            "transition-run",
            1,
            phase=RoastPhase.DEVELOPMENT,
            bean_temp=188.0,
            dev_pct=19.97,
            recorded_at_utc="2026-01-01T00:00:00.000000+00:00",
        )
        # The transition tick: phase already flipped to 'cooling', carrying the
        # true drop instant — the just-frozen development_percent and the fresh
        # bean_temp_c. The temps are made to DIFFER (189 vs the dev row's 188)
        # so BOTH fields discriminate a revert to the naive last-development
        # rule, not just the percentage.
        await _insert_telemetry_row_at(
            tmp_store,
            "transition-run",
            2,
            phase=RoastPhase.COOLING,
            bean_temp=189.0,
            dev_pct=21.0,
            recorded_at_utc="2026-01-01T00:00:10.000000+00:00",
        )
        # The drop event lands almost exactly on the transition row (10.0 s
        # in), far from the development row (0.0 s) — the event-anchored read
        # must pick the transition row.
        await _record_drop_event(
            tmp_store, "transition-run", recorded_at_utc="2026-01-01T00:00:10.010000+00:00"
        )
        await tmp_store.complete_run(
            run_id="transition-run", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        result = await scorer.score_run(tmp_store, "transition-run")
        assert isinstance(result, scorer.ScoredRun)
        # Both from the transition row; the naive last-development rule would
        # give 188.0 / 19.97 and fail on either assertion.
        assert result.score.drop_temp_c == pytest.approx(189.0)
        assert result.score.dtr_percent == pytest.approx(21.0)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_fetch_telemetry_rows_orders_by_insertion_id_not_tick(
    tmp_store: RoastStore,
) -> None:
    """Telemetry chronology follows the durable insertion id, never
    ``(tick, id)`` (fix #1, Codex P1): a restart resets the process-local
    tick counter to zero, so a post-restart row can carry a LOWER tick than
    an older pre-restart row despite being chronologically LATER. Mirrors
    ``RoastStore.read_telemetry_points``'s own documented insertion-id rule.
    """
    await tmp_store.initialize()
    try:
        profile = _profile()
        await _create_run(tmp_store, "restart-run", profile=profile)
        # Pre-restart: a HIGH tick, inserted FIRST (lower id). Raw insert
        # (not the real record_telemetry write path, which throttles on a
        # MONOTONICALLY INCREASING elapsed_seconds — a decreasing tick/elapsed
        # pair, the very thing this test constructs, would itself be silently
        # dropped by that unrelated guard).
        await _insert_telemetry_row_at(
            tmp_store,
            "restart-run",
            500,
            phase=RoastPhase.DEVELOPMENT,
            bean_temp=190.0,
            dev_pct=None,
            recorded_at_utc="2026-01-01T00:00:00.000000+00:00",
        )
        # Post-restart: the tick counter reset to a LOW value, inserted
        # SECOND (higher id) — chronologically LATER despite the lower tick.
        await _insert_telemetry_row_at(
            tmp_store,
            "restart-run",
            5,
            phase=RoastPhase.DEVELOPMENT,
            bean_temp=170.0,
            dev_pct=None,
            recorded_at_utc="2026-01-01T00:01:00.000000+00:00",
        )
        rows = await scorer._fetch_telemetry_rows(  # pyright: ignore[reportPrivateUsage]
            tmp_store, "restart-run"
        )
        # Insertion (id) order: the pre-restart (high-tick) row first, the
        # post-restart (low-tick) row last. A (tick, id) sort would reverse
        # this (5 sorts before 500), putting the STALE pre-restart row last.
        assert [float(row["bean_temp_c"]) for row in rows] == [190.0, 170.0]
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_score_run_event_anchor_prefers_true_drop_over_a_gapped_cooling_sample(
    tmp_store: RoastStore,
) -> None:
    """Regression test for fix #2 (Codex P1): for a run recorded with an
    ordinary PERIODIC telemetry cadence (not force-persisted on a phase
    transition), the row immediately after the last development row can be a
    routine cooling sample several seconds later, with a FALLEN bean_temp_c
    (genuine post-drop cooling) but the same frozen development_percent. The
    event-anchored read must still land on the TRUE drop-instant reading (the
    last development row itself, nearest in time to the drop_beans command
    event), not the later, cooled sample the old successor-row heuristic
    would have picked."""
    await tmp_store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _create_run(tmp_store, "gapped-run", profile=profile)
        await _insert_telemetry_row_at(
            tmp_store,
            "gapped-run",
            1,
            phase=RoastPhase.DEVELOPMENT,
            bean_temp=195.0,
            dev_pct=16.0,
            recorded_at_utc="2026-01-01T00:00:00.000000+00:00",
        )
        # A gapped, ordinary PERIODIC post-drop cooling sample: 25 s later,
        # the bean temp has genuinely fallen (real cooling), but
        # development_percent is still the frozen, correct achieved DTR.
        await _insert_telemetry_row_at(
            tmp_store,
            "gapped-run",
            2,
            phase=RoastPhase.COOLING,
            bean_temp=180.0,
            dev_pct=16.0,
            recorded_at_utc="2026-01-01T00:00:25.000000+00:00",
        )
        # The drop_beans command event fires at the TRUE drop instant, 0.5 s
        # after the last development row's own reading — far nearer to it
        # than to the gapped cooling sample 25 s later.
        await _record_drop_event(
            tmp_store, "gapped-run", recorded_at_utc="2026-01-01T00:00:00.500000+00:00"
        )
        await tmp_store.complete_run(
            run_id="gapped-run", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        result = await scorer.score_run(tmp_store, "gapped-run")
        assert isinstance(result, scorer.ScoredRun)
        # If the naive successor-row heuristic had been used instead of the
        # event anchor, this would read 180.0 (the fallen, cooled temp) — a
        # 7 C MISS instead of a perfect HIT.
        assert result.score.drop_temp_c == pytest.approx(195.0)
        assert result.score.dtr_percent == pytest.approx(16.0)
        assert result.score.hit is True
        assert result.score.scalar == pytest.approx(1.0)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_score_run_handles_naive_legacy_timestamp_without_aborting(
    tmp_store: RoastStore,
) -> None:
    """Regression test (Codex P2, round 3): a legacy telemetry row with a
    NAIVE ISO timestamp (no UTC offset) must not raise when compared against
    the drop event's offset-aware timestamp — assumed UTC, not a
    corpus-aborting ``TypeError``."""
    await tmp_store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _create_run(tmp_store, "naive-run", profile=profile)
        # A legacy row: NAIVE timestamp (no "+00:00" offset).
        await _insert_telemetry_row_at(
            tmp_store,
            "naive-run",
            1,
            phase=RoastPhase.DEVELOPMENT,
            bean_temp=195.0,
            dev_pct=16.0,
            recorded_at_utc="2026-01-01T00:00:00.000000",
        )
        await _record_drop_event(
            tmp_store, "naive-run", recorded_at_utc="2026-01-01T00:00:00.500000+00:00"
        )
        await tmp_store.complete_run(
            run_id="naive-run", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        result = await scorer.score_run(tmp_store, "naive-run")
        assert isinstance(result, scorer.ScoredRun)
        assert result.score.drop_temp_c == pytest.approx(195.0)
        assert result.score.dtr_percent == pytest.approx(16.0)
        assert result.score.hit is True
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_score_run_same_tick_ceiling_guard_drop_with_no_development_row(
    tmp_store: RoastStore,
) -> None:
    """Regression test (Codex P1, round 3): first crack detected AT/ABOVE the
    ceiling guard temperature makes the controller transition
    DEVELOPMENT -> COOLING within the SAME tick it fires the drop, so only a
    single COOLING-tagged telemetry row is ever persisted — no
    ``development``-phase row exists at all. The event-anchored read must
    still find it: a same-tick guard-drop now SCORES as an abnormal MISS
    (scalar 0), not silently excluded from the corpus."""
    await tmp_store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _create_run(tmp_store, "guard-only-run", profile=profile)
        # The ONLY telemetry row for this run: tagged 'cooling' (the phase
        # already flipped by the time it was persisted), at/above the
        # ceiling guard.
        await _insert_telemetry_row_at(
            tmp_store,
            "guard-only-run",
            1,
            phase=RoastPhase.COOLING,
            bean_temp=198.0,
            dev_pct=0.5,
            recorded_at_utc="2026-01-01T00:00:00.100000+00:00",
        )
        # A single event carries BOTH the executed drop_beans command AND the
        # ceiling-guard reason, exactly like the real controller
        # (D88 amendment A1's _maybe_ceiling_guard_drop).
        await tmp_store.record_event(
            run_id="guard-only-run",
            kind=RoastEventKind.COMMAND_EXECUTED,
            source=RoastEventSource.CONTROLLER,
            payload={
                "command": RoastCommand.DROP_BEANS.value,
                "source": "policy",
                "reason": DropReason.CEILING_GUARD.value,
            },
            recorded_at_utc="2026-01-01T00:00:00.000000+00:00",
        )
        await tmp_store.complete_run(
            run_id="guard-only-run", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        result = await scorer.score_run(tmp_store, "guard-only-run")
        assert isinstance(result, scorer.ScoredRun)
        assert result.score.drop_temp_c == pytest.approx(198.0)
        assert result.score.terminated_abnormally is True
        assert result.score.hit is False
        assert result.score.scalar == pytest.approx(0.0)
    finally:
        await tmp_store.close()


def test_finite_or_none() -> None:
    assert scorer._finite_or_none(None) is None  # pyright: ignore[reportPrivateUsage]
    assert scorer._finite_or_none(float("inf")) is None  # pyright: ignore[reportPrivateUsage]
    assert scorer._finite_or_none(float("-inf")) is None  # pyright: ignore[reportPrivateUsage]
    assert scorer._finite_or_none(23.5) == 23.5  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_score_run_normalizes_non_finite_ambient_to_none(tmp_store: RoastStore) -> None:
    """Regression test (Codex P2, round 3): a non-finite ``ambient_temp_c``
    (SQLite round-trips ``+/-Infinity`` faithfully, unlike ``NaN``, which it
    silently stores as ``NULL``) must normalize to ``None`` — ``json.dumps``
    would otherwise emit a non-standard ``Infinity`` token a strict
    ``JSON.parse`` rejects for the whole report."""
    await tmp_store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _seed_scoreable_run(
            tmp_store, "hot-ambient-run", profile=profile, drop_temp_c=195.0, dtr_percent=16.0
        )
        await tmp_store.set_ambient(
            "hot-ambient-run", temperature_c=float("inf"), humidity_percent=None, pressure_hpa=None
        )
        result = await scorer.score_run(tmp_store, "hot-ambient-run")
        assert isinstance(result, scorer.ScoredRun)
        assert result.ambient_temp_c is None

        report = scorer.CorpusReport(scored=[result], skipped=[])
        payload = scorer.report_to_json(report)
        assert payload["runs"][0]["ambient_temp_c"] is None
        # Must be valid, RFC-8259-strict JSON: no "Infinity" token anywhere.
        assert "Infinity" not in json.dumps(payload)

        table = scorer.render_markdown_table(report)
        assert "inf" not in table.lower()
    finally:
        await tmp_store.close()


# --- _extract_drop_reading / _nearest_reading_to_timestamp: direct unit tests --
# (the fallback branches score_run's own drop-gate makes unreachable through
# score_run itself — a valid drop_event_recorded_at_utc is guaranteed non-None
# by the time score_run calls _extract_drop_reading — but genuinely defensive
# code the pure helpers must still cover: an unparseable event timestamp, an
# unparseable per-row timestamp, and the no-successor-row fallback tail.)


def _make_telemetry_rows(
    specs: list[tuple[str, float | None, float | None, str]],
) -> list[sqlite3.Row]:
    """Build real ``sqlite3.Row`` objects (not RoastStore-backed) shaped like
    :func:`rpd_corpus_score._fetch_telemetry_rows`'s projection, for direct
    unit tests of the pure row-processing helpers.

    Args:
        specs: ``(agent_phase, bean_temp_c, development_percent, recorded_at_utc)``
            tuples, in the intended row order.
    """
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE t (agent_phase TEXT, bean_temp_c REAL,"
        " development_percent REAL, recorded_at_utc TEXT)"
    )
    connection.executemany("INSERT INTO t VALUES (?, ?, ?, ?)", specs)
    rows = list(
        connection.execute(
            "SELECT agent_phase, bean_temp_c, development_percent, recorded_at_utc"
            " FROM t ORDER BY rowid"
        )
    )
    connection.close()
    return rows


def test_nearest_reading_to_timestamp_skips_row_with_unparseable_recorded_at() -> None:
    """A row whose own ``recorded_at_utc`` fails to parse must be skipped,
    not crash the search — the next candidate still wins."""
    rows = _make_telemetry_rows(
        [
            ("development", 180.0, 10.0, "not-a-timestamp"),
            ("cooling", 195.0, 16.0, "2026-01-01T00:00:00.500000+00:00"),
        ]
    )
    reading = scorer._nearest_reading_to_timestamp(  # pyright: ignore[reportPrivateUsage]
        rows, "2026-01-01T00:00:00.000000+00:00"
    )
    assert reading is not None
    assert reading.bean_temp_c == 195.0
    assert reading.development_percent == 16.0


def test_extract_drop_reading_falls_back_to_last_dev_row_on_unparseable_event_timestamp() -> None:
    """An unparseable ``drop_event_recorded_at_utc`` falls back to the last
    ``development`` row's OWN reading, not a following row — the successor
    and the last-development row are made to DIFFER so this discriminates a
    regression back to the (removed) successor-row heuristic."""
    rows = _make_telemetry_rows(
        [
            ("development", 195.0, 16.0, "2026-01-01T00:00:00+00:00"),
            ("cooling", 180.0, 16.0, "2026-01-01T00:00:05+00:00"),
        ]
    )
    reading = scorer._extract_drop_reading(  # pyright: ignore[reportPrivateUsage]
        rows, [0], "not-a-timestamp"
    )
    assert reading is not None
    assert reading.bean_temp_c == 195.0
    assert reading.development_percent == 16.0


def test_extract_drop_reading_falls_back_to_last_development_row_when_no_successor() -> None:
    """No row follows the last development row: the fallback's final tail —
    the last development row's own reading — must still be returned."""
    rows = _make_telemetry_rows([("development", 195.0, 16.0, "2026-01-01T00:00:00+00:00")])
    reading = scorer._extract_drop_reading(  # pyright: ignore[reportPrivateUsage]
        rows, [0], "not-a-timestamp"
    )
    assert reading is not None
    assert reading.bean_temp_c == 195.0
    assert reading.development_percent == 16.0


def test_extract_drop_reading_skips_event_anchor_when_timestamp_is_none() -> None:
    """``drop_event_recorded_at_utc=None`` — structurally unreachable through
    ``score_run``, which gates on a non-``None`` event timestamp before ever
    calling this helper, but a defensive path the pure helper must still
    cover — skips the event-anchor entirely and goes straight to the
    fallback."""
    rows = _make_telemetry_rows([("development", 195.0, 16.0, "2026-01-01T00:00:00+00:00")])
    reading = scorer._extract_drop_reading(rows, [0], None)  # pyright: ignore[reportPrivateUsage]
    assert reading is not None
    assert reading.bean_temp_c == 195.0
    assert reading.development_percent == 16.0


def test_extract_drop_reading_fallback_ignores_a_complete_but_fallen_successor_row() -> None:
    """Regression test (Codex P2, round 3): a COMPLETE (both fields
    populated) but genuinely fallen cooling successor row — the exact shape
    a pre-force-persist historical run's periodic cadence can produce — must
    still be ignored by the fallback in favor of the last ``development``
    row's own reading. Without a correlated timestamp (the event anchor
    already failed here), the fallback can no longer tell that successor
    apart from a real several-seconds-later cooling sample, so it must never
    be trusted, even when it is superficially "complete"."""
    rows = _make_telemetry_rows(
        [
            ("development", 195.0, 16.0, "2026-01-01T00:00:00+00:00"),
            # A LATER, fallen cooling sample: both fields present, but the
            # temperature has genuinely dropped (real post-drop cooling).
            ("cooling", 180.0, 16.0, "2026-01-01T00:00:25+00:00"),
        ]
    )
    reading = scorer._extract_drop_reading(  # pyright: ignore[reportPrivateUsage]
        rows, [0], "not-a-timestamp"
    )
    assert reading is not None
    # If the removed successor-row heuristic had been used, this would read
    # 180.0 (the fallen temp) instead of the true 195.0.
    assert reading.bean_temp_c == pytest.approx(195.0)
    assert reading.development_percent == pytest.approx(16.0)


def test_nearest_reading_to_timestamp_compares_naive_and_aware_timestamps_safely() -> None:
    """Regression test (Codex P2, round 3): a legacy row's NAIVE ISO
    timestamp (no UTC offset) compared against the drop event's
    offset-AWARE one must not raise ``TypeError`` — both are normalized to
    UTC-aware before subtracting, so the naive row is still found and used."""
    rows = _make_telemetry_rows(
        [
            # NAIVE: no "+00:00" offset.
            ("development", 195.0, 16.0, "2026-01-01T00:00:00.000000"),
        ]
    )
    reading = scorer._nearest_reading_to_timestamp(  # pyright: ignore[reportPrivateUsage]
        rows, "2026-01-01T00:00:00.500000+00:00"
    )
    assert reading is not None
    assert reading.bean_temp_c == 195.0
    assert reading.development_percent == 16.0


def test_extract_drop_reading_empty_development_indices_uses_event_anchor() -> None:
    """``development_indices=[]`` (a same-tick ceiling-guard drop has NO
    ``development``-phase row at all): the event anchor alone must still
    find the reading — the fallback is never consulted, and must never index
    an empty list."""
    rows = _make_telemetry_rows([("cooling", 198.0, 0.5, "2026-01-01T00:00:00.100000+00:00")])
    reading = scorer._extract_drop_reading(  # pyright: ignore[reportPrivateUsage]
        rows, [], "2026-01-01T00:00:00.000000+00:00"
    )
    assert reading is not None
    assert reading.bean_temp_c == 198.0
    assert reading.development_percent == 0.5


def test_extract_drop_reading_empty_development_indices_and_failed_anchor_returns_none() -> None:
    """``development_indices=[]`` AND the event anchor also fails to find a
    usable row: there is nothing left to fall back to, so the pure helper
    must return ``None`` rather than raise on an empty-list index."""
    rows = _make_telemetry_rows([])
    reading = scorer._extract_drop_reading(  # pyright: ignore[reportPrivateUsage]
        rows, [], "2026-01-01T00:00:00.000000+00:00"
    )
    assert reading is None


@pytest.mark.asyncio
async def test_score_run_missing_targets_is_skipped(tmp_store: RoastStore) -> None:
    await tmp_store.initialize()
    try:
        await _insert_legacy_run_missing_targets(tmp_store, "legacy-run")
        result = await scorer.score_run(tmp_store, "legacy-run")
        assert isinstance(result, scorer.SkippedRun)
        assert result.run_id == "legacy-run"
        assert "could not parse frozen profile" in result.reason
    finally:
        await tmp_store.close()


# --- score_run: additional skip paths -----------------------------------------


@pytest.mark.asyncio
async def test_score_run_not_found_is_skipped(tmp_store: RoastStore) -> None:
    await tmp_store.initialize()
    try:
        result = await scorer.score_run(tmp_store, "ghost")
        assert isinstance(result, scorer.SkippedRun)
        assert result.reason == "run not found"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_score_run_pre_first_crack_only_run_is_skipped_for_no_drop_event(
    tmp_store: RoastStore,
) -> None:
    """A run that never reached DEVELOPMENT and never dropped is skipped on
    the drop-gate (round 3: the drop-event check now runs BEFORE the
    development-row check, so this hits "no drop_beans command event", not a
    development-specific reason)."""
    await tmp_store.initialize()
    try:
        profile = _profile()
        await _create_run(tmp_store, "no-dev-run", profile=profile)
        await _record_row(
            tmp_store, "no-dev-run", 1, phase=RoastPhase.ROASTING_PRE_FIRST_CRACK, bean_temp=180.0
        )
        await tmp_store.complete_run(
            run_id="no-dev-run", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        result = await scorer.score_run(tmp_store, "no-dev-run")
        assert isinstance(result, scorer.SkippedRun)
        assert result.reason == "no drop_beans command event (run cooled/ended without a bean drop)"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_score_run_development_row_missing_fields_is_skipped(
    tmp_store: RoastStore,
) -> None:
    await tmp_store.initialize()
    try:
        profile = _profile()
        await _create_run(tmp_store, "null-dev-run", profile=profile)
        # A development row with no bean_temp_c reading (a failed/sessionless
        # telemetry read that tick) and no development_percent. A drop event IS
        # recorded so this run clears the drop-gate and reaches the "no
        # usable reading" check this test targets — neither the event anchor
        # (no row near it has both fields) nor the development fallback (the
        # only development row itself is missing both fields) can produce one.
        await _record_row(
            tmp_store, "null-dev-run", 1, phase=RoastPhase.DEVELOPMENT, bean_temp=None
        )
        await _record_drop_event(tmp_store, "null-dev-run")
        await tmp_store.complete_run(
            run_id="null-dev-run", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        result = await scorer.score_run(tmp_store, "null-dev-run")
        assert isinstance(result, scorer.SkippedRun)
        assert "no drop reading" in result.reason
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_score_run_no_development_row_and_no_event_match_is_skipped(
    tmp_store: RoastStore,
) -> None:
    """A drop event exists but there is NO telemetry at all (so the event
    anchor has nothing to match) and NO development-phase row to fall back
    on — the unified terminal skip, exercising ``_extract_drop_reading``'s
    empty-``development_indices`` guard directly through ``score_run``."""
    await tmp_store.initialize()
    try:
        profile = _profile()
        await _create_run(tmp_store, "empty-run", profile=profile)
        await _record_drop_event(tmp_store, "empty-run")
        await tmp_store.complete_run(
            run_id="empty-run", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        result = await scorer.score_run(tmp_store, "empty-run")
        assert isinstance(result, scorer.SkippedRun)
        assert "no drop reading" in result.reason
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_score_run_non_finite_metric_input_is_skipped(tmp_store: RoastStore) -> None:
    """A non-finite achieved drop temp (a corrupt historical trace) makes
    ``joint_window_score`` raise ``ValueError`` — one corrupt run must be
    skipped, not abort the whole corpus (fix #4, Codex P2).

    Uses ``inf``, not ``nan``: SQLite silently stores a bound ``NaN`` REAL
    parameter as ``NULL`` (round-trip verified), which would instead exercise
    the "missing bean_temp_c" skip path — ``+/-Infinity`` round-trips as
    IEEE-754 and reaches the metric unchanged.
    """
    await tmp_store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _create_run(tmp_store, "corrupt-run", profile=profile)
        await _insert_telemetry_row_at(
            tmp_store,
            "corrupt-run",
            1,
            phase=RoastPhase.DEVELOPMENT,
            bean_temp=float("inf"),
            dev_pct=16.0,
            recorded_at_utc="2026-01-01T00:00:00.000000+00:00",
        )
        await _record_drop_event(
            tmp_store, "corrupt-run", recorded_at_utc="2026-01-01T00:00:00.500000+00:00"
        )
        await tmp_store.complete_run(
            run_id="corrupt-run", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        result = await scorer.score_run(tmp_store, "corrupt-run")
        assert isinstance(result, scorer.SkippedRun)
        assert "corrupt trace or profile" in result.reason
    finally:
        await tmp_store.close()


# --- score_run: the drop-gate (fix #1, Codex P2) ------------------------------


@pytest.mark.asyncio
async def test_score_run_without_drop_command_is_skipped(tmp_store: RoastStore) -> None:
    """A run that reaches DEVELOPMENT and is then cooled/ended WITHOUT ever
    dropping (e.g. an operator ``start_cooling`` recovery) has development
    telemetry but no executed ``drop_beans`` command event — its
    post-development rows must not be scored as a drop reading."""
    await tmp_store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _seed_scoreable_run(
            tmp_store,
            "no-drop-run",
            profile=profile,
            drop_temp_c=195.0,
            dtr_percent=16.0,
            record_drop=False,
        )
        result = await scorer.score_run(tmp_store, "no-drop-run")
        assert isinstance(result, scorer.SkippedRun)
        assert "no drop_beans" in result.reason
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_score_run_with_drop_command_still_scores(tmp_store: RoastStore) -> None:
    """A normally-seeded run (WITH the executed ``drop_beans`` event) still
    scores — the drop-gate must not false-positive on a genuine drop."""
    await tmp_store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _seed_scoreable_run(
            tmp_store, "with-drop-run", profile=profile, drop_temp_c=195.0, dtr_percent=16.0
        )
        result = await scorer.score_run(tmp_store, "with-drop-run")
        assert isinstance(result, scorer.ScoredRun)
        assert result.score.hit is True
    finally:
        await tmp_store.close()


# --- terminated_abnormally: in-run guard/emergency detection ------------------


@pytest.mark.asyncio
async def test_completed_run_with_emergency_stop_is_terminated_abnormally(
    tmp_store: RoastStore,
) -> None:
    """An in-run EMERGENCY_STOP verdict marks even a 'completed'-outcome run
    abnormal (D15: the typed SafetyVerdict, bound as a parameter, not a raw
    string literal)."""
    await tmp_store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _seed_scoreable_run(
            tmp_store, "estop-run", profile=profile, drop_temp_c=195.0, dtr_percent=16.0
        )
        await tmp_store.record_safety_evaluation(
            run_id="estop-run",
            tick=99,
            evaluation=SafetyEvaluation(
                rule="hard_ceiling",
                verdict=SafetyVerdict.EMERGENCY_STOP,
                reason="bean temp exceeded the hard e-stop bound",
            ),
        )
        result = await scorer.score_run(tmp_store, "estop-run")
        assert isinstance(result, scorer.ScoredRun)
        assert result.score.terminated_abnormally is True
        assert result.score.hit is False
        assert result.score.scalar == pytest.approx(0.0)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_completed_run_with_ceiling_guard_drop_is_terminated_abnormally(
    tmp_store: RoastStore,
) -> None:
    """A persisted ceiling-guard drop event (D88 amendment A1) marks even a
    'completed'-outcome run abnormal."""
    await tmp_store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _seed_scoreable_run(
            tmp_store, "guard-run", profile=profile, drop_temp_c=195.0, dtr_percent=16.0
        )
        await tmp_store.record_event(
            run_id="guard-run",
            kind=RoastEventKind.COMMAND_EXECUTED,
            source=RoastEventSource.CONTROLLER,
            payload={
                "command": "drop_beans",
                "source": "policy",
                "reason": DropReason.CEILING_GUARD.value,
            },
        )
        result = await scorer.score_run(tmp_store, "guard-run")
        assert isinstance(result, scorer.ScoredRun)
        assert result.score.terminated_abnormally is True
        assert result.score.hit is False
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_completed_run_with_development_target_drop_stays_normal(
    tmp_store: RoastStore,
) -> None:
    """The OTHER deterministic drop reason (the ordinary dev%/temp anchor) must
    NOT be misread as an abnormal termination."""
    await tmp_store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _seed_scoreable_run(
            tmp_store, "normal-drop-run", profile=profile, drop_temp_c=195.0, dtr_percent=16.0
        )
        await tmp_store.record_event(
            run_id="normal-drop-run",
            kind=RoastEventKind.COMMAND_EXECUTED,
            source=RoastEventSource.CONTROLLER,
            payload={
                "command": "drop_beans",
                "source": "policy",
                "reason": DropReason.DEVELOPMENT_TARGET.value,
            },
        )
        result = await scorer.score_run(tmp_store, "normal-drop-run")
        assert isinstance(result, scorer.ScoredRun)
        assert result.score.terminated_abnormally is False
        assert result.score.hit is True
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_completed_run_with_non_emergency_verdicts_stays_normal(
    tmp_store: RoastStore,
) -> None:
    """The ordinary per-tick ALLOW/CLAMP verdicts every real roast records must
    NOT be misread as an abnormal termination.

    A real corpus run has many ``safety_evaluations`` rows (api.py records one
    per tick, mostly ALLOW). If the emergency-stop query ever lost its
    ``verdict = ?`` filter, every scored run would be misclassified abnormal —
    this pins the verdict-specific match (the mirror of the reason-specific
    ceiling-guard guard test above)."""
    await tmp_store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _seed_scoreable_run(
            tmp_store, "allow-run", profile=profile, drop_temp_c=195.0, dtr_percent=16.0
        )
        per_tick = (
            (10, SafetyVerdict.ALLOW),
            (20, SafetyVerdict.ALLOW),
            (30, SafetyVerdict.CLAMP),
        )
        for tick, verdict in per_tick:
            await tmp_store.record_safety_evaluation(
                run_id="allow-run",
                tick=tick,
                evaluation=SafetyEvaluation(
                    rule="rate_limit" if verdict is SafetyVerdict.CLAMP else "nominal",
                    verdict=verdict,
                    reason="ordinary per-tick evaluation",
                ),
            )
        result = await scorer.score_run(tmp_store, "allow-run")
        assert isinstance(result, scorer.ScoredRun)
        assert result.score.terminated_abnormally is False
        assert result.score.hit is True
    finally:
        await tmp_store.close()


# --- score_corpus: discovery + explicit run-ids -------------------------------


@pytest.mark.asyncio
async def test_score_corpus_discovers_every_finished_non_excluded_run(
    tmp_store: RoastStore,
) -> None:
    await tmp_store.initialize()
    try:
        profile = _profile()
        await _seed_scoreable_run(
            tmp_store, "run-a", profile=profile, drop_temp_c=195.0, dtr_percent=16.0
        )
        await _seed_scoreable_run(
            tmp_store, "run-b", profile=profile, drop_temp_c=188.0, dtr_percent=21.0
        )
        # An excluded run must never appear (#582).
        await _seed_scoreable_run(
            tmp_store, "run-excluded", profile=profile, drop_temp_c=195.0, dtr_percent=16.0
        )
        await tmp_store.set_run_excluded("run-excluded", excluded=True)
        # A still-active run (no outcome yet) must never appear.
        await _create_run(tmp_store, "run-active", profile=profile)

        report = await scorer.score_corpus(tmp_store)
        scored_ids = {run.run_id for run in report.scored}
        assert scored_ids == {"run-a", "run-b"}
        assert report.skipped == []
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_score_corpus_explicit_run_ids_filters_excluded_and_unfinished(
    tmp_store: RoastStore,
) -> None:
    await tmp_store.initialize()
    try:
        profile = _profile()
        await _seed_scoreable_run(
            tmp_store, "run-a", profile=profile, drop_temp_c=195.0, dtr_percent=16.0
        )
        await _seed_scoreable_run(
            tmp_store, "run-excluded", profile=profile, drop_temp_c=195.0, dtr_percent=16.0
        )
        await tmp_store.set_run_excluded("run-excluded", excluded=True)
        await _create_run(tmp_store, "run-active", profile=profile)

        report = await scorer.score_corpus(
            tmp_store, run_ids=["run-a", "run-excluded", "run-active", "ghost"]
        )
        assert {run.run_id for run in report.scored} == {"run-a"}
        skip_reasons = {skip.run_id: skip.reason for skip in report.skipped}
        assert skip_reasons["run-excluded"] == "run is excluded (soft-discarded, #582)"
        assert skip_reasons["run-active"] == "run has not finished (outcome is null)"
        assert skip_reasons["ghost"] == "run not found"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_score_corpus_discovery_skips_one_bad_profile_without_aborting(
    tmp_store: RoastStore,
) -> None:
    """A legacy/malformed frozen profile among the auto-discovered ids must
    not abort the whole corpus (fix #2, Codex P2): id-only discovery defers
    the profile parse to :func:`~rpd_corpus_score.score_run`, so the bad run
    lands in ``skipped`` and the good one still scores."""
    await tmp_store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _seed_scoreable_run(
            tmp_store, "good-run", profile=profile, drop_temp_c=195.0, dtr_percent=16.0
        )
        await _insert_legacy_run_missing_targets(tmp_store, "legacy-run")

        report = await scorer.score_corpus(tmp_store)  # run_ids=None: full auto-discovery

        assert {run.run_id for run in report.scored} == {"good-run"}
        skip_reasons = {skip.run_id: skip.reason for skip in report.skipped}
        assert "could not parse frozen profile" in skip_reasons["legacy-run"]
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_score_corpus_survives_a_run_that_raises(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Categorical resilience backstop: ANY per-run failure — a non-numeric REAL,
    malformed payload_json, an incompatible timestamp, anything a typed accessor
    can't anticipate — must become a SkippedRun, never abort the whole corpus.
    Simulated by making score_run raise for one run; the others still score."""
    await tmp_store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _seed_scoreable_run(
            tmp_store, "good-run", profile=profile, drop_temp_c=195.0, dtr_percent=16.0
        )
        await _seed_scoreable_run(
            tmp_store, "corrupt-run", profile=profile, drop_temp_c=195.0, dtr_percent=16.0
        )

        real_score_run = scorer.score_run

        async def flaky(store: RoastStore, run_id: str) -> object:
            if run_id == "corrupt-run":
                raise ValueError("simulated corrupt telemetry row")
            return await real_score_run(store, run_id)

        monkeypatch.setattr(scorer, "score_run", flaky)

        report = await scorer.score_corpus(tmp_store)

        assert {run.run_id for run in report.scored} == {"good-run"}
        skip_reasons = {skip.run_id: skip.reason for skip in report.skipped}
        assert "scoring failed" in skip_reasons["corrupt-run"]
        assert "simulated corrupt telemetry row" in skip_reasons["corrupt-run"]
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_score_corpus_explicit_run_ids_skips_malformed_profile_without_aborting(
    tmp_store: RoastStore,
) -> None:
    """A malformed frozen profile among EXPLICIT ``--run-ids`` must not abort
    the whole invocation (fix #3, Codex P2) — the explicit-ids prefilter
    calling ``read_run()`` is now guarded the same way ``score_run`` already
    is, so a bad profile is a per-run skip, not an unhandled exception."""
    await tmp_store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _seed_scoreable_run(
            tmp_store, "good-run", profile=profile, drop_temp_c=195.0, dtr_percent=16.0
        )
        await _insert_legacy_run_missing_targets(tmp_store, "legacy-run")

        report = await scorer.score_corpus(tmp_store, run_ids=["good-run", "legacy-run"])

        assert {run.run_id for run in report.scored} == {"good-run"}
        skip_reasons = {skip.run_id: skip.reason for skip in report.skipped}
        assert "could not parse frozen profile" in skip_reasons["legacy-run"]
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_score_corpus_deduplicates_explicit_run_ids(tmp_store: RoastStore) -> None:
    """A duplicated ``--run-ids`` entry must not be scored twice (fix #9,
    Codex P2) — it would otherwise double-count in ``n_scored``/hit-rate/mean
    scalar."""
    await tmp_store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _seed_scoreable_run(
            tmp_store, "run-a", profile=profile, drop_temp_c=195.0, dtr_percent=16.0
        )

        report = await scorer.score_corpus(tmp_store, run_ids=["run-a", "run-a", "run-a"])

        assert len(report.scored) == 1
        assert report.skipped == []
        stats = scorer.aggregate_stats(report)
        assert stats["n_scored"] == 1
        assert stats["hits"] == 1
    finally:
        await tmp_store.close()


# --- aggregate_stats / rendering -----------------------------------------------


def test_aggregate_stats_empty_corpus() -> None:
    stats = scorer.aggregate_stats(scorer.CorpusReport(scored=[], skipped=[]))
    assert stats == {
        "n_scored": 0,
        "n_skipped": 0,
        "hits": 0,
        "hit_rate": 0.0,
        "mean_scalar": 0.0,
        "rated": 0,
        "ambient_evidence_observed": 0,
    }


@pytest.mark.asyncio
async def test_aggregate_stats_and_rendering(tmp_store: RoastStore) -> None:
    await tmp_store.initialize()
    try:
        profile = _profile()
        await _seed_scoreable_run(
            tmp_store, "run-hit", profile=profile, drop_temp_c=195.0, dtr_percent=16.0, rating=5
        )
        await _seed_scoreable_run(
            tmp_store,
            "run-miss",
            profile=profile,
            drop_temp_c=188.0,
            dtr_percent=21.0,
            rating=None,
        )
        await _create_run(tmp_store, "run-no-dev", profile=profile)
        await tmp_store.complete_run(
            run_id="run-no-dev", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        report = await scorer.score_corpus(tmp_store)
        stats = scorer.aggregate_stats(report)
        assert stats["n_scored"] == 2
        assert stats["n_skipped"] == 1
        assert stats["hits"] == 1
        assert stats["hit_rate"] == pytest.approx(0.5)
        assert stats["mean_scalar"] == pytest.approx(0.5)
        assert stats["rated"] == 1
        assert stats["ambient_evidence_observed"] == 0

        table = scorer.render_markdown_table(report)
        assert "HIT" in table
        assert "MISS" in table
        assert "5★" in table  # rated row
        assert "—" in table  # unrated row + skipped columns
        assert "no drop_beans command event" in table
        assert "N scored: 2 (skipped: 1)" in table
        assert "HIT: 1/2 (50.0%)" in table
        # The #711 Goodhart guard extends to the aggregate: the mean scalar
        # line states how many of the scored runs are actually rated.
        assert "(rated: 1/2)" in table
        assert "retained DEVELOPMENT telemetry-snapshot coverage" in table
        assert "ambient evidence observed: 0/2" in table

        payload = scorer.report_to_json(report)
        assert payload["aggregate"] == stats
        assert len(payload["runs"]) == 2
        assert len(payload["skipped"]) == 1
        run_hit_entry = next(r for r in payload["runs"] if r["run_id"] == "run-hit")
        assert run_hit_entry["hit"] is True
        assert run_hit_entry["bean_name"] == "Guatemala Conebosque"
        assert run_hit_entry["operator_rating"] == 5
        assert run_hit_entry["ambient_doctrine_evidence"]["verdict"] == "not_proven"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_render_markdown_table_escapes_pipe_and_newline_in_bean_name(
    tmp_store: RoastStore,
) -> None:
    """A bean name containing ``|`` or a newline must not corrupt the table
    (fix #5, Codex P2): an unescaped ``|`` reads as an extra column
    delimiter, and a raw newline breaks the row across two lines."""
    await tmp_store.initialize()
    try:
        profile = _profile(
            name="Evil | Bean\nName", target_drop_temp_c=195.0, target_development_percent=16.0
        )
        await _seed_scoreable_run(
            tmp_store, "evil-run", profile=profile, drop_temp_c=195.0, dtr_percent=16.0
        )
        report = await scorer.score_corpus(tmp_store)
        table = scorer.render_markdown_table(report)

        row_line = next(line for line in table.splitlines() if "Evil" in line)
        # Exactly 9 columns (10 DELIMITING pipes) — count only pipes that are
        # not part of an escaped "\|" (the escaped pipe is still a literal
        # '|' character, just no longer a column delimiter).
        assert row_line.replace("\\|", "").count("|") == 10
        assert "\n" not in row_line
        assert "Evil \\| Bean Name" in table
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_render_markdown_table_escapes_pipe_in_a_bogus_run_id(
    tmp_store: RoastStore,
) -> None:
    """An explicit ``--run-ids`` value need not exist in the store, so a
    bogus/adversarial id reaches the skipped-row table verbatim (fix, Codex
    P3, round 4): a ``|`` in its first 8 characters must not corrupt the
    table any more than an unescaped bean name or skip reason would."""
    await tmp_store.initialize()
    try:
        bogus_id = "bad|id-with-a-pipe"
        report = await scorer.score_corpus(tmp_store, run_ids=[bogus_id])
        assert report.scored == []
        assert report.skipped == [scorer.SkippedRun(run_id=bogus_id, reason="run not found")]

        table = scorer.render_markdown_table(report)
        row_line = next(line for line in table.splitlines() if "run not found" in line)
        # Exactly 9 columns (10 DELIMITING pipes) — count only pipes that are
        # not part of an escaped "\|".
        assert row_line.replace("\\|", "").count("|") == 10
        assert "bad\\|id-w" in table
    finally:
        await tmp_store.close()


# --- snapshot_store_to_temp ----------------------------------------------------


def test_snapshot_store_to_temp_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        scorer.snapshot_store_to_temp(tmp_path / "does-not-exist.sqlite3", tmp_path)


@pytest.mark.asyncio
async def test_snapshot_store_to_temp_copies_committed_data(tmp_path: Path) -> None:
    real_path = tmp_path / "real.sqlite3"
    store = RoastStore(real_path)
    await store.initialize()
    try:
        await _create_run(store, "seed-run", profile=_profile())
    finally:
        await store.close()

    snapshot_dir = tmp_path / "snap"
    snapshot_dir.mkdir()
    snapshot_path = scorer.snapshot_store_to_temp(real_path, snapshot_dir)
    assert snapshot_path != real_path

    connection = sqlite3.connect(str(snapshot_path))
    try:
        cursor = connection.execute("SELECT id FROM roast_runs")
        rows = cursor.fetchall()
    finally:
        connection.close()
    assert rows == [("seed-run",)]


@pytest.mark.asyncio
async def test_snapshot_store_to_temp_handles_question_mark_in_store_filename(
    tmp_path: Path,
) -> None:
    """A store filename containing ``?`` must still open correctly, read-only
    (fix #8, Codex P2): a raw ``f"file:{path}?mode=ro"`` mis-parses the
    embedded ``?`` as the URI's query-string delimiter, truncating the path
    SQLite actually opens."""
    real_path = tmp_path / "operator?db.sqlite3"
    store = RoastStore(real_path)
    await store.initialize()
    try:
        await _create_run(store, "seed-run", profile=_profile())
    finally:
        await store.close()

    snapshot_dir = tmp_path / "snap"
    snapshot_dir.mkdir()
    snapshot_path = scorer.snapshot_store_to_temp(real_path, snapshot_dir)

    connection = sqlite3.connect(str(snapshot_path))
    try:
        cursor = connection.execute("SELECT id FROM roast_runs")
        rows = cursor.fetchall()
    finally:
        connection.close()
    assert rows == [("seed-run",)]


# --- end-to-end: run_corpus_score / main ---------------------------------------


@pytest.mark.asyncio
async def test_run_corpus_score_end_to_end_against_a_real_store_file(
    tmp_path: Path,
) -> None:
    real_path = tmp_path / "operator.sqlite3"
    store = RoastStore(real_path)
    await store.initialize()
    try:
        profile = _profile(target_drop_temp_c=195.0, target_development_percent=16.0)
        await _seed_scoreable_run(
            store, "baseline", profile=profile, drop_temp_c=188.0, dtr_percent=21.0
        )
        await _seed_scoreable_run(
            store, "treatment", profile=profile, drop_temp_c=190.0, dtr_percent=24.0
        )
    finally:
        await store.close()

    report = await scorer.run_corpus_score(real_path, None)
    assert {run.run_id for run in report.scored} == {"baseline", "treatment"}
    for run in report.scored:
        assert run.score.hit is False
        assert run.score.scalar == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_main_writes_json_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real_path = tmp_path / "operator.sqlite3"
    store = RoastStore(real_path)
    await store.initialize()
    try:
        await _seed_scoreable_run(
            store, "run-a", profile=_profile(), drop_temp_c=195.0, dtr_percent=16.0
        )
    finally:
        await store.close()

    json_out = tmp_path / "out" / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["rpd_corpus_score.py", "--store", str(real_path), "--json", str(json_out)],
    )
    exit_code = await scorer.main()
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "N scored: 1" in captured.out
    assert f"wrote JSON report -> {json_out}" in captured.out

    payload = json.loads(json_out.read_text())
    assert payload["aggregate"]["n_scored"] == 1
    assert payload["runs"][0]["run_id"] == "run-a"


@pytest.mark.asyncio
async def test_main_without_json_flag_prints_table_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No ``--json`` and an explicit ``--run-ids`` filter: no JSON is written."""
    real_path = tmp_path / "operator.sqlite3"
    store = RoastStore(real_path)
    await store.initialize()
    try:
        await _seed_scoreable_run(
            store, "run-a", profile=_profile(), drop_temp_c=195.0, dtr_percent=16.0
        )
        await _seed_scoreable_run(
            store, "run-b", profile=_profile(), drop_temp_c=188.0, dtr_percent=21.0
        )
    finally:
        await store.close()

    monkeypatch.setattr(
        sys,
        "argv",
        ["rpd_corpus_score.py", "--store", str(real_path), "--run-ids", "run-a"],
    )
    exit_code = await scorer.main()
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "N scored: 1" in captured.out
    assert "wrote JSON report" not in captured.out


@pytest.mark.asyncio
async def test_main_rejects_json_path_equal_to_store_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` pointing at the source store must be refused BEFORE any read
    (fix #3, Codex P1, data loss): writing the JSON report there would
    truncate and destroy the operator's SQLite database."""
    real_path = tmp_path / "operator.sqlite3"
    store = RoastStore(real_path)
    await store.initialize()
    try:
        await _seed_scoreable_run(
            store, "run-a", profile=_profile(), drop_temp_c=195.0, dtr_percent=16.0
        )
    finally:
        await store.close()

    original_bytes = real_path.read_bytes()

    monkeypatch.setattr(
        sys,
        "argv",
        ["rpd_corpus_score.py", "--store", str(real_path), "--json", str(real_path)],
    )
    with pytest.raises(SystemExit):
        await scorer.main()

    # The store file must be byte-for-byte untouched — not truncated, not
    # partially overwritten — and still a valid, openable SQLite database.
    assert real_path.read_bytes() == original_bytes
    connection = sqlite3.connect(str(real_path))
    try:
        cursor = connection.execute("SELECT id FROM roast_runs")
        rows = cursor.fetchall()
    finally:
        connection.close()
    assert rows == [("run-a",)]


@pytest.mark.asyncio
async def test_main_rejects_json_path_pointing_at_wal_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` pointing at the store's ``-wal`` sidecar must also be
    refused (fix #7, Codex P1, data loss): the sidecar is as live/mutable as
    the database file itself while the operator's agent has the store open
    in WAL mode."""
    real_path = tmp_path / "operator.sqlite3"
    store = RoastStore(real_path)
    await store.initialize()
    try:
        await _seed_scoreable_run(
            store, "run-a", profile=_profile(), drop_temp_c=195.0, dtr_percent=16.0
        )
    finally:
        await store.close()

    wal_path = tmp_path / "operator.sqlite3-wal"
    # The guard must reject the PATH by name regardless of whether the
    # sidecar happens to exist on disk right now (WAL mode may or may not
    # leave one behind after a clean close) — a resolved-path compare, not
    # an existence check.
    monkeypatch.setattr(
        sys,
        "argv",
        ["rpd_corpus_score.py", "--store", str(real_path), "--json", str(wal_path)],
    )
    with pytest.raises(SystemExit):
        await scorer.main()


@pytest.mark.asyncio
async def test_main_rejects_json_path_that_is_a_hard_link_to_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` pointing at a HARD LINK to the store must also be refused
    (fix #7, Codex P1, data loss): a hard link has a different path string
    but the same inode, so a resolved-path compare alone would miss it —
    only :func:`os.path.samefile` catches it."""
    real_path = tmp_path / "operator.sqlite3"
    store = RoastStore(real_path)
    await store.initialize()
    try:
        await _seed_scoreable_run(
            store, "run-a", profile=_profile(), drop_temp_c=195.0, dtr_percent=16.0
        )
    finally:
        await store.close()

    hardlink_path = tmp_path / "operator-alias.sqlite3"
    os.link(real_path, hardlink_path)
    original_bytes = real_path.read_bytes()

    monkeypatch.setattr(
        sys,
        "argv",
        ["rpd_corpus_score.py", "--store", str(real_path), "--json", str(hardlink_path)],
    )
    with pytest.raises(SystemExit):
        await scorer.main()

    # Writing through the hard link would have truncated the SAME inode the
    # store file itself uses — assert the underlying data survived.
    assert real_path.read_bytes() == original_bytes
    connection = sqlite3.connect(str(real_path))
    try:
        cursor = connection.execute("SELECT id FROM roast_runs")
        rows = cursor.fetchall()
    finally:
        connection.close()
    assert rows == [("run-a",)]
