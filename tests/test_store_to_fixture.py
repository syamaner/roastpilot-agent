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
from datetime import datetime, timedelta
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
    assert summary["weight_loss_percent"] is None


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
