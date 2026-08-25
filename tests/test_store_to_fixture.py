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
import sqlite3
import sys
from datetime import UTC, datetime, timedelta, timezone
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


# The REAL store's two clocks (verified against ~/.local/state/roastpilot on the
# real roasts fa24e673 / c3b84625): roast_events.monotonic_seconds is ABSOLUTE
# time.monotonic() (hundreds of thousands of seconds — process uptime), while
# telemetry_snapshots.elapsed_seconds is run-relative (≈0 at the first tick).
# The synthetic store MUST reproduce that offset, or it silently lets a
# same-origin bug through (the failure that shipped on the first fix). So every
# event is recorded at _MONOTONIC_OFFSET + its run-relative time; telemetry stays
# run-relative. run_started is the offset itself (its monotonic == the origin).
_MONOTONIC_OFFSET = 500_000.0


async def _record_marks(
    store: RoastStore,
    run_id: str,
    *,
    charge_s: float,
    first_crack_s: float,
    drop_s: float,
) -> None:
    """Record run_started + the three marks on the REAL two-clock layout.

    Event ``monotonic_seconds`` are ABSOLUTE (``_MONOTONIC_OFFSET`` + the
    run-relative time), mirroring the store; ``run_started`` carries the offset so
    the converter can rebase onto the run-relative telemetry clock. The drop is the
    transition into cooling (the controller's ``_drop_monotonic``, #239) — a
    ``phase_changed`` event with ``{"phase": "cooling"}`` — NOT ``run_completed``.
    Shared by the tests that only need a valid drop mark.
    """
    await store.record_event(
        run_id=run_id,
        kind=RoastEventKind.RUN_STARTED,
        source=RoastEventSource.CONTROLLER,
        monotonic_seconds=_MONOTONIC_OFFSET,
    )
    await store.record_event(
        run_id=run_id,
        kind=RoastEventKind.T0_DETECTED,
        source=RoastEventSource.CONTROLLER,
        monotonic_seconds=_MONOTONIC_OFFSET + charge_s,
    )
    await store.record_event(
        run_id=run_id,
        kind=RoastEventKind.FIRST_CRACK,
        source=RoastEventSource.CONTROLLER,
        monotonic_seconds=_MONOTONIC_OFFSET + first_crack_s,
        payload={"source": RoastEventSource.MCP.value},
    )
    await store.record_event(
        run_id=run_id,
        kind=RoastEventKind.PHASE_CHANGED,
        source=RoastEventSource.CONTROLLER,
        monotonic_seconds=_MONOTONIC_OFFSET + drop_s,
        payload={"phase": RoastPhase.COOLING.value},
    )


async def _synthetic_store(
    db_path: Path,
    *,
    run_id: str = "synthetic-run",
    rating: int | None = 4,
    notes: str | None = "bright, clean",
    roasted_weight_grams: float | None = None,
    drop_bean_temp_c: float = 194.0,
    cooled_bean_temp_c: float = 170.0,
    record_run_completed: bool = True,
    outcome: str | None = "completed",
) -> RoastStore:
    """Build a completed roast modelling the REAL drop→cooling→complete sequence.

    The drop is the transition INTO cooling — a ``phase_changed`` event carrying
    ``{"phase": "cooling"}`` at ``drop_bean_temp_c`` — NOT ``run_completed`` (which
    fires later, at COOLING→COMPLETE, after the bean has cooled to
    ``cooled_bean_temp_c``). So the timeline is:

    - charge (``t0_detected``) → first crack (``first_crack``) → a heating ramp to
      ``drop_bean_temp_c`` (telemetry every 5 s);
    - the drop: a ``phase_changed`` → cooling event at the ramp's peak;
    - a descending cooling tail (telemetry every 5 s down toward
      ``cooled_bean_temp_c``) the converter must NOT include;
    - (optionally) a ``run_completed`` event after the cooling tail.

    A correct converter must read the drop temp / degree from the cooling
    transition, never the cooled tail or ``run_completed`` — the gap between
    ``drop_bean_temp_c`` and ``cooled_bean_temp_c`` is what proves it.

    Args:
        db_path: Where to create the store.
        run_id: The run id to seed.
        rating: The operator rating to stamp (or ``None`` to leave unrated).
        notes: The operator notes.
        drop_bean_temp_c: Bean temperature at the drop (cooling transition).
        cooled_bean_temp_c: Bean temperature the cooling tail descends to.
        record_run_completed: Emit a ``run_completed`` event after cooling (a
            roast-2-shaped run cooled but its MCP child segfaulted before COMPLETE
            — set ``False``).
        outcome: The terminal ``complete_run`` outcome (``completed`` / ``faulted``
            / ``aborted``), or ``None`` to leave the run NEVER finalised
            (``completed_at_utc`` stays NULL). The real roast 2 (``c3b84625``) is
            the ``None`` case — its MCP child segfaulted and it was never
            restart-finalised, so it has a NULL ``completed_at_utc`` and is only
            reachable by an explicit ``--run-id``. A non-``completed`` outcome
            skips the operator rating.

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
    cooling_end_s = 960.0  # 240 s cooling tail after the drop
    tick = 0

    # Heating ramp from charge through the drop, peaking at drop_bean_temp_c.
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

    # Descending COOLING tail (the bean falls toward cooled_bean_temp_c). A correct
    # converter truncates these rows out of the fixture; including them would drag
    # drop_temp_c down off the true drop.
    elapsed = drop_s + 5.0
    while elapsed <= cooling_end_s:
        frac = (elapsed - drop_s) / (cooling_end_s - drop_s)
        bean = drop_bean_temp_c - (drop_bean_temp_c - cooled_bean_temp_c) * frac
        await store.record_telemetry(
            run_id=run_id,
            tick=tick,
            agent_phase=RoastPhase.COOLING,
            elapsed_seconds=elapsed,
            interval_seconds=0.0,
            telemetry=RoastTelemetry(bean_temp_c=bean, env_temp_c=bean + 5.0, cooling_on=True),
            heat_level_percent=0,
            fan_level_percent=100,
            development_percent=None,
        )
        tick += 1
        elapsed += 5.0

    # Events on the REAL two-clock layout: ABSOLUTE monotonic = _MONOTONIC_OFFSET +
    # the run-relative time (telemetry above stays run-relative). run_started is the
    # offset, the rebasing origin. The drop is the phase_changed→cooling transition
    # (payload phase), NOT run_completed (which lands after the cooling tail).
    await store.record_event(
        run_id=run_id,
        kind=RoastEventKind.RUN_STARTED,
        source=RoastEventSource.CONTROLLER,
        monotonic_seconds=_MONOTONIC_OFFSET,
    )
    await store.record_event(
        run_id=run_id,
        kind=RoastEventKind.T0_DETECTED,
        source=RoastEventSource.CONTROLLER,
        monotonic_seconds=_MONOTONIC_OFFSET + charge_s,
    )
    await store.record_event(
        run_id=run_id,
        kind=RoastEventKind.FIRST_CRACK,
        source=RoastEventSource.CONTROLLER,
        monotonic_seconds=_MONOTONIC_OFFSET + first_crack_s,
        payload={"source": RoastEventSource.MCP.value},
    )
    await store.record_event(
        run_id=run_id,
        kind=RoastEventKind.PHASE_CHANGED,
        source=RoastEventSource.CONTROLLER,
        monotonic_seconds=_MONOTONIC_OFFSET + drop_s,
        payload={"phase": RoastPhase.COOLING.value},
    )
    if record_run_completed:
        await store.record_event(
            run_id=run_id,
            kind=RoastEventKind.RUN_COMPLETED,
            source=RoastEventSource.CONTROLLER,
            monotonic_seconds=_MONOTONIC_OFFSET + cooling_end_s,
        )

    if outcome is not None:
        terminal_phase = RoastPhase.FAULTED if outcome == "faulted" else RoastPhase.COMPLETE
        await store.complete_run(
            run_id=run_id,
            outcome=outcome,  # type: ignore[arg-type]
            agent_phase=terminal_phase,
        )
        if outcome == "completed" and rating is not None:
            await store.set_operator_rating(run_id, rating=rating, notes=notes)  # type: ignore[arg-type]
        if outcome == "completed" and roasted_weight_grams is not None:
            await store.set_roasted_weight(run_id, roasted_weight_grams=roasted_weight_grams)
    return store


async def _sparse_store(db_path: Path, *, run_id: str, include_telemetry: bool) -> RoastStore:
    """Build a faulted run with only a run-start event and optional one row."""
    store = RoastStore(db_path=db_path)
    await store.initialize()
    await store.create_run(
        run_id=run_id,
        profile=_PROFILE,
        config=AppConfig(),
        agent_phase=RoastPhase.STARTING,
    )
    if include_telemetry:
        await store.record_telemetry(
            run_id=run_id,
            tick=0,
            agent_phase=RoastPhase.STARTING,
            elapsed_seconds=0.0,
            interval_seconds=0.0,
            telemetry=RoastTelemetry(bean_temp_c=20.0, env_temp_c=21.0),
            heat_level_percent=0,
            fan_level_percent=0,
            development_percent=None,
        )
    await store.record_event(
        run_id=run_id,
        kind=RoastEventKind.RUN_STARTED,
        source=RoastEventSource.CONTROLLER,
        monotonic_seconds=_MONOTONIC_OFFSET,
    )
    await store.complete_run(
        run_id=run_id,
        outcome="faulted",
        agent_phase=RoastPhase.FAULTED,
    )
    return store


_BACKDATED_ORIGIN = datetime.fromisoformat("2026-08-10T20:00:00+00:00")
_BACKDATED_WALL_SKEW = 0.13
_BACKDATED_T0_SECONDS = 49.0
_BACKDATED_FIRST_CRACK_SECONDS = 575.0
_BACKDATED_DROP_SECONDS = 720.0
_BACKDATED_FROZEN_DTR = (
    (_BACKDATED_DROP_SECONDS - _BACKDATED_FIRST_CRACK_SECONDS)
    / (_BACKDATED_DROP_SECONDS - _BACKDATED_T0_SECONDS)
    * 100.0
)


def _backdated_wall(seconds: float) -> str:
    """Map a run-relative instant onto the synthetic wall clock, including skew."""
    return (_BACKDATED_ORIGIN + timedelta(seconds=seconds + _BACKDATED_WALL_SKEW)).isoformat()


async def _backdated_store(
    db_path: Path,
    *,
    run_id: str = "backdated-run",
    include_t0_anchor: bool = True,
    include_first_crack_anchor: bool = True,
) -> RoastStore:
    """Build a store whose event rows confirm T0/FC after their true onsets.

    The event rows retain the baseline 60/600/720-second marks. Telemetry wall
    time is rewritten to ``origin + elapsed + 0.13 s`` so the preferred UTC
    anchors at 49/575 seconds must be mapped between clocks. The frozen achieved
    DTR on drop/cooling rows records the controller's backdated truth.
    """
    store = await _synthetic_store(db_path, run_id=run_id)
    cursor = await store.connection.execute(
        "SELECT id, elapsed_seconds FROM telemetry_snapshots"
        " WHERE run_id = ? ORDER BY tick ASC, id ASC",
        (run_id,),
    )
    rows = await cursor.fetchall()
    for row in rows:
        elapsed = float(row["elapsed_seconds"])
        await store.connection.execute(
            "UPDATE telemetry_snapshots SET recorded_at_utc = ? WHERE id = ?",
            (_backdated_wall(elapsed), int(row["id"])),
        )
    if include_first_crack_anchor:
        raw_state = json.dumps(
            {
                "first_crack_status": {
                    "detected_at_utc": _backdated_wall(_BACKDATED_FIRST_CRACK_SECONDS)
                }
            }
        )
        await store.connection.execute(
            "UPDATE telemetry_snapshots SET raw_state_json = ?"
            " WHERE run_id = ? AND elapsed_seconds >= 600.0",
            (raw_state, run_id),
        )
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET development_percent = ?"
        " WHERE run_id = ? AND elapsed_seconds >= ?",
        (_BACKDATED_FROZEN_DTR, run_id, _BACKDATED_DROP_SECONDS),
    )
    await store.connection.commit()
    if include_t0_anchor:
        await store.record_t0_detected_at(
            run_id, t0_detected_at_utc=_backdated_wall(_BACKDATED_T0_SECONDS)
        )
    return store


@pytest.mark.asyncio
async def test_backdated_anchors_match_frozen_development_truth(tmp_path: Path) -> None:
    """T1: exported marks and DTR use the controller's backdated truth."""
    db_path = tmp_path / "backdated.sqlite3"
    store = await _backdated_store(db_path)
    await store.close()

    out_dir = tmp_path / "fixture"
    s2f.convert(db_path, out_dir)
    summary = json.loads((out_dir / "summary.json").read_text())
    _, ground = bakeoff_replay.load_roast(out_dir / "roast.jsonl")

    assert summary["development_time_percent"] == pytest.approx(_BACKDATED_FROZEN_DTR, abs=0.06)
    assert summary["development_time_seconds"] == 145.0
    assert ground.t0_seconds == 49.0
    assert ground.first_crack_seconds == 575.0
    assert ground.drop_seconds == 720.0
    assert ground.development_time_ratio == pytest.approx(145.0 / 671.0)


