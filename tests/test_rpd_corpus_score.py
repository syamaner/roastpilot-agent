"""Tests for the RP-D joint-objective offline corpus scorer (#711, D124, PR-D2).

Hardware-free: every run is built directly in a temp SQLite
:class:`~roastpilot_agent.store.RoastStore` (the same real write paths
``test_store.py`` uses), then scored with the real, pure
:func:`bakeoff_replay.joint_window_score`. No LLM, no network, no operator
store is ever touched.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Literal

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import rpd_corpus_score as scorer  # noqa: E402

from roastpilot_agent.config import AppConfig  # noqa: E402
from roastpilot_agent.models import RoastPhase, RoastProfile, RoastTelemetry  # noqa: E402
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
) -> None:
    """Build a run with one development row (the drop) via the real write path.

    ``add_cooling_tail=True`` appends a post-drop COOLING row with a lower
    (physically falling) bean temperature and no ``development_percent`` — the
    scorer must ignore it and still read the drop off the LAST
    ``development``-phase row.
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
        await _record_row(
            tmp_store,
            "transition-run",
            1,
            phase=RoastPhase.DEVELOPMENT,
            bean_temp=188.0,
            dev_pct=19.97,
        )
        # The transition tick: phase already flipped to 'cooling', but the
        # reading is the SAME (or higher) bean_temp_c and the just-frozen,
        # slightly-higher development_percent — the true drop instant.
        await _record_row(
            tmp_store, "transition-run", 2, phase=RoastPhase.COOLING, bean_temp=188.0, dev_pct=21.0
        )
        await tmp_store.complete_run(
            run_id="transition-run", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        result = await scorer.score_run(tmp_store, "transition-run")
        assert isinstance(result, scorer.ScoredRun)
        assert result.score.drop_temp_c == pytest.approx(188.0)
        assert result.score.dtr_percent == pytest.approx(21.0)
    finally:
        await tmp_store.close()


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
async def test_score_run_without_development_telemetry_is_skipped(
    tmp_store: RoastStore,
) -> None:
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
        assert result.reason == "no development-phase telemetry row"
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
        # telemetry read that tick) and no development_percent.
        await _record_row(
            tmp_store, "null-dev-run", 1, phase=RoastPhase.DEVELOPMENT, bean_temp=None
        )
        await tmp_store.complete_run(
            run_id="null-dev-run", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        result = await scorer.score_run(tmp_store, "null-dev-run")
        assert isinstance(result, scorer.SkippedRun)
        assert "missing bean_temp_c or development_percent" in result.reason
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
    from roastpilot_agent.safety import SafetyEvaluation, SafetyVerdict

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
    from roastpilot_agent.models import DropReason, RoastEventKind, RoastEventSource

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
    from roastpilot_agent.models import DropReason, RoastEventKind, RoastEventSource

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


# --- aggregate_stats / rendering -----------------------------------------------


def test_aggregate_stats_empty_corpus() -> None:
    stats = scorer.aggregate_stats(scorer.CorpusReport(scored=[], skipped=[]))
    assert stats == {
        "n_scored": 0,
        "n_skipped": 0,
        "hits": 0,
        "hit_rate": 0.0,
        "mean_scalar": 0.0,
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

        table = scorer.render_markdown_table(report)
        assert "HIT" in table
        assert "MISS" in table
        assert "5★" in table  # rated row
        assert "—" in table  # unrated row + skipped columns
        assert "no development-phase telemetry row" in table
        assert "N scored: 2 (skipped: 1)" in table
        assert "HIT: 1/2 (50.0%)" in table

        payload = scorer.report_to_json(report)
        assert payload["aggregate"] == stats
        assert len(payload["runs"]) == 2
        assert len(payload["skipped"]) == 1
        run_hit_entry = next(r for r in payload["runs"] if r["run_id"] == "run-hit")
        assert run_hit_entry["hit"] is True
        assert run_hit_entry["bean_name"] == "Guatemala Conebosque"
        assert run_hit_entry["operator_rating"] == 5
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
