"""Tests for the store → replay-fixture converter (#300, D44).

Privacy invariant (AGENTS.md): real roast stores are the operator's personal
data and are NEVER committed, so these tests build a **synthetic** store in-test
via the real ``store.py`` write API — a handful of fabricated ticks, the three
roast marks, completion, and an operator rating — then convert it and assert the
output is a valid bake-off fixture that ``bakeoff_replay.load_roast`` parses and
whose ``summary.json`` carries the operator rating + degree label. The real
roast-2 / roast-3 ingestion is a MANUAL validation note in the PR body (the
operator runs it against the gitignored store), not a committed test.

The shared degree helper is unit-tested here too (the boundaries that define
core_medium / soft_medium / over).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import alog_to_fixture  # noqa: E402
import bakeoff_replay  # noqa: E402
import store_to_fixture as s2f  # noqa: E402
from roast_degree import classify_degree  # noqa: E402

from roastpilot_agent.config import AppConfig  # noqa: E402
from roastpilot_agent.models import (  # noqa: E402
    RoastEventKind,
    RoastEventSource,
    RoastPhase,
    RoastProfile,
    RoastTelemetry,
)
from roastpilot_agent.store import RoastStore  # noqa: E402

_PROFILE = RoastProfile(
    name="store-to-fixture-test",
    bean_origin="Ethiopia",
    bean_weight_grams=250.0,
    initial_heat_percent=100,
    initial_fan_percent=30,
    target_drop_temp_c=195.0,
    target_development_percent=20.0,
)


async def _synthetic_store(
    db_path: Path,
    *,
    run_id: str = "synthetic-run",
    rating: int | None = 4,
    notes: str | None = "bright, clean",
    drop_bean_temp_c: float = 194.0,
) -> RoastStore:
    """Build a small completed roast in a fresh store via the real write API.

    Lays down a charge → first-crack → drop timeline: telemetry every 5 s on the
    controller clock, a ``t0_detected`` / ``first_crack`` / ``run_completed``
    event at the matching instants, run completion, and (optionally) an operator
    rating. The drop telemetry row is forced to ``drop_bean_temp_c`` so the degree
    classification is deterministic.

    Args:
        db_path: Where to create the store.
        run_id: The run id to seed.
        rating: The operator rating to stamp (or ``None`` to leave unrated).
        notes: The operator notes.
        drop_bean_temp_c: Bean temperature at the drop instant.

    Returns:
        The initialized, populated (still-open) store.
    """
    store = RoastStore(db_path=db_path)
    await store.initialize()
    await store.create_run(
        run_id=run_id,
        profile=_PROFILE,
        config=AppConfig(),
        agent_phase=RoastPhase.STARTING,
    )

    charge_s, first_crack_s, drop_s = 60.0, 600.0, 720.0
    # Telemetry every 5 s from charge through drop; a gentle ramp to the drop temp.
    tick = 0
    elapsed = 0.0
    while elapsed <= drop_s:
        if abs(elapsed - drop_s) < 1e-6:
            bean = drop_bean_temp_c
        else:
            bean = 60.0 + (drop_bean_temp_c - 60.0) * (elapsed / drop_s)
        await store.record_telemetry(
            run_id=run_id,
            tick=tick,
            agent_phase=RoastPhase.DEVELOPMENT,
            elapsed_seconds=elapsed,
            interval_seconds=0.0,  # write every row in the test
            telemetry=RoastTelemetry(bean_temp_c=bean, env_temp_c=bean + 20.0),
            heat_level_percent=100 if elapsed < first_crack_s else 40,
            fan_level_percent=30,
            development_percent=None,
        )
        tick += 1
        elapsed += 5.0

    for kind, when in (
        (RoastEventKind.T0_DETECTED, charge_s),
        (RoastEventKind.FIRST_CRACK, first_crack_s),
        (RoastEventKind.RUN_COMPLETED, drop_s),
    ):
        await store.record_event(
            run_id=run_id,
            kind=kind,
            source=RoastEventSource.CONTROLLER,
            monotonic_seconds=when,
        )

    await store.complete_run(run_id=run_id, outcome="completed", agent_phase=RoastPhase.COMPLETE)
    if rating is not None:
        await store.set_operator_rating(run_id, rating=rating, notes=notes)  # type: ignore[arg-type]
    return store


@pytest.mark.asyncio
async def test_converter_emits_a_loadable_labelled_fixture(tmp_path: Path) -> None:
    """The end-to-end acceptance: synthetic store → fixture → load_roast + label."""
    db_path = tmp_path / "synthetic.sqlite3"
    store = await _synthetic_store(db_path, rating=4, notes="bright, clean")
    await store.close()  # release WAL so the read-only converter sees a settled file

    out_dir = tmp_path / "fixture"
    entry = s2f.convert(db_path, out_dir)

    # The fixture parses through the real bake-off loader (the contract that
    # matters — the bake-off needs zero changes for a store-sourced fixture).
    fixture = out_dir / "roast.jsonl"
    telemetry, ground = bakeoff_replay.load_roast(fixture)
    assert telemetry, "expected telemetry rows"
    assert ground.drop_temp_c == 194.0
    assert ground.first_crack_seconds == 600.0
    assert ground.drop_seconds == 720.0
    assert ground.t0_seconds == 60.0

    # summary.json carries the #300 outcome label.
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["operator_rating"] == 4
    assert summary["operator_notes"] == "bright, clean"
    assert summary["degree"] == "core_medium"  # drop 194 ≤ 195
    assert summary["source"] == "agent-store"
    assert summary["drop_temp_c"] == 194.0
    # Parity with alog_to_fixture's summary keys.
    for key in (
        "active",
        "phase",
        "roaster_driver",
        "first_crack_temp_c",
        "development_time_seconds",
        "development_time_percent",
        "total_roast_seconds",
    ):
        assert key in summary

    assert entry["run_id"] == "synthetic-run"
    assert entry["operator_rating"] == 4
    assert entry["degree"] == "core_medium"


@pytest.mark.asyncio
async def test_unrated_roast_carries_null_label(tmp_path: Path) -> None:
    """An unrated roast still converts; the label fields are null, not missing."""
    db_path = tmp_path / "unrated.sqlite3"
    store = await _synthetic_store(db_path, rating=None, notes=None, drop_bean_temp_c=196.5)
    await store.close()

    out_dir = tmp_path / "fixture"
    s2f.convert(db_path, out_dir)
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["operator_rating"] is None
    assert summary["operator_notes"] is None
    assert summary["degree"] == "soft_medium"  # 195 < 196.5 ≤ 197


@pytest.mark.asyncio
async def test_default_run_is_most_recent_completed(tmp_path: Path) -> None:
    """With no --run-id, the converter picks the most-recent completed run."""
    db_path = tmp_path / "two-runs.sqlite3"
    store = await _synthetic_store(db_path, run_id="older", rating=3)
    # A second, later-completed run in the same store.
    await store.create_run(
        run_id="newer",
        profile=_PROFILE,
        config=AppConfig(),
        agent_phase=RoastPhase.STARTING,
    )
    await store.record_telemetry(
        run_id="newer",
        tick=0,
        agent_phase=RoastPhase.DEVELOPMENT,
        elapsed_seconds=0.0,
        interval_seconds=0.0,
        telemetry=RoastTelemetry(bean_temp_c=60.0, env_temp_c=80.0),
        heat_level_percent=100,
        fan_level_percent=30,
    )
    await store.record_telemetry(
        run_id="newer",
        tick=1,
        agent_phase=RoastPhase.DEVELOPMENT,
        elapsed_seconds=120.0,
        interval_seconds=0.0,
        telemetry=RoastTelemetry(bean_temp_c=190.0, env_temp_c=210.0),
        heat_level_percent=40,
        fan_level_percent=30,
    )
    for kind, when in (
        (RoastEventKind.T0_DETECTED, 0.0),
        (RoastEventKind.FIRST_CRACK, 90.0),
        (RoastEventKind.RUN_COMPLETED, 120.0),
    ):
        await store.record_event(
            run_id="newer",
            kind=kind,
            source=RoastEventSource.CONTROLLER,
            monotonic_seconds=when,
        )
    await store.complete_run(run_id="newer", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    await store.close()

    entry = s2f.convert(db_path, tmp_path / "fixture")
    assert entry["run_id"] == "newer"


@pytest.mark.asyncio
async def test_missing_marks_raises(tmp_path: Path) -> None:
    """A completed run without the FC/drop marks is not a scorable fixture."""
    db_path = tmp_path / "no-marks.sqlite3"
    store = RoastStore(db_path=db_path)
    await store.initialize()
    await store.create_run(
        run_id="bare", profile=_PROFILE, config=AppConfig(), agent_phase=RoastPhase.STARTING
    )
    await store.record_telemetry(
        run_id="bare",
        tick=0,
        agent_phase=RoastPhase.PREHEATING,
        elapsed_seconds=0.0,
        interval_seconds=0.0,
        telemetry=RoastTelemetry(bean_temp_c=60.0, env_temp_c=80.0),
        heat_level_percent=100,
        fan_level_percent=30,
    )
    await store.complete_run(run_id="bare", outcome="aborted", agent_phase=RoastPhase.COMPLETE)
    await store.close()

    with pytest.raises(s2f.FixtureConversionError, match="required marks"):
        s2f.convert(db_path, tmp_path / "fixture")


@pytest.mark.asyncio
async def test_explicit_unknown_run_id_raises(tmp_path: Path) -> None:
    """An explicit run id that is not a completed run raises, not silently picks another."""
    db_path = tmp_path / "one-run.sqlite3"
    store = await _synthetic_store(db_path, run_id="real")
    await store.close()
    with pytest.raises(s2f.FixtureConversionError, match="no completed roast_run"):
        s2f.convert(db_path, tmp_path / "fixture", run_id="ghost")


def test_missing_store_file_raises(tmp_path: Path) -> None:
    """A nonexistent store path fails loudly (never creates an empty database)."""
    with pytest.raises(FileNotFoundError):
        s2f.read_store_roast(tmp_path / "nope.sqlite3")


def test_empty_store_raises(tmp_path: Path) -> None:
    """A store with no completed runs raises rather than emitting an empty fixture."""
    import sqlite3

    db_path = tmp_path / "empty.sqlite3"
    # Minimal roast_runs table with no rows (read-only path needs the table).
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE roast_runs (id TEXT PRIMARY KEY, completed_at_utc TEXT, rowid_x INTEGER)"
    )
    connection.commit()
    connection.close()
    with pytest.raises(s2f.FixtureConversionError, match="no completed roast_runs"):
        s2f.read_store_roast(db_path)


@pytest.mark.parametrize(
    ("drop_temp_c", "expected"),
    [
        (180.0, "core_medium"),
        (195.0, "core_medium"),  # boundary inclusive
        (195.01, "soft_medium"),
        (196.5, "soft_medium"),
        (197.0, "soft_medium"),  # boundary inclusive
        (197.01, "over"),
        (203.0, "over"),
    ],
)
def test_classify_degree_boundaries(drop_temp_c: float, expected: str) -> None:
    """The degree thresholds: ≤195 core, (195,197] soft, >197 over."""
    assert classify_degree(drop_temp_c) == expected


@pytest.mark.asyncio
async def test_telemetry_seconds_falls_back_to_tick_when_elapsed_null(tmp_path: Path) -> None:
    """A pre-v5 row with NULL elapsed_seconds origins at tick × tick_interval."""
    db_path = tmp_path / "null-elapsed.sqlite3"
    store = RoastStore(db_path=db_path)
    await store.initialize()
    await store.create_run(
        run_id="r", profile=_PROFILE, config=AppConfig(), agent_phase=RoastPhase.STARTING
    )
    # Force a NULL elapsed_seconds by writing the snapshot row directly (the
    # store's write API always supplies elapsed; a pre-v5 row could be NULL).
    for tick, bean in ((0, 60.0), (120, 194.0)):
        await store.connection.execute(
            "INSERT INTO telemetry_snapshots (run_id, tick, recorded_at_utc, elapsed_seconds,"
            " agent_phase, bean_temp_c, env_temp_c, heat_level_percent, fan_level_percent)"
            " VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)",
            ("r", tick, "2026-01-01T00:00:00+00:00", "development", bean, bean + 20, 100, 30),
        )
    await store.connection.commit()
    for kind, when in (
        (RoastEventKind.T0_DETECTED, 0.0),
        (RoastEventKind.FIRST_CRACK, 90.0),
        (RoastEventKind.RUN_COMPLETED, 120.0),
    ):
        await store.record_event(
            run_id="r", kind=kind, source=RoastEventSource.CONTROLLER, monotonic_seconds=when
        )
    await store.complete_run(run_id="r", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    await store.close()

    out_dir = tmp_path / "fixture"
    s2f.convert(db_path, out_dir)
    telemetry, _ = bakeoff_replay.load_roast(out_dir / "roast.jsonl")
    # tick 0 → 0.0 s, tick 120 → 120.0 s (× 1.0 s tick interval).
    seconds = sorted(float(r["monotonic_seconds"]) for r in telemetry)
    assert seconds == [0.0, 120.0]


@pytest.mark.asyncio
async def test_null_temperature_rows_are_skipped(tmp_path: Path) -> None:
    """A telemetry row with no thermocouple reading is dropped, not emitted."""
    db_path = tmp_path / "null-temp.sqlite3"
    store = RoastStore(db_path=db_path)
    await store.initialize()
    await store.create_run(
        run_id="r", profile=_PROFILE, config=AppConfig(), agent_phase=RoastPhase.STARTING
    )
    # One real row + one null-temperature row (telemetry=None writes NULL temps).
    await store.record_telemetry(
        run_id="r",
        tick=0,
        agent_phase=RoastPhase.DEVELOPMENT,
        elapsed_seconds=0.0,
        interval_seconds=0.0,
        telemetry=RoastTelemetry(bean_temp_c=60.0, env_temp_c=80.0),
        heat_level_percent=100,
        fan_level_percent=30,
    )
    await store.record_telemetry(
        run_id="r",
        tick=1,
        agent_phase=RoastPhase.DEVELOPMENT,
        elapsed_seconds=60.0,
        interval_seconds=0.0,
        telemetry=None,  # no MCP reading this tick → NULL temperatures
    )
    await store.record_telemetry(
        run_id="r",
        tick=2,
        agent_phase=RoastPhase.DEVELOPMENT,
        elapsed_seconds=120.0,
        interval_seconds=0.0,
        telemetry=RoastTelemetry(bean_temp_c=194.0, env_temp_c=214.0),
        heat_level_percent=40,
        fan_level_percent=30,
    )
    for kind, when in (
        (RoastEventKind.T0_DETECTED, 0.0),
        (RoastEventKind.FIRST_CRACK, 90.0),
        (RoastEventKind.RUN_COMPLETED, 120.0),
    ):
        await store.record_event(
            run_id="r", kind=kind, source=RoastEventSource.CONTROLLER, monotonic_seconds=when
        )
    await store.complete_run(run_id="r", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    await store.close()

    entry = s2f.convert(db_path, tmp_path / "fix")
    telemetry, _ = bakeoff_replay.load_roast(Path(str(entry["fixture"])))
    assert len(telemetry) == 2  # the null-temperature middle row is skipped


@pytest.mark.asyncio
async def test_cli_main_happy_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI converts the latest run and prints a one-line summary, exit 0."""
    db_path = tmp_path / "cli.sqlite3"
    store = await _synthetic_store(db_path, rating=5)
    await store.close()
    out_dir = tmp_path / "out"
    code = s2f.main([str(db_path), "--out-dir", str(out_dir)])
    assert code == 0
    assert (out_dir / "roast.jsonl").exists()
    assert (out_dir / "summary.json").exists()
    captured = capsys.readouterr()
    assert "converted run synthetic-run" in captured.out