@pytest.mark.asyncio
async def test_first_crack_temperature_uses_backdated_onset(tmp_path: Path) -> None:
    """T2: FC temperature comes from the onset row, not confirmation."""
    db_path = tmp_path / "backdated-temp.sqlite3"
    store = await _backdated_store(db_path)
    await store.close()

    out_dir = tmp_path / "fixture"
    s2f.convert(db_path, out_dir)
    summary = json.loads((out_dir / "summary.json").read_text())
    onset_temp = round(60.0 + (194.0 - 60.0) * (575.0 / 720.0), 1)
    confirmation_temp = round(60.0 + (194.0 - 60.0) * (600.0 / 720.0), 1)

    assert summary["first_crack_temp_c"] == onset_temp
    assert summary["first_crack_temp_c"] < confirmation_temp


@pytest.mark.asyncio
async def test_backdated_manifest_reports_preferred_anchor_provenance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T3: preferred anchors are visible and do not emit fallback warnings."""
    db_path = tmp_path / "backdated-provenance.sqlite3"
    store = await _backdated_store(db_path)
    await store.close()

    entry = s2f.convert(db_path, tmp_path / "fixture")

    assert entry["charge_anchor"] == "run_row_utc"
    assert entry["first_crack_anchor"] == "fc_status_utc"
    assert entry["development_time_percent"] == pytest.approx(21.6)
    assert capsys.readouterr().err == ""


@pytest.mark.asyncio
async def test_utc_mapping_cancels_constant_wall_clock_skew(tmp_path: Path) -> None:
    """T4: nearest telemetry anchoring cancels the injected 0.13-second skew."""
    db_path = tmp_path / "backdated-skew.sqlite3"
    store = await _backdated_store(db_path)
    await store.close()

    roast = s2f.read_store_roast(db_path)

    assert roast.charge_seconds == 49.0
    assert roast.first_crack_seconds == 575.0


def test_utc_mapping_handles_iso_variants_bad_rows_and_ties(tmp_path: Path) -> None:
    """UTC mapping accepts Z/naive values, skips bad rows, and breaks ties early."""
    db_path = tmp_path / "mapping-edges.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE telemetry_snapshots ("
        "id INTEGER PRIMARY KEY, run_id TEXT, tick INTEGER,"
        "recorded_at_utc TEXT, elapsed_seconds REAL)"
    )
    connection.executemany(
        "INSERT INTO telemetry_snapshots"
        " (run_id, tick, recorded_at_utc, elapsed_seconds) VALUES (?, ?, ?, ?)",
        [
            ("r", 0, "2026-08-10T20:00:00", 0.0),
            ("r", 1, "not-a-date", 50.0),
            ("r", 2, "2026-08-10T20:00:10Z", 100.0),
        ],
    )
    connection.commit()

    # Target is equidistant from ticks 0 and 2. The earlier tick wins, mapping
    # to 0 + 5 rather than 100 - 5; the malformed middle row is ignored.
    assert (
        s2f._utc_to_run_seconds(  # pyright: ignore[reportPrivateUsage]
            connection, "r", "2026-08-10T20:00:05Z"
        )
        == 5.0
    )
    assert (
        s2f._utc_to_run_seconds(  # pyright: ignore[reportPrivateUsage]
            connection, "r", "2026-08-10T21:00:05+01:00"
        )
        == 5.0
    )
    assert (
        s2f._utc_to_run_seconds(  # pyright: ignore[reportPrivateUsage]
            connection, "r", "not-a-date"
        )
        is None
    )
    connection.close()


@pytest.mark.asyncio
async def test_first_crack_source_absent_falls_back_with_direction_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T5: absent FC onset falls back and warns that DTR is understated."""
    db_path = tmp_path / "fc-fallback.sqlite3"
    store = await _backdated_store(db_path, include_first_crack_anchor=False)
    await store.close()

    entry = s2f.convert(db_path, tmp_path / "fixture")
    warning = capsys.readouterr().err

    assert entry["first_crack_anchor"] == "event_row"
    assert "backdated-run" in warning
    assert "first crack" in warning
    assert "source absent" in warning
    assert "DTR understated" in warning


@pytest.mark.asyncio
async def test_t0_source_absent_falls_back_with_direction_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T6: NULL run-row T0 falls back without blocking export."""
    db_path = tmp_path / "t0-fallback.sqlite3"
    store = await _backdated_store(db_path, include_t0_anchor=False)
    await store.close()

    entry = s2f.convert(db_path, tmp_path / "fixture")
    warning = capsys.readouterr().err

    assert entry["charge_anchor"] == "event_row"
    assert entry["development_time_percent"] == pytest.approx(21.97, abs=0.05)
    assert "backdated-run" in warning
    assert "charge" in warning
    assert "source absent" in warning
    assert "DTR overstated" in warning


@pytest.mark.asyncio
async def test_unparseable_first_crack_onset_falls_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T7: an unparseable FC timestamp warns and uses the event row."""
    db_path = tmp_path / "fc-unparseable.sqlite3"
    store = await _backdated_store(db_path)
    raw_state = json.dumps({"first_crack_status": {"detected_at_utc": "not-a-date"}})
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET raw_state_json = ?"
        " WHERE run_id = ? AND raw_state_json IS NOT NULL",
        (raw_state, "backdated-run"),
    )
    await store.connection.commit()
    await store.close()

    entry = s2f.convert(db_path, tmp_path / "fixture")
    warning = capsys.readouterr().err

    assert entry["first_crack_anchor"] == "event_row"
    assert "source unparseable" in warning


@pytest.mark.asyncio
async def test_malformed_raw_state_row_does_not_abort_onset_scan(tmp_path: Path) -> None:
    """T8: malformed JSON is skipped when a later row has a valid onset."""
    db_path = tmp_path / "fc-malformed-json.sqlite3"
    store = await _backdated_store(db_path)
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET raw_state_json = '{malformed'"
        " WHERE run_id = ? AND elapsed_seconds = 600.0",
        ("backdated-run",),
    )
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET raw_state_json = '[]'"
        " WHERE run_id = ? AND elapsed_seconds = 605.0",
        ("backdated-run",),
    )
    await store.connection.commit()
    await store.close()

    entry = s2f.convert(db_path, tmp_path / "fixture")

    assert entry["first_crack_anchor"] == "fc_status_utc"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_state",
    [
        {},
        {"first_crack_status": {}},
        {"first_crack_status": {"detected_at_utc": None}},
    ],
)
async def test_missing_first_crack_status_path_falls_back(
    tmp_path: Path, raw_state: dict[str, object]
) -> None:
    """T9: missing/null FC status paths are tolerated as source absence."""
    db_path = tmp_path / "fc-missing-path.sqlite3"
    store = await _backdated_store(db_path)
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET raw_state_json = ?"
        " WHERE run_id = ? AND raw_state_json IS NOT NULL",
        (json.dumps(raw_state), "backdated-run"),
    )
    await store.connection.commit()
    await store.close()

    entry = s2f.convert(db_path, tmp_path / "fixture")

    assert entry["first_crack_anchor"] == "event_row"


@pytest.mark.asyncio
async def test_distinct_first_crack_onsets_choose_earliest_with_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T10: resumed-session FC onsets choose the earliest and warn loudly."""
    db_path = tmp_path / "fc-ambiguous.sqlite3"
    store = await _backdated_store(db_path)
    second_onset = _backdated_wall(574.0)
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET raw_state_json = ?"
        " WHERE run_id = ? AND elapsed_seconds >= 650.0",
        (
            json.dumps({"first_crack_status": {"detected_at_utc": second_onset}}),
            "backdated-run",
        ),
    )
    await store.connection.commit()
    await store.close()

    out_dir = tmp_path / "fixture"
    entry = s2f.convert(db_path, out_dir)
    warning = capsys.readouterr().err
    _, ground = bakeoff_replay.load_roast(out_dir / "roast.jsonl")

    assert entry["first_crack_anchor"] == "fc_status_utc"
    assert ground.first_crack_seconds == 574.0
    assert "2 distinct onset values" in warning
    assert f"chose earliest {second_onset}" in warning


@pytest.mark.asyncio
async def test_first_crack_onset_without_accepted_event_is_not_exportable(
    tmp_path: Path,
) -> None:
    """A raw MCP onset cannot fabricate a mark the controller never accepted."""
    db_path = tmp_path / "fc-onset-without-event.sqlite3"
    store = await _backdated_store(db_path)
    await store.connection.execute(
        "DELETE FROM roast_events WHERE run_id = ? AND kind = ?",
        ("backdated-run", RoastEventKind.FIRST_CRACK.value),
    )
    await store.connection.commit()
    await store.close()

    with pytest.raises(
        s2f.FixtureConversionError,
        match=r"lacks required marks: \['first_crack_detected'\]",
    ):
        s2f.convert(db_path, tmp_path / "fixture")


@pytest.mark.asyncio
async def test_operator_first_crack_event_overrides_pending_mcp_onset(tmp_path: Path) -> None:
    """An operator acceptance remains authoritative over pending MCP state."""
    db_path = tmp_path / "operator-first-crack.sqlite3"
    store = await _backdated_store(db_path)
    await store.connection.execute(
        "UPDATE roast_events SET payload_json = ? WHERE run_id = ? AND kind = ?",
        (
            json.dumps({"bean_temp_c": 171.7, "source": RoastEventSource.OPERATOR.value}),
            "backdated-run",
            RoastEventKind.FIRST_CRACK.value,
        ),
    )
    await store.connection.commit()
    await store.close()

    out_dir = tmp_path / "fixture"
    entry = s2f.convert(db_path, out_dir)
    summary = json.loads((out_dir / "summary.json").read_text())
    _, ground = bakeoff_replay.load_roast(out_dir / "roast.jsonl")
    confirmation_temp = round(60.0 + (194.0 - 60.0) * (600.0 / 720.0), 1)

    assert entry["first_crack_anchor"] == "event_row"
    assert ground.first_crack_seconds == 600.0
    assert ground.first_crack_seconds != _BACKDATED_FIRST_CRACK_SECONDS
    assert summary["first_crack_temp_c"] == confirmation_temp
    assert summary["development_time_percent"] == pytest.approx(17.9, abs=0.05)


@pytest.mark.asyncio
async def test_mcp_first_crack_event_still_uses_status_onset(tmp_path: Path) -> None:
    """An explicitly MCP-sourced acceptance retains the preferred onset."""
    db_path = tmp_path / "mcp-first-crack.sqlite3"
    store = await _backdated_store(db_path)
    await store.close()

    out_dir = tmp_path / "fixture"
    entry = s2f.convert(db_path, out_dir)
    _, ground = bakeoff_replay.load_roast(out_dir / "roast.jsonl")

    assert entry["first_crack_anchor"] == "fc_status_utc"
    assert ground.first_crack_seconds == _BACKDATED_FIRST_CRACK_SECONDS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload_json",
    [
        pytest.param(None, id="missing-payload"),
        pytest.param(json.dumps({}), id="missing-source"),
        pytest.param(json.dumps({"source": None}), id="null-source"),
        pytest.param(json.dumps({"source": "unknown"}), id="unknown-source"),
        pytest.param(json.dumps({"source": "MCP"}), id="wrong-case"),
        pytest.param(json.dumps({"source": 7}), id="non-string-source"),
        pytest.param(json.dumps([]), id="non-object-payload"),
        pytest.param("{malformed", id="malformed-payload"),
    ],
)
async def test_non_mcp_first_crack_provenance_uses_event_row(
    tmp_path: Path, payload_json: str | None
) -> None:
    """Missing, malformed, or unrecognised provenance fails safe to the event."""
    db_path = tmp_path / "non-mcp-first-crack.sqlite3"
    store = await _backdated_store(db_path)
    await store.connection.execute(
        "UPDATE roast_events SET payload_json = ? WHERE run_id = ? AND kind = ?",
        (payload_json, "backdated-run", RoastEventKind.FIRST_CRACK.value),
    )
    await store.connection.commit()
    await store.close()

    out_dir = tmp_path / "fixture"
    entry = s2f.convert(db_path, out_dir)
    _, ground = bakeoff_replay.load_roast(out_dir / "roast.jsonl")

    assert entry["first_crack_anchor"] == "event_row"
    assert ground.first_crack_seconds == 600.0


@pytest.mark.asyncio
async def test_pre_schema_v3_store_falls_back_for_charge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T11: stores without t0_detected_at_utc retain event-row compatibility."""
    db_path = tmp_path / "pre-v3.sqlite3"
    store = await _backdated_store(db_path)
    await store.close()
    connection = sqlite3.connect(db_path)
    connection.execute("ALTER TABLE roast_runs DROP COLUMN t0_detected_at_utc")
    connection.commit()
    connection.close()

    entry = s2f.convert(db_path, tmp_path / "fixture")
    warning = capsys.readouterr().err

    assert entry["charge_anchor"] == "event_row"
    assert "charge" in warning
    assert "source absent" in warning


@pytest.mark.asyncio
async def test_unmappable_utc_anchors_fall_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T12: preferred UTC sources fall back when telemetry has no elapsed clock."""
    db_path = tmp_path / "unmappable.sqlite3"
    store = await _backdated_store(db_path)
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET elapsed_seconds = NULL WHERE run_id = ?",
        ("backdated-run",),
    )
    await store.connection.commit()
    await store.close()

    entry = s2f.convert(db_path, tmp_path / "fixture")
    warning = capsys.readouterr().err

    assert entry["charge_anchor"] == "event_row"
    assert entry["first_crack_anchor"] == "event_row"
    assert warning.count("source unmappable") == 2


@pytest.mark.asyncio
async def test_negative_utc_mapping_falls_back_instead_of_exporting_negative_mark(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A preferred instant before telemetry is unmappable, never a negative mark."""
    db_path = tmp_path / "negative-mapping.sqlite3"
    store = await _backdated_store(db_path)
    await store.connection.execute(
        "UPDATE roast_runs SET t0_detected_at_utc = ? WHERE id = ?",
        (_backdated_wall(-10.0), "backdated-run"),
    )
    await store.connection.commit()
    await store.close()

    out_dir = tmp_path / "fixture"
    entry = s2f.convert(db_path, out_dir)
    warning = capsys.readouterr().err
    _, ground = bakeoff_replay.load_roast(out_dir / "roast.jsonl")

    assert entry["charge_anchor"] == "event_row"
    assert ground.t0_seconds == 60.0
    assert ground.t0_seconds >= 0.0
    assert "run_row_utc source unmappable" in warning


@pytest.mark.asyncio
async def test_both_telemetry_clock_axes_reset_is_refused_before_output(
    tmp_path: Path,
) -> None:
    """T1: overlapping tick/elapsed restart segments refuse before mkdir."""
    db_path = tmp_path / "both-clock-axes-reset.sqlite3"
    run_id = "both-clock-axes-reset"
    store = await _synthetic_store(db_path, run_id=run_id, outcome="faulted")
    await store.connection.execute(
        "UPDATE telemetry_snapshots"
        " SET tick = tick - 100, elapsed_seconds = elapsed_seconds - 500.0"
        " WHERE run_id = ? AND tick >= 100",
        (run_id,),
    )
    await store.connection.commit()
    await store.close()

    out_dir = tmp_path / "fixture"
    with pytest.raises(s2f.FixtureConversionError) as exc_info:
        s2f.convert(db_path, out_dir, run_id)

    message = str(exc_info.value)
    assert run_id in message
    assert "tick 99 -> 0" in message
    assert "elapsed_seconds 495.0 -> 0.0" in message
    assert "agent restarts reset the run-relative clock" in message
    assert not out_dir.exists()


@pytest.mark.asyncio
async def test_elapsed_only_clock_reset_is_refused(tmp_path: Path) -> None:
    """T2: elapsed reversal is refused while ticks remain strictly increasing."""
    db_path = tmp_path / "elapsed-only-clock-reset.sqlite3"
    run_id = "elapsed-only-reset"
    store = await _synthetic_store(db_path, run_id=run_id, outcome="faulted")
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET elapsed_seconds = elapsed_seconds - 500.0"
        " WHERE run_id = ? AND tick >= 100",
        (run_id,),
    )
    await store.connection.commit()
    await store.close()

    with pytest.raises(s2f.FixtureConversionError) as exc_info:
        s2f.read_store_roast(db_path, run_id)

    message = str(exc_info.value)
    assert "elapsed_seconds 495.0 -> 0.0" in message
    assert "tick " not in message


@pytest.mark.asyncio
async def test_tick_only_clock_reset_is_refused(tmp_path: Path) -> None:
    """T3: a tick-only reset is refused even while elapsed time stays monotonic."""
    db_path = tmp_path / "tick-only-clock-reset.sqlite3"
    store = await _synthetic_store(db_path, run_id="tick-only-reset", outcome="faulted")
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET tick = tick - 100 WHERE run_id = ? AND tick >= 100",
        ("tick-only-reset",),
    )
    await store.connection.commit()
    await store.close()

    with pytest.raises(s2f.FixtureConversionError) as exc_info:
        s2f.convert(db_path, tmp_path / "fixture", "tick-only-reset")

    message = str(exc_info.value)
    assert "tick 99 -> 0" in message
    assert "elapsed_seconds" not in message


@pytest.mark.asyncio
async def test_monotonic_telemetry_clock_exports_normally(tmp_path: Path) -> None:
    """T4: a normal strictly increasing clock remains exportable."""
    db_path = tmp_path / "monotonic-clock.sqlite3"
    store = await _synthetic_store(db_path, run_id="monotonic-clock")
    await store.close()

    out_dir = tmp_path / "fixture"
    entry = s2f.convert(db_path, out_dir, "monotonic-clock")

    assert entry["run_id"] == "monotonic-clock"
    assert (out_dir / "roast.jsonl").is_file()
    assert (out_dir / "summary.json").is_file()