def test_cli_main_error_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A missing store makes the CLI print an error to stderr and exit 1."""
    code = s2f.main([str(tmp_path / "nope.sqlite3"), "--out-dir", str(tmp_path / "out")])
    assert code == 1
    assert "error:" in capsys.readouterr().err


async def _completed_run_with_events_only(db_path: Path, run_id: str = "r") -> RoastStore:
    """A completed run carrying the three marks but no telemetry at all."""
    store = RoastStore(db_path=db_path)
    await store.initialize()
    await store.create_run(
        run_id=run_id, profile=_PROFILE, config=AppConfig(), agent_phase=RoastPhase.STARTING
    )
    for kind, when in (
        (RoastEventKind.T0_DETECTED, 0.0),
        (RoastEventKind.FIRST_CRACK, 90.0),
        (RoastEventKind.RUN_COMPLETED, 120.0),
    ):
        await store.record_event(
            run_id=run_id, kind=kind, source=RoastEventSource.CONTROLLER, monotonic_seconds=when
        )
    await store.complete_run(run_id=run_id, outcome="completed", agent_phase=RoastPhase.COMPLETE)
    return store


@pytest.mark.asyncio
async def test_no_telemetry_raises(tmp_path: Path) -> None:
    """A completed run with marks but zero telemetry is not a scorable fixture."""
    db_path = tmp_path / "no-telemetry.sqlite3"
    store = await _completed_run_with_events_only(db_path)
    await store.close()
    with pytest.raises(s2f.FixtureConversionError, match="no telemetry snapshots"):
        s2f.convert(db_path, tmp_path / "fixture")


@pytest.mark.asyncio
async def test_all_null_temperature_telemetry_raises(tmp_path: Path) -> None:
    """Telemetry rows that all lack a thermocouple reading leave no scorable rows."""
    db_path = tmp_path / "all-null-temp.sqlite3"
    store = await _completed_run_with_events_only(db_path)
    for tick, elapsed in ((0, 0.0), (1, 60.0)):
        await store.record_telemetry(
            run_id="r",
            tick=tick,
            agent_phase=RoastPhase.DEVELOPMENT,
            elapsed_seconds=elapsed,
            interval_seconds=0.0,
            telemetry=None,  # NULL temperatures every tick
        )
    await store.close()
    with pytest.raises(s2f.FixtureConversionError, match="no telemetry rows with temperature"):
        s2f.convert(db_path, tmp_path / "fixture")


def _synthetic_alog(out: Path, *, drop_bt: float) -> Path:
    """Write a tiny synthetic Artisan ``.alog`` (a Python dict literal).

    A 0/300/360 s charge / first-crack / drop timeline with a bean ramp ending at
    ``drop_bt`` — enough for ``alog_to_fixture`` to resolve its marks.

    Args:
        out: The directory to write ``synthetic.alog`` into.
        drop_bt: Bean temperature at the drop sample.

    Returns:
        The written ``.alog`` path.
    """
    timex = [0.0, 300.0, 360.0]
    temp2 = [60.0, 178.0, drop_bt]
    temp1 = [80.0, 198.0, drop_bt + 20.0]
    # timeindex = [CHARGE, DRYe, FCs, FCe, SCs, SCe, DROP, COOL] as indices into timex.
    timeindex = [0, 0, 1, 0, 0, 0, 2, 0]
    profile = {
        "timex": timex,
        "temp1": temp1,
        "temp2": temp2,
        "timeindex": timeindex,
        "specialevents": [],
        "specialeventstype": [],
        "specialeventsvalue": [],
    }
    path = out / "synthetic.alog"
    path.write_text(repr(profile), encoding="utf-8")
    return path


def test_alog_summary_carries_the_shared_label_fields(tmp_path: Path) -> None:
    """The .alog adapter emits the same #300 label fields (null rating, degree)."""
    alog = _synthetic_alog(tmp_path, drop_bt=196.5)
    alog_to_fixture.convert(alog, tmp_path / "out", label="syn", origin="synthetic")
    summary = json.loads((tmp_path / "out" / "syn" / "summary.json").read_text())
    assert summary["operator_rating"] is None  # an .alog has no operator rating
    assert summary["operator_notes"] is None
    assert summary["degree"] == "soft_medium"  # drop 196.5 → (195, 197]
    assert summary["source"] == "artisan-alog"


def test_alog_and_store_summaries_share_their_key_set(tmp_path: Path) -> None:
    """Parity invariant: both adapters emit the SAME summary keys.

    The bake-off must read either source identically, so the key set must not
    drift between ``alog_to_fixture`` and ``store_to_fixture``.
    """
    alog = _synthetic_alog(tmp_path, drop_bt=193.0)
    alog_to_fixture.convert(alog, tmp_path / "alog-out", label="syn", origin="synthetic")
    alog_summary = json.loads((tmp_path / "alog-out" / "syn" / "summary.json").read_text())

    db_path = tmp_path / "store.sqlite3"
    store = asyncio.run(_synthetic_store(db_path, rating=5))
    asyncio.run(store.close())
    s2f.convert(db_path, tmp_path / "store-out")
    store_summary = json.loads((tmp_path / "store-out" / "summary.json").read_text())

    assert alog_summary.keys() == store_summary.keys()