@pytest.mark.asyncio
async def test_null_elapsed_clock_uses_tick_fallback(tmp_path: Path) -> None:
    """T5: legacy all-NULL elapsed values export via the tick fallback."""
    db_path = tmp_path / "null-elapsed-clock.sqlite3"
    run_id = "null-elapsed-clock"
    store = await _synthetic_store(db_path, run_id=run_id)
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET elapsed_seconds = NULL WHERE run_id = ?",
        (run_id,),
    )
    await store.connection.commit()
    await store.close()

    out_dir = tmp_path / "fixture"
    s2f.convert(db_path, out_dir, run_id)
    rows = [json.loads(line) for line in (out_dir / "roast.jsonl").read_text().splitlines()]
    telemetry_seconds = [row["monotonic_seconds"] for row in rows if row["type"] == "telemetry"]

    assert telemetry_seconds[:3] == [0.0, 1.0, 2.0]


@pytest.mark.asyncio
async def test_single_telemetry_row_reaches_missing_marks_refusal(tmp_path: Path) -> None:
    """T6: a one-row clock passes the guard and fails honestly on marks."""
    db_path = tmp_path / "single-row.sqlite3"
    run_id = "single-row"
    store = await _sparse_store(db_path, run_id=run_id, include_telemetry=True)
    await store.close()

    with pytest.raises(s2f.FixtureConversionError) as exc_info:
        s2f.convert(db_path, tmp_path / "fixture", run_id)

    message = str(exc_info.value)
    assert "lacks required marks" in message
    assert "telemetry clock" not in message


@pytest.mark.asyncio
async def test_no_telemetry_uses_existing_refusal(tmp_path: Path) -> None:
    """T7: no telemetry retains its existing, distinct diagnosis."""
    db_path = tmp_path / "no-telemetry.sqlite3"
    run_id = "no-telemetry"
    store = await _sparse_store(db_path, run_id=run_id, include_telemetry=False)
    await store.close()

    with pytest.raises(s2f.FixtureConversionError) as exc_info:
        s2f.convert(db_path, tmp_path / "fixture", run_id)

    message = str(exc_info.value)
    assert message == f"run {run_id} has no telemetry snapshots"
    assert "telemetry clock" not in message


@pytest.mark.asyncio
async def test_non_finite_elapsed_clock_is_refused_in_its_own_terms(
    tmp_path: Path,
) -> None:
    """T8: positive infinity is invalid without claiming a backwards step."""
    db_path = tmp_path / "non-finite-elapsed.sqlite3"
    run_id = "non-finite-elapsed"
    store = await _synthetic_store(db_path, run_id=run_id, outcome="faulted")
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET elapsed_seconds = ? WHERE run_id = ? AND tick = 100",
        (float("inf"), run_id),
    )
    await store.connection.commit()
    await store.close()

    with pytest.raises(s2f.FixtureConversionError) as exc_info:
        s2f.convert(db_path, tmp_path / "fixture", run_id)

    message = str(exc_info.value)
    assert "elapsed_seconds has non-finite value inf" in message
    assert "elapsed_seconds 495.0 ->" not in message


@pytest.mark.asyncio
async def test_first_elapsed_clock_violation_remains_deterministic(tmp_path: Path) -> None:
    """A later non-finite value does not replace the first elapsed violation."""
    db_path = tmp_path / "multiple-elapsed-violations.sqlite3"
    run_id = "multiple-elapsed-violations"
    store = await _synthetic_store(db_path, run_id=run_id, outcome="faulted")
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET elapsed_seconds = 0.0 WHERE run_id = ? AND tick = 100",
        (run_id,),
    )
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET elapsed_seconds = ? WHERE run_id = ? AND tick = 110",
        (float("inf"), run_id),
    )
    await store.connection.commit()
    await store.close()

    with pytest.raises(s2f.FixtureConversionError) as exc_info:
        s2f.read_store_roast(db_path, run_id)

    message = str(exc_info.value)
    assert "elapsed_seconds 495.0 -> 0.0" in message
    assert "non-finite" not in message


@pytest.mark.asyncio
async def test_cli_announces_clock_refusal_before_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T9: the CLI announces a both-axis refusal and leaves no directory."""
    db_path = tmp_path / "cli-clock-reset.sqlite3"
    run_id = "cli-clock-reset"
    store = await _synthetic_store(db_path, run_id=run_id, outcome="faulted")
    await store.connection.execute(
        "UPDATE telemetry_snapshots"
        " SET tick = tick - 100, elapsed_seconds = elapsed_seconds - 500.0"
        " WHERE run_id = ? AND tick >= 100",
        (run_id,),
    )
    await store.connection.commit()
    await store.close()

    out_dir = tmp_path / "fixture"
    result = s2f.main([str(db_path), "--run-id", run_id, "--out-dir", str(out_dir)])
    captured = capsys.readouterr()

    assert result == 1
    assert captured.err.startswith("error:")
    assert "tick" in captured.err
    assert "elapsed_seconds" in captured.err
    assert not out_dir.exists()


@pytest.mark.asyncio
async def test_equal_telemetry_clock_values_are_not_reversals(tmp_path: Path) -> None:
    """Equal consecutive values on either clock axis remain exportable."""
    db_path = tmp_path / "equal-clock-values.sqlite3"
    run_id = "equal-clock-values"
    store = await _synthetic_store(db_path, run_id=run_id)
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET tick = 99, elapsed_seconds = 495.0"
        " WHERE run_id = ? AND tick = 100",
        (run_id,),
    )
    await store.connection.commit()
    await store.close()

    entry = s2f.convert(db_path, tmp_path / "fixture", run_id)

    assert entry["run_id"] == run_id


@pytest.mark.asyncio
async def test_both_absent_utc_sources_fall_back_independently(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both absent preferred sources retain their own event-row fallback warning."""
    db_path = tmp_path / "both-fallbacks.sqlite3"
    store = await _backdated_store(
        db_path,
        include_t0_anchor=False,
        include_first_crack_anchor=False,
    )
    await store.close()

    entry = s2f.convert(db_path, tmp_path / "fixture")
    warning = capsys.readouterr().err

    assert entry["charge_anchor"] == "event_row"
    assert entry["first_crack_anchor"] == "event_row"
    assert "charge uses event_row" in warning
    assert "DTR overstated" in warning
    assert "first crack uses event_row" in warning
    assert "DTR understated" in warning


@pytest.mark.asyncio
async def test_corrupt_backdated_onset_inverting_marks_fails_closed(tmp_path: Path) -> None:
    """T13: a mapped FC onset before charge trips the mark-order backstop."""
    db_path = tmp_path / "fc-before-charge.sqlite3"
    store = await _backdated_store(db_path)
    corrupt_state = json.dumps({"first_crack_status": {"detected_at_utc": _backdated_wall(30.0)}})
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET raw_state_json = ?"
        " WHERE run_id = ? AND raw_state_json IS NOT NULL",
        (corrupt_state, "backdated-run"),
    )
    await store.connection.commit()
    await store.close()

    with pytest.raises(s2f.FixtureConversionError) as exc_info:
        s2f.convert(db_path, tmp_path / "fixture")
    message = str(exc_info.value)
    assert "mark order invalid" in message
    assert "charge 49.0" in message
    assert "fc 30.0" in message
    assert "drop 720.0" in message


@pytest.mark.asyncio
async def test_frozen_dtr_mismatch_emits_cross_check_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T14: a fallback DTR mismatch over 0.5 pp is loud but still exports."""
    db_path = tmp_path / "dtr-warning.sqlite3"
    store = await _backdated_store(db_path, include_first_crack_anchor=False)
    await store.close()

    s2f.convert(db_path, tmp_path / "fixture")
    warning = capsys.readouterr().err

    assert "backdated-run" in warning
    assert "development_time_percent" in warning
    assert "17.9" in warning
    assert f"{_BACKDATED_FROZEN_DTR}" in warning


@pytest.mark.asyncio
async def test_preferred_anchors_without_frozen_dtr_warn_unverifiable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Preferred anchors are never silently treated as verified without frozen DTR."""
    db_path = tmp_path / "no-frozen-dtr.sqlite3"
    store = await _backdated_store(db_path)
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET development_percent = NULL WHERE run_id = ?",
        ("backdated-run",),
    )
    await store.connection.commit()
    await store.close()

    entry = s2f.convert(db_path, tmp_path / "fixture")
    warning = capsys.readouterr().err

    assert entry["charge_anchor"] == "run_row_utc"
    assert entry["first_crack_anchor"] == "fc_status_utc"
    assert "could not be cross-checked" in warning
    assert "no frozen development_percent exists" in warning
    assert "charge=run_row_utc" in warning
    assert "first_crack=fc_status_utc" in warning


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("frozen_development_percent", "should_warn"),
    [(22.1, False), (22.1001, True)],
)
async def test_frozen_dtr_cross_check_boundary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    frozen_development_percent: float,
    should_warn: bool,
) -> None:
    """The frozen-DTR warning threshold is strictly greater than 0.5 pp."""
    db_path = tmp_path / "dtr-boundary.sqlite3"
    store = await _backdated_store(db_path)
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET development_percent = ?"
        " WHERE run_id = ? AND development_percent IS NOT NULL",
        (frozen_development_percent, "backdated-run"),
    )
    await store.connection.commit()
    await store.close()

    s2f.convert(db_path, tmp_path / "fixture")
    warning = capsys.readouterr().err

    assert ("development_time_percent cross-check differs" in warning) is should_warn


@pytest.mark.asyncio
async def test_tick_reset_is_refused_before_frozen_dtr_cross_check(
    tmp_path: Path,
) -> None:
    """A resumed run is refused before its frozen DTR can be cross-checked."""
    db_path = tmp_path / "dtr-tick-reset.sqlite3"
    store = await _backdated_store(db_path)
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET development_percent = NULL WHERE run_id = ?",
        ("backdated-run",),
    )
    await store.connection.execute(
        "INSERT INTO telemetry_snapshots"
        " (run_id, tick, recorded_at_utc, elapsed_seconds, agent_phase,"
        " development_percent) VALUES (?, ?, ?, NULL, ?, ?)",
        ("backdated-run", 9999, _backdated_wall(1000.0), "development", 30.0),
    )
    await store.connection.execute(
        "INSERT INTO telemetry_snapshots"
        " (run_id, tick, recorded_at_utc, elapsed_seconds, agent_phase,"
        " development_percent) VALUES (?, ?, ?, NULL, ?, ?)",
        (
            "backdated-run",
            0,
            _backdated_wall(1001.0),
            "development",
            _BACKDATED_FROZEN_DTR,
        ),
    )
    await store.connection.commit()
    await store.close()

    with pytest.raises(s2f.FixtureConversionError, match="tick 9999 -> 0"):
        s2f.convert(db_path, tmp_path / "fixture")


# --- #788: AC1 — unverifiable cross-check when the exported DTR is null -----


@pytest.mark.asyncio
async def test_degenerate_zero_span_run_warns_unverifiable_and_still_exports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T1: charge == first crack == drop exports a null DTR with a NEW warning,
    distinct from the existing frozen-absent warning."""
    db_path = tmp_path / "degenerate-zero-span.sqlite3"
    store = RoastStore(db_path=db_path)
    await store.initialize()
    await store.create_run(
        run_id="r", profile=_PROFILE, config=AppConfig(), agent_phase=RoastPhase.STARTING
    )
    await store.record_telemetry(
        run_id="r",
        tick=0,
        agent_phase=RoastPhase.DEVELOPMENT,
        elapsed_seconds=0.0,
        interval_seconds=0.0,
        telemetry=RoastTelemetry(bean_temp_c=180.0, env_temp_c=200.0),
        heat_level_percent=100,
        fan_level_percent=30,
        development_percent=42.0,
    )
    # All three marks land on the SAME instant: span == 0, so the pre-existing
    # `<=` mark-order guard tolerates it (per contract, NOT tightened here) and
    # summary["development_time_percent"] comes out None (span <= 0).
    await _record_marks(store, "r", charge_s=720.0, first_crack_s=720.0, drop_s=720.0)
    await store.complete_run(run_id="r", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    await store.close()

    out_dir = tmp_path / "fixture"
    entry = s2f.convert(db_path, out_dir, "r")
    warning = capsys.readouterr().err
    summary = json.loads((out_dir / "summary.json").read_text())

    assert summary["development_time_percent"] is None
    assert entry["development_time_percent"] is None
    assert (out_dir / "roast.jsonl").is_file()
    assert (out_dir / "summary.json").is_file()
    assert "r" in warning
    assert "could not be cross-checked against frozen development_percent 42.0" in warning
    assert "the exported value is null because the roast span" in warning
    assert "no frozen development_percent exists" not in warning


@pytest.mark.asyncio
async def test_frozen_absent_and_null_export_warnings_never_both_fire(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T2: the row-1 (frozen absent) and row-3 (null export) messages are
    mutually exclusive and textually distinguishable."""
    db_path = tmp_path / "no-frozen-dtr-distinct.sqlite3"
    store = await _backdated_store(db_path)
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET development_percent = NULL WHERE run_id = ?",
        ("backdated-run",),
    )
    await store.connection.commit()
    await store.close()

    s2f.convert(db_path, tmp_path / "fixture")
    warning = capsys.readouterr().err

    assert "no frozen development_percent exists" in warning
    assert "the exported value is null because the roast span" not in warning


@pytest.mark.asyncio
async def test_happy_path_emits_no_unverifiable_cross_check_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T3: a normal roast (positive span, frozen present, within threshold)
    triggers neither unverifiable-DTR branch."""
    db_path = tmp_path / "happy-path.sqlite3"
    store = await _backdated_store(db_path)
    await store.close()

    s2f.convert(db_path, tmp_path / "fixture")
    warning = capsys.readouterr().err

    assert "could not be cross-checked" not in warning


# --- #788: AC2 — first-crack onset dedup keyed by parsed UTC instant --------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second_form",
    ["same_offset", "z_suffix", "shifted_plus_one_hour"],
)
async def test_first_crack_onset_dedup_by_parsed_instant_across_iso_forms(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], second_form: str
) -> None:
    """T4: Z / +00:00 / shifted-offset renderings of ONE instant count as one
    onset, never triggering the ambiguity warning."""
    db_path = tmp_path / f"onset-instant-dedup-{second_form}.sqlite3"
    store = await _backdated_store(db_path)
    first_onset = _backdated_wall(_BACKDATED_FIRST_CRACK_SECONDS)
    parsed = datetime.fromisoformat(first_onset)
    if second_form == "same_offset":
        second_onset = first_onset
    elif second_form == "z_suffix":
        second_onset = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    else:
        second_onset = parsed.astimezone(timezone(timedelta(hours=1))).isoformat()
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET raw_state_json = ?"
        " WHERE run_id = ? AND elapsed_seconds = 605.0",
        (
            json.dumps({"first_crack_status": {"detected_at_utc": second_onset}}),
            "backdated-run",
        ),
    )
    await store.connection.commit()
    await store.close()

    out_dir = tmp_path / "fixture"
    entry = s2f.convert(db_path, out_dir)
    warning = capsys.readouterr().err
    _, ground = bakeoff_replay.load_roast(out_dir / "roast.jsonl")

    assert entry["first_crack_anchor"] == "fc_status_utc"
    assert ground.first_crack_seconds == _BACKDATED_FIRST_CRACK_SECONDS
    assert "distinct onset values" not in warning


@pytest.mark.asyncio
async def test_distinct_onsets_across_iso_forms_choose_earliest_with_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T5: genuinely distinct instants across ISO forms still warn (2 distinct)
    and select the earliest by parsed instant, not by string/row order."""
    db_path = tmp_path / "onset-distinct-forms.sqlite3"
    store = await _backdated_store(db_path)
    earlier_instant_z = (
        datetime.fromisoformat(_backdated_wall(574.0))
        .astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET raw_state_json = ?"
        " WHERE run_id = ? AND elapsed_seconds >= 650.0",
        (
            json.dumps({"first_crack_status": {"detected_at_utc": earlier_instant_z}}),
            "backdated-run",
        ),
    )
    await store.connection.commit()
    await store.close()

    out_dir = tmp_path / "fixture"
    entry = s2f.convert(db_path, out_dir)
    warning = capsys.readouterr().err
    _, ground = bakeoff_replay.load_roast(out_dir / "roast.jsonl")

    assert entry["first_crack_anchor"] == "fc_status_utc"
    assert ground.first_crack_seconds == 574.0
    assert "2 distinct onset values" in warning
    assert f"chose earliest {earlier_instant_z}" in warning


@pytest.mark.asyncio
async def test_mixed_duplicate_and_distinct_onset_forms_count_two_not_three(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T6: same instant in two forms plus one genuinely distinct instant counts
    2, not 3, and the genuinely distinct earlier instant wins."""
    db_path = tmp_path / "onset-mixed-forms.sqlite3"
    store = await _backdated_store(db_path)
    same_instant_z = (
        datetime.fromisoformat(_backdated_wall(_BACKDATED_FIRST_CRACK_SECONDS))
        .astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )
    distinct_earlier = _backdated_wall(560.0)
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET raw_state_json = ?"
        " WHERE run_id = ? AND elapsed_seconds = 650.0",
        (json.dumps({"first_crack_status": {"detected_at_utc": same_instant_z}}), "backdated-run"),
    )
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET raw_state_json = ?"
        " WHERE run_id = ? AND elapsed_seconds = 655.0",
        (
            json.dumps({"first_crack_status": {"detected_at_utc": distinct_earlier}}),
            "backdated-run",
        ),
    )
    await store.connection.commit()
    await store.close()

    out_dir = tmp_path / "fixture"
    entry = s2f.convert(db_path, out_dir)
    warning = capsys.readouterr().err
    _, ground = bakeoff_replay.load_roast(out_dir / "roast.jsonl")

    assert entry["first_crack_anchor"] == "fc_status_utc"
    assert ground.first_crack_seconds == 560.0
    assert "2 distinct onset values" in warning
    assert f"chose earliest {distinct_earlier}" in warning


@pytest.mark.asyncio
async def test_microsecond_distinct_onsets_are_not_merged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T7: sub-second-distinct instants are never tolerance-merged."""
    db_path = tmp_path / "onset-microsecond.sqlite3"
    store = await _backdated_store(db_path)
    first_micro = "2026-08-10T20:09:35.000100+00:00"
    second_micro = "2026-08-10T20:09:35.000200Z"
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET raw_state_json = ?"
        " WHERE run_id = ? AND raw_state_json IS NOT NULL",
        (json.dumps({"first_crack_status": {"detected_at_utc": first_micro}}), "backdated-run"),
    )
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET raw_state_json = ?"
        " WHERE run_id = ? AND elapsed_seconds = 605.0",
        (json.dumps({"first_crack_status": {"detected_at_utc": second_micro}}), "backdated-run"),
    )
    await store.connection.commit()
    await store.close()

    out_dir = tmp_path / "fixture"
    s2f.convert(db_path, out_dir)
    warning = capsys.readouterr().err

    assert "2 distinct onset values" in warning


@pytest.mark.asyncio
async def test_all_unparseable_onsets_still_count_distinct_and_fall_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T8: two distinct unparseable strings still count separately and the
    first encountered one is chosen for the (unparseable) fallback path."""
    db_path = tmp_path / "onset-all-unparseable.sqlite3"
    store = await _backdated_store(db_path)
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET raw_state_json = NULL"
        " WHERE run_id = ? AND elapsed_seconds NOT IN (600.0, 605.0)",
        ("backdated-run",),
    )
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET raw_state_json = ?"
        " WHERE run_id = ? AND elapsed_seconds = 600.0",
        (json.dumps({"first_crack_status": {"detected_at_utc": "bad-one"}}), "backdated-run"),
    )
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET raw_state_json = ?"
        " WHERE run_id = ? AND elapsed_seconds = 605.0",
        (json.dumps({"first_crack_status": {"detected_at_utc": "bad-two"}}), "backdated-run"),
    )
    await store.connection.commit()
    await store.close()

    out_dir = tmp_path / "fixture"
    entry = s2f.convert(db_path, out_dir)
    warning = capsys.readouterr().err

    assert entry["first_crack_anchor"] == "event_row"
    assert "source unparseable" in warning
    assert "2 distinct onset values" in warning
    assert "chose earliest bad-one" in warning


@pytest.mark.asyncio
async def test_utc_normalization_overflow_onset_falls_back_as_unparseable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A boundary-aware onset that cannot reach UTC uses the safe fallback."""
    db_path = tmp_path / "onset-normalization-overflow.sqlite3"
    store = await _backdated_store(db_path)
    overflow_onset = "0001-01-01T00:00:00+01:00"
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET raw_state_json = NULL"
        " WHERE run_id = ? AND elapsed_seconds NOT IN (600.0, 605.0)",
        ("backdated-run",),
    )
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET raw_state_json = ?"
        " WHERE run_id = ? AND elapsed_seconds = 600.0",
        (
            json.dumps({"first_crack_status": {"detected_at_utc": overflow_onset}}),
            "backdated-run",
        ),
    )
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET raw_state_json = ?"
        " WHERE run_id = ? AND elapsed_seconds = 605.0",
        (json.dumps({"first_crack_status": {"detected_at_utc": "bad-two"}}), "backdated-run"),
    )
    await store.connection.commit()
    await store.close()

    entry = s2f.convert(db_path, tmp_path / "fixture")
    warning = capsys.readouterr().err

    assert entry["first_crack_anchor"] == "event_row"
    assert "source unparseable" in warning
    assert "2 distinct onset values" in warning
    assert f"chose earliest {overflow_onset}" in warning


@pytest.mark.asyncio
async def test_mixed_parseable_and_unparseable_onset_prefers_parseable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T9: one unparseable plus one parseable value still selects the
    parseable onset and still warns with 2 distinct onset values."""
    db_path = tmp_path / "onset-mixed-parseable.sqlite3"
    store = await _backdated_store(db_path)
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET raw_state_json = ?"
        " WHERE run_id = ? AND elapsed_seconds = 600.0",
        (json.dumps({"first_crack_status": {"detected_at_utc": "not-a-date"}}), "backdated-run"),
    )
    await store.connection.commit()
    await store.close()

    out_dir = tmp_path / "fixture"
    entry = s2f.convert(db_path, out_dir)
    warning = capsys.readouterr().err
    _, ground = bakeoff_replay.load_roast(out_dir / "roast.jsonl")

    assert entry["first_crack_anchor"] == "fc_status_utc"
    assert ground.first_crack_seconds == _BACKDATED_FIRST_CRACK_SECONDS
    assert "2 distinct onset values" in warning


# --- #788: AC3 — first-crack fallback reason distinguishes provenance -------


@pytest.mark.asyncio
async def test_operator_sourced_fallback_reports_fixed_provenance_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T11: an operator-sourced acceptance reports the fixed provenance
    reason, never the generic 'source absent' text."""
    db_path = tmp_path / "operator-provenance.sqlite3"
    store = await _backdated_store(db_path)
    await store.connection.execute(
        "UPDATE roast_events SET payload_json = ? WHERE run_id = ? AND kind = ?",
        (
            json.dumps({"source": "operator"}),
            "backdated-run",
            RoastEventKind.FIRST_CRACK.value,
        ),
    )
    await store.connection.commit()
    await store.close()

    entry = s2f.convert(db_path, tmp_path / "fixture")
    warning = capsys.readouterr().err

    assert entry["first_crack_anchor"] == "event_row"
    assert "acceptance is operator-sourced, so MCP onset is not authoritative" in warning
    assert "source absent" not in warning


@pytest.mark.asyncio
async def test_named_non_mcp_source_reports_fixed_not_verified_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T12: a named non-MCP, non-operator source uses the fixed fail-closed
    reason and never reflects the source string into stderr."""
    db_path = tmp_path / "named-other-source.sqlite3"
    store = await _backdated_store(db_path)
    await store.connection.execute(
        "UPDATE roast_events SET payload_json = ? WHERE run_id = ? AND kind = ?",
        (
            json.dumps({"source": "controller"}),
            "backdated-run",
            RoastEventKind.FIRST_CRACK.value,
        ),
    )
    await store.connection.commit()
    await store.close()

    entry = s2f.convert(db_path, tmp_path / "fixture")
    warning = capsys.readouterr().err

    assert entry["first_crack_anchor"] == "event_row"
    assert "acceptance is not verified MCP-sourced, so MCP onset is not authoritative" in warning
    assert "controller" not in warning
    assert "source absent" not in warning


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload_json",
    [
        pytest.param(None, id="missing-payload"),
        pytest.param(json.dumps({}), id="missing-source"),
        pytest.param(json.dumps({"source": None}), id="null-source"),
        pytest.param(json.dumps({"source": "unknown"}), id="unknown-source"),
        pytest.param(json.dumps({"source": "MCP"}), id="wrong-case"),
        pytest.param(json.dumps({"source": 7}), id="non-string-source"),
        pytest.param(json.dumps([]), id="non-object-payload"),
        pytest.param("{malformed", id="malformed-payload"),
    ],
)
async def test_unreadable_or_unrecognised_provenance_reports_fixed_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], payload_json: str | None
) -> None:
    """T14: every arbitrary/malformed/missing/non-string/wrong-case provenance
    selects the SAME fixed fail-closed reason and never echoes its input."""
    db_path = tmp_path / "unreadable-provenance.sqlite3"
    store = await _backdated_store(db_path)
    await store.connection.execute(
        "UPDATE roast_events SET payload_json = ? WHERE run_id = ? AND kind = ?",
        (payload_json, "backdated-run", RoastEventKind.FIRST_CRACK.value),
    )
    await store.connection.commit()
    await store.close()

    entry = s2f.convert(db_path, tmp_path / "fixture")
    warning = capsys.readouterr().err

    assert entry["first_crack_anchor"] == "event_row"
    assert "acceptance is not verified MCP-sourced, so MCP onset is not authoritative" in warning
    assert "source absent" not in warning


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
    # The drop is the cooling transition (720 s, bean 194), NOT run_completed
    # (960 s, bean cooled to 170): drop_temp_c must read the true drop.
    assert ground.drop_temp_c == 194.0
    assert ground.first_crack_seconds == 600.0
    assert ground.drop_seconds == 720.0
    assert ground.t0_seconds == 60.0
    # The cooling tail is truncated: no telemetry row past the drop instant, and
    # nothing as cool as the cooled-down tail (which would corrupt drop_temp_c).
    assert all(float(r["monotonic_seconds"]) <= 720.0 for r in telemetry)
    assert min(float(r["bean_temp_c"]) for r in telemetry[-3:]) > 170.0

    # summary.json carries the #300 outcome label.
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["operator_rating"] == 4
    assert summary["operator_notes"] == "bright, clean"
    assert summary["degree"] == "core_medium"  # drop 194 ≤ 195
    assert summary["source"] == "agent-store"
    assert summary["drop_temp_c"] == 194.0
    # total_roast_seconds is charge→drop (660 s), NOT charge→run_completed.
    assert summary["total_roast_seconds"] == 660.0
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
async def test_summary_carries_roasted_weight_and_weight_loss_label(tmp_path: Path) -> None:
    """#388: the store roasted-out weight + derived weight-loss % become corpus
    labels — 250 g in, 221 g out → 11.6 %."""
    db_path = tmp_path / "weighed.sqlite3"
    store = await _synthetic_store(db_path, roasted_weight_grams=221.0)
    await store.close()
    out_dir = tmp_path / "fixture"
    s2f.convert(db_path, out_dir)
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["charge_weight_grams"] == 250.0  # _PROFILE.bean_weight_grams
    assert summary["roasted_weight_grams"] == 221.0
    assert summary["weight_loss_percent"] == 11.6  # (250 - 221) / 250 * 100


@pytest.mark.asyncio
async def test_summary_weight_loss_is_null_when_unweighed(tmp_path: Path) -> None:
    """#388: an un-weighed roast carries a null weight-loss label (charge weight is
    still surfaced from the frozen profile)."""
    db_path = tmp_path / "unweighed.sqlite3"
    store = await _synthetic_store(db_path)  # no roasted weight
    await store.close()
    out_dir = tmp_path / "fixture"
    s2f.convert(db_path, out_dir)
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["charge_weight_grams"] == 250.0
    assert summary["roasted_weight_grams"] is None


@pytest.mark.asyncio
async def test_summary_charge_weight_grams_is_the_effective_corrected_value(
    tmp_path: Path,
) -> None:
    """#520 round-2 P1: a corrected roast must never feed the corpus the
    WRONG physical truth — charge_weight_grams exports the EFFECTIVE
    (corrected) charge, and weight_loss_percent derives from it, not the
    stale frozen 250 g default. Roast 13's own worked example: 255 g
    corrected, 223 g out -> 12.55%, never the 10.8% the frozen default alone
    would compute."""
    db_path = tmp_path / "corrected.sqlite3"
    store = await _synthetic_store(db_path, roasted_weight_grams=223.0)
    await store.set_corrected_charge("synthetic-run", corrected_charge_grams=255.0)
    await store.close()
    out_dir = tmp_path / "fixture"
    s2f.convert(db_path, out_dir)
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["charge_weight_grams"] == 255.0  # EFFECTIVE, not the 250 g frozen default
    assert summary["corrected_charge_grams"] == 255.0
    assert summary["roasted_weight_grams"] == 223.0
    assert summary["weight_loss_percent"] == 12.55  # (255 - 223) / 255 * 100


@pytest.mark.asyncio
async def test_summary_corrected_charge_grams_is_null_when_never_corrected(
    tmp_path: Path,
) -> None:
    """#520 round-2 P1: an uncorrected roast exports corrected_charge_grams as
    null and charge_weight_grams as the frozen default, unaffected."""
    db_path = tmp_path / "uncorrected.sqlite3"
    store = await _synthetic_store(db_path, roasted_weight_grams=221.0)
    await store.close()
    out_dir = tmp_path / "fixture"
    s2f.convert(db_path, out_dir)
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["charge_weight_grams"] == 250.0
    assert summary["corrected_charge_grams"] is None
    assert summary["weight_loss_percent"] == 11.6  # (250 - 221) / 250 * 100, unaffected


@pytest.mark.asyncio
async def test_summary_carries_every_tasting_entry(tmp_path: Path) -> None:
    """#522: multi-entry tastings reach the corpus — the signal
    operator_rating/notes alone cannot carry (a revisit is an ADDITIONAL
    entry, never an overwrite)."""
    db_path = tmp_path / "tasted.sqlite3"
    store = await _synthetic_store(db_path)
    await store.add_tasting("synthetic-run", stars=2, notes="flat", defects=["flat"])
    await store.add_tasting(
        "synthetic-run",
        stars=4,
        notes="grassy note faded",
        brew_method="pour_over",
        grind_note="medium-fine",
        attributes=["sweetness"],
        defects=["grassy"],
    )
    await store.close()
    out_dir = tmp_path / "fixture"
    s2f.convert(db_path, out_dir)
    summary = json.loads((out_dir / "summary.json").read_text())
    tastings = summary["tastings"]
    assert len(tastings) == 2  # BOTH entries — a revisit never overwrites.
    assert tastings[0]["stars"] == 2
    assert tastings[0]["defects"] == ["flat"]
    assert tastings[1]["stars"] == 4
    assert tastings[1]["brew_method"] == "pour_over"
    assert tastings[1]["grind_note"] == "medium-fine"
    assert tastings[1]["attributes"] == ["sweetness"]
    assert tastings[1]["defects"] == ["grassy"]


@pytest.mark.asyncio
async def test_summary_tastings_carry_the_degassing_offset(tmp_path: Path) -> None:
    """#522 round 4: the fixture's own clock is roast-relative, so the raw
    absolute tasted_at_utc alone gives a downstream reader no way to compute
    the degassing offset the field exists to capture — each entry must carry
    a derived degassing_offset_hours."""
    db_path = tmp_path / "degassing.sqlite3"
    store = await _synthetic_store(db_path)
    completed = s2f.read_store_roast(db_path).completed_at_utc
    assert completed is not None
    tasted_same_evening = (datetime.fromisoformat(completed) + timedelta(hours=2)).isoformat()
    tasted_next_day = (datetime.fromisoformat(completed) + timedelta(hours=20)).isoformat()
    await store.add_tasting("synthetic-run", stars=2, tasted_at_utc=tasted_same_evening)
    await store.add_tasting("synthetic-run", stars=4, tasted_at_utc=tasted_next_day)
    await store.close()

    out_dir = tmp_path / "fixture"
    s2f.convert(db_path, out_dir)
    summary = json.loads((out_dir / "summary.json").read_text())
    tastings = summary["tastings"]
    assert tastings[0]["degassing_offset_hours"] == 2.0
    assert tastings[1]["degassing_offset_hours"] == 20.0


@pytest.mark.asyncio
async def test_summary_tastings_degassing_offset_is_zero_not_negative_at_completion(
    tmp_path: Path,
) -> None:
    """#522 round 5: a tasting stored exactly AT completion (the API's
    round-5 clamp for a same-minute-but-raw-earlier entry, per the
    datetime-local minute-precision guard) must compute a degassing offset
    of exactly 0.00 — never a small negative value, the exact garbage the
    validator chain exists to prevent."""
    db_path = tmp_path / "at_completion.sqlite3"
    store = await _synthetic_store(db_path)
    completed = s2f.read_store_roast(db_path).completed_at_utc
    assert completed is not None
    # Mirrors RoastService.add_tasting's round-5 clamp: the API stores
    # completed_at_utc verbatim in this case, never a raw sub-minute-earlier
    # value.
    await store.add_tasting("synthetic-run", stars=3, tasted_at_utc=completed)
    await store.close()

    out_dir = tmp_path / "fixture"
    s2f.convert(db_path, out_dir)
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["tastings"][0]["degassing_offset_hours"] == 0.0


@pytest.mark.asyncio
async def test_summary_tastings_degassing_offset_is_null_without_tasted_at(
    tmp_path: Path,
) -> None:
    """#522 round 4: an entry with no tasted_at_utc (the operator did not
    supply one) carries a null offset, not a fabricated one."""
    db_path = tmp_path / "no_tasted_at.sqlite3"
    store = await _synthetic_store(db_path)
    await store.add_tasting("synthetic-run", stars=3)  # no tasted_at_utc
    await store.close()

    out_dir = tmp_path / "fixture"
    s2f.convert(db_path, out_dir)
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["tastings"][0]["degassing_offset_hours"] is None


@pytest.mark.asyncio
async def test_summary_tastings_is_empty_for_an_untasted_roast(tmp_path: Path) -> None:
    """#522: an untasted roast carries an empty tastings list, not a null or
    a missing key — the bake-off / any downstream reader can always index it."""
    db_path = tmp_path / "untasted.sqlite3"
    store = await _synthetic_store(db_path)
    await store.close()
    out_dir = tmp_path / "fixture"
    s2f.convert(db_path, out_dir)
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["tastings"] == []


@pytest.mark.asyncio
async def test_schema_v10_compat_roast_tastings_table_absent(tmp_path: Path) -> None:
    """#522: schema v10 stores predate the roast_tastings table (added in v11).

    read_store_roast must not crash when the table is absent; it should fall
    back to an empty tastings list."""
    import sqlite3

    db_path = tmp_path / "v10store.sqlite3"
    store = await _synthetic_store(db_path)
    await store.close()
    # Simulate schema v10 by dropping the table added in v11.
    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP TABLE roast_tastings")
    conn.commit()
    conn.close()

    result = s2f.read_store_roast(db_path)
    assert result.tastings == []


@pytest.mark.asyncio
async def test_schema_v11_compat_corrected_charge_grams_absent(tmp_path: Path) -> None:
    """#520: schema v11 stores predate the corrected_charge_grams column
    (added in v12).

    read_store_roast must not crash when the column is absent; it should
    fall back to NULL AS corrected_charge_grams and derive charge_weight_grams
    from the frozen profile alone."""
    import sqlite3

    db_path = tmp_path / "v11store.sqlite3"
    store = await _synthetic_store(db_path)
    await store.close()
    # Simulate schema v11 by dropping the column added in v12.
    conn = sqlite3.connect(str(db_path))
    conn.execute("ALTER TABLE roast_runs DROP COLUMN corrected_charge_grams")
    conn.commit()
    conn.close()

    result = s2f.read_store_roast(db_path)
    assert result.corrected_charge_grams is None
    assert result.charge_weight_grams == 250.0  # falls back to the frozen profile


@pytest.mark.asyncio
async def test_schema_v6_compat_roasted_weight_grams_absent(tmp_path: Path) -> None:
    """#224: schema v6 stores lack the roasted_weight_grams column (added in v7/#388).

    read_store_roast must not crash when the column is absent; it should fall
    back to NULL AS roasted_weight_grams and return roasted_weight_grams=None.
    """
    import sqlite3

    db_path = tmp_path / "v6store.sqlite3"
    store = await _synthetic_store(db_path)
    await store.close()
    # Simulate schema v6 by dropping the column that was added in v7.
    # SQLite 3.35+ supports ALTER TABLE DROP COLUMN.
    conn = sqlite3.connect(str(db_path))
    conn.execute("ALTER TABLE roast_runs DROP COLUMN roasted_weight_grams")
    conn.commit()
    conn.close()

    result = s2f.read_store_roast(db_path)
    assert result.roasted_weight_grams is None


@pytest.mark.asyncio
async def test_events_are_rebased_off_the_absolute_monotonic_clock(tmp_path: Path) -> None:
    """Events (absolute monotonic) are rebased onto the run-relative telemetry clock.

    The store keeps events on absolute ``time.monotonic()`` (here
    ``_MONOTONIC_OFFSET`` + run-relative) but telemetry on a run-relative clock.
    Treating them as one origin (the bug that shipped on the first fix) matches the
    drop against telemetry that maxes near the run length, so it always resolves to
    the LAST/cooled row → ``drop_temp_c`` off the cooled tail. With correct rebasing
    the drop lands at the true drop (194 °C, ``core_medium``), and the emitted event
    times are run-relative (drop at 720 s ≪ ``_MONOTONIC_OFFSET``).
    """
    db_path = tmp_path / "rebase.sqlite3"
    store = await _synthetic_store(db_path, drop_bean_temp_c=194.0, cooled_bean_temp_c=150.0)
    await store.close()

    out_dir = tmp_path / "fixture"
    s2f.convert(db_path, out_dir)
    rows = [json.loads(line) for line in (out_dir / "roast.jsonl").read_text().splitlines()]
    events = {r["kind"]: r["monotonic_seconds"] for r in rows if r["type"] == "event"}
    # Run-relative, NOT absolute (would be ~500_000 if the offset leaked through).
    assert events["beans_added"] == 60.0
    assert events["first_crack_detected"] == 600.0
    assert events["beans_dropped"] == 720.0
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["drop_temp_c"] == 194.0  # the true drop, not the 150 °C tail
    assert summary["degree"] == "core_medium"


@pytest.mark.asyncio
async def test_inverted_mark_order_fails_closed(tmp_path: Path) -> None:
    """A mis-stamped run (fc after drop) is refused, not emitted with negative time.

    The fixture is the eval corpus, so a negative development/total time would
    poison any scorer; the converter fails closed rather than emit it.
    """
    db_path = tmp_path / "inverted.sqlite3"
    store = RoastStore(db_path=db_path)
    await store.initialize()
    await store.create_run(
        run_id="r", profile=_PROFILE, config=AppConfig(), agent_phase=RoastPhase.STARTING
    )
    await store.record_telemetry(
        run_id="r",
        tick=0,
        agent_phase=RoastPhase.DEVELOPMENT,
        elapsed_seconds=0.0,
        interval_seconds=0.0,
        telemetry=RoastTelemetry(bean_temp_c=180.0, env_temp_c=200.0),
        heat_level_percent=100,
        fan_level_percent=30,
    )
    # first crack (800) AFTER the drop (720): inverted.
    await _record_marks(store, "r", charge_s=60.0, first_crack_s=800.0, drop_s=720.0)
    await store.complete_run(run_id="r", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    await store.close()

    with pytest.raises(s2f.FixtureConversionError, match="mark order invalid"):
        s2f.convert(db_path, tmp_path / "fixture")


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
async def test_over_roast_label_is_not_corrupted_by_the_cooling_tail(tmp_path: Path) -> None:
    """The exact triage bug: an over-done drop whose cooled tail looks core_medium.

    Drop at 198 °C (> 197 → ``over``) but the cooling tail falls to 170 °C
    (≤ 195 → ``core_medium``). Reading the drop off ``run_completed`` / the cooled
    tail would mislabel it ``core_medium`` — the converter must read the cooling
    transition and label it ``over`` (the verified ``fa24e673`` failure mode).
    """
    db_path = tmp_path / "over.sqlite3"
    store = await _synthetic_store(
        db_path, drop_bean_temp_c=198.0, cooled_bean_temp_c=170.0, rating=2
    )
    await store.close()

    out_dir = tmp_path / "fixture"
    s2f.convert(db_path, out_dir)
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["drop_temp_c"] == 198.0
    assert summary["degree"] == "over"  # would be core_medium off the cooled tail


@pytest.mark.asyncio
async def test_roast2_shaped_run_cooled_but_never_finalised(tmp_path: Path) -> None:
    """A roast-2-shaped run (cooled, NULL completed_at_utc) must still convert.

    Roast 2 (``c3b84625``) dropped + cooled but the MCP child segfaulted before
    ``run_completed`` and was NEVER restart-finalised — its ``completed_at_utc`` is
    NULL (verified in the real store). The explicit ``--run-id`` path must resolve
    it regardless of completion (the marks-presence check is the real gate) and
    read the drop off the cooling transition, with no ``run_completed`` row at all.
    """
    db_path = tmp_path / "roast2.sqlite3"
    store = await _synthetic_store(
        db_path,
        run_id="c3b84625",
        drop_bean_temp_c=193.0,
        record_run_completed=False,  # segfaulted before run_completed
        outcome=None,  # NEVER finalised → completed_at_utc NULL
        rating=None,
    )
    await store.close()

    # The no-arg auto-pick would NOT find it (no completed run); an explicit id must.
    with pytest.raises(s2f.FixtureConversionError, match="no completed roast_runs"):
        s2f.convert(db_path, tmp_path / "auto")
    out_dir = tmp_path / "fixture"
    entry = s2f.convert(db_path, out_dir, run_id="c3b84625")
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["drop_temp_c"] == 193.0
    assert summary["degree"] == "core_medium"
    _, ground = bakeoff_replay.load_roast(out_dir / "roast.jsonl")
    assert ground.drop_seconds == 720.0
    assert entry["run_id"] == "c3b84625"


@pytest.mark.asyncio
async def test_default_run_skips_a_discarded_run(tmp_path: Path) -> None:
    """#582 corpus hygiene: a soft-discarded run must never re-enter the corpus
    through the no-arg auto-pick, even when it is the MOST RECENT completed
    run — a broken "most-recent" query with no ``excluded`` filter would
    otherwise silently pick it right back up, defeating the discard."""
    db_path = tmp_path / "discarded-most-recent.sqlite3"
    store = await _synthetic_store(db_path, run_id="older-included", rating=3)
    # A second, LATER-completed run — discarded, so it must be skipped even
    # though a plain "most recent completed" query would otherwise pick it.
    await store.create_run(
        run_id="newer-discarded",
        profile=_PROFILE,
        config=AppConfig(),
        agent_phase=RoastPhase.STARTING,
    )
    await store.record_telemetry(
        run_id="newer-discarded",
        tick=0,
        agent_phase=RoastPhase.DEVELOPMENT,
        elapsed_seconds=0.0,
        interval_seconds=0.0,
        telemetry=RoastTelemetry(bean_temp_c=60.0, env_temp_c=80.0),
        heat_level_percent=100,
        fan_level_percent=30,
    )
    await store.record_telemetry(
        run_id="newer-discarded",
        tick=1,
        agent_phase=RoastPhase.DEVELOPMENT,
        elapsed_seconds=120.0,
        interval_seconds=0.0,
        telemetry=RoastTelemetry(bean_temp_c=190.0, env_temp_c=210.0),
        heat_level_percent=40,
        fan_level_percent=30,
    )
    await _record_marks(store, "newer-discarded", charge_s=0.0, first_crack_s=90.0, drop_s=120.0)
    await store.complete_run(
        run_id="newer-discarded", outcome="completed", agent_phase=RoastPhase.COMPLETE
    )
    await store.set_run_excluded("newer-discarded", excluded=True)
    await store.close()

    entry = s2f.convert(db_path, tmp_path / "fixture")
    assert entry["run_id"] == "older-included"


@pytest.mark.asyncio
async def test_explicit_discarded_run_id_raises(tmp_path: Path) -> None:
    """#582: an explicit ``--run-id`` naming a discarded run is refused — it must
    not silently re-export a run the operator explicitly excluded right back
    into the learning corpus."""
    db_path = tmp_path / "explicit-discarded.sqlite3"
    store = await _synthetic_store(db_path, run_id="bad-data")
    await store.set_run_excluded("bad-data", excluded=True)
    await store.close()

    with pytest.raises(s2f.FixtureConversionError, match="discarded"):
        s2f.convert(db_path, tmp_path / "fixture", run_id="bad-data")


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
    await _record_marks(store, "newer", charge_s=0.0, first_crack_s=90.0, drop_s=120.0)
    await store.complete_run(run_id="newer", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    await store.close()

    entry = s2f.convert(db_path, tmp_path / "fixture")
    assert entry["run_id"] == "newer"


async def _run_with_telemetry_and_run_started(db_path: Path, run_id: str = "bare") -> RoastStore:
    """A completed run with run_started + telemetry but NO charge/FC/drop marks."""
    store = RoastStore(db_path=db_path)
    await store.initialize()
    await store.create_run(
        run_id=run_id, profile=_PROFILE, config=AppConfig(), agent_phase=RoastPhase.STARTING
    )
    await store.record_event(
        run_id=run_id,
        kind=RoastEventKind.RUN_STARTED,
        source=RoastEventSource.CONTROLLER,
        monotonic_seconds=_MONOTONIC_OFFSET,
    )
    await store.record_telemetry(
        run_id=run_id,
        tick=0,
        agent_phase=RoastPhase.PREHEATING,
        elapsed_seconds=0.0,
        interval_seconds=0.0,
        telemetry=RoastTelemetry(bean_temp_c=60.0, env_temp_c=80.0),
        heat_level_percent=100,
        fan_level_percent=30,
    )
    await store.complete_run(run_id=run_id, outcome="aborted", agent_phase=RoastPhase.COMPLETE)
    return store


@pytest.mark.asyncio
async def test_missing_marks_raises(tmp_path: Path) -> None:
    """A run with run_started but no FC/drop marks is not a scorable fixture."""
    db_path = tmp_path / "no-marks.sqlite3"
    store = await _run_with_telemetry_and_run_started(db_path)
    await store.close()

    with pytest.raises(s2f.FixtureConversionError, match="required marks"):
        s2f.convert(db_path, tmp_path / "fixture")


@pytest.mark.asyncio
async def test_missing_run_started_raises(tmp_path: Path) -> None:
    """No run_started event → the event/telemetry clocks cannot be reconciled."""
    db_path = tmp_path / "no-run-started.sqlite3"
    store = RoastStore(db_path=db_path)
    await store.initialize()
    await store.create_run(
        run_id="r", profile=_PROFILE, config=AppConfig(), agent_phase=RoastPhase.STARTING
    )
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
    # Marks present, but NO run_started → no rebasing origin.
    await store.record_event(
        run_id="r",
        kind=RoastEventKind.PHASE_CHANGED,
        source=RoastEventSource.CONTROLLER,
        monotonic_seconds=_MONOTONIC_OFFSET + 120.0,
        payload={"phase": RoastPhase.COOLING.value},
    )
    await store.complete_run(run_id="r", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    await store.close()

    with pytest.raises(s2f.FixtureConversionError, match="no run_started event"):
        s2f.convert(db_path, tmp_path / "fixture")


@pytest.mark.asyncio
async def test_explicit_unknown_run_id_raises(tmp_path: Path) -> None:
    """An explicit run id that does not exist raises, not silently picks another."""
    db_path = tmp_path / "one-run.sqlite3"
    store = await _synthetic_store(db_path, run_id="real")
    await store.close()
    with pytest.raises(s2f.FixtureConversionError, match="no roast_run with id"):
        s2f.convert(db_path, tmp_path / "fixture", run_id="ghost")


def test_missing_store_file_raises(tmp_path: Path) -> None:
    """A nonexistent store path fails loudly (never creates an empty database)."""
    with pytest.raises(FileNotFoundError):
        s2f.read_store_roast(tmp_path / "nope.sqlite3")


def test_connect_readonly_returns_name_keyed_row_factory(tmp_path: Path) -> None:
    """``_connect_readonly`` delegates to the shared ``store_snapshot`` helper
    (#726) but still returns a ``sqlite3.Row``-keyed connection — the contract
    every caller in this module relies on."""
    db_path = tmp_path / "store.sqlite3"
    connection = sqlite3.connect(str(db_path))
    connection.execute("CREATE TABLE roast_runs (id TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO roast_runs (id) VALUES ('seed')")
    connection.commit()
    connection.close()

    ro = s2f._connect_readonly(db_path)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    try:
        assert ro.row_factory is sqlite3.Row
        row = ro.execute("SELECT id FROM roast_runs").fetchone()
        assert row["id"] == "seed"
    finally:
        ro.close()


def test_connect_readonly_missing_store_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no store at"):
        s2f._connect_readonly(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            tmp_path / "nope.sqlite3"
        )


@pytest.mark.asyncio
async def test_store_path_with_hash_character_reads_correctly(tmp_path: Path) -> None:
    """A store filename containing ``#`` must open the intended file rather
    than a mis-parsed URI (the naive ``file:{path}?mode=ro`` form reads ``#``
    as the URI fragment delimiter, silently truncating the path)."""
    db_path = tmp_path / "operator#db.sqlite3"
    store = await _synthetic_store(db_path, run_id="hash-run")
    await store.close()

    roast = s2f.read_store_roast(db_path)
    assert roast.run_id == "hash-run"


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
    await _record_marks(store, "r", charge_s=0.0, first_crack_s=90.0, drop_s=120.0)
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
    await _record_marks(store, "r", charge_s=0.0, first_crack_s=90.0, drop_s=120.0)
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
    await _record_marks(store, run_id, charge_s=0.0, first_crack_s=90.0, drop_s=120.0)
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
