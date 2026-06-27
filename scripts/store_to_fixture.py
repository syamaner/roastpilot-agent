"""Convert a completed agent roast (SQLite store) into a bake-off replay fixture.

The roast-data pipeline (#300, D44) makes the #277 control-advisor eval
**repeatable on new data**: every roast the agent itself runs lands in the
SQLite store (``store.py``: ``roast_runs`` / ``telemetry_snapshots`` /
``roast_events``), and this adapter turns one such run into the same replay
fixture format the bake-off scores — ``<dir>/roast.jsonl`` (telemetry + the
three event rows) plus a labelled ``summary.json``. It is the store-side sibling
of ``alog_to_fixture.py`` (the Artisan ``.alog`` adapter); both emit the identical
contract so the bake-off (``bakeoff_replay.load_roast``) needs zero changes.

Outcome label (#300 / D42): the store carries the operator's per-roast rating
(``roast_runs.operator_rating`` 1–5 + ``operator_notes``), so the fixture's
``summary.json`` carries them through as the outcome label, alongside the shared
``degree`` classification (``roast_degree.classify_degree`` on the drop bean
temperature). That is what turns a recorded roast into a *labelled* corpus entry
for the D42 learning loop, not just a replayable trace.

Event-kind mapping (store → the fixture's three event kinds):

- ``t0_detected`` → ``beans_added`` (the charge/turning-point instant the agent
  origins its roast clock on);
- ``first_crack`` → ``first_crack_detected``;
- the transition INTO cooling (a ``phase_changed`` event with ``phase ==
  "cooling"``, where the controller sets ``_drop_monotonic``, #239) →
  ``beans_dropped`` — the true drop instant. NOT ``run_completed``: that fires at
  COOLING→COMPLETE *after* the cooling tail (bean already tens of °C below the
  drop), so it would corrupt ``drop_temp_c`` + the degree label, and a roast that
  cooled-but-never-completed (e.g. roast 2) records no ``run_completed`` at all.

**Two clocks — reconciled via ``run_started``.** Telemetry ``elapsed_seconds``
is **run-relative** (≈0 at the first tick), but ``roast_events.monotonic_seconds``
is the **absolute** ``time.monotonic()`` reading (hundreds of thousands of
seconds — process uptime). They do NOT share an origin. So every event time is
rebased onto the telemetry clock by subtracting the ``run_started`` event's
``monotonic_seconds`` (the controller's ``_run_started_monotonic``) before any
nearest-row match or truncation. (A run with no ``run_started`` event — and so no
resolvable offset — cannot be reconciled and is rejected.)

The output fixture ``monotonic_seconds`` is on that single reconciled run-relative
clock: telemetry from the snapshot's ``elapsed_seconds`` (``tick × tick_interval``
fallback for a row predating the column), events rebased as above. Telemetry is
emitted from charge through the drop only (parity with ``alog_to_fixture``); the
cooling tail is truncated so ``drop_temp_c`` reads the true drop, not a cooled-down
sample. Temperatures are Celsius throughout (the store keeps everything in °C).

**Privacy (AGENTS.md invariant).** Real roast stores are the operator's personal
data and are NEVER committed. This adapter reads a store **read-only** and writes
the fixture to a local working directory (``--out-dir``, gitignored). Registering
a real fixture into the bake-off test-set list is a LOCAL operator action — this
script never writes any source roast id or timestamp into a committed artefact.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from roast_degree import classify_degree  # noqa: E402

#: The controller tick interval (seconds) — the fallback when a telemetry row
#: predates the ``elapsed_seconds`` column. Mirrors
#: ``ControllerConfig.tick_interval_seconds`` (1.0 s, the Hottop thermocouple
#: response time); kept as a literal so this stdlib-only script imports no agent
#: package.
_TICK_INTERVAL_SECONDS = 1.0

#: Store ``roast_events.kind`` → the fixture event kind the scorer requires.
_RUN_STARTED_KIND = "run_started"
_CHARGE_KIND = "t0_detected"
_FIRST_CRACK_KIND = "first_crack"
#: The drop instant is the transition INTO cooling (``_drop_monotonic``, #239),
#: emitted as a ``phase_changed`` event whose payload ``phase`` is ``cooling``
#: (controller.py). It is NOT ``run_completed`` — that fires at COOLING→COMPLETE,
#: *after* the cooling tail, by which point the bean has fallen tens of °C below
#: the true drop temperature (so ``run_completed`` would corrupt ``drop_temp_c``
#: and the degree label, and roast 2 never recorded one at all).
_PHASE_CHANGED_KIND = "phase_changed"
_COOLING_PHASE = "cooling"

#: The roaster the agent runs (Hottop), matching the ``.alog`` fixtures' summary.
#: The frozen ``profile_json`` / ``config_json`` carry no driver id (it is an
#: MCP/serve runtime detail, not part of ``RoastProfile`` or ``ControllerConfig``),
#: so the summary records the known roaster rather than inventing a lookup.
_ROASTER_DRIVER = "hottop_kn8828b_2k_plus"


class FixtureConversionError(ValueError):
    """The store run cannot produce a scorable fixture (missing marks/telemetry)."""


@dataclass(frozen=True)
class StoreRoast:
    """One completed roast read out of the agent SQLite store.

    Attributes:
        run_id: The ``roast_runs.id`` (uuid4 hex).
        operator_rating: The operator's 1–5 self-rating, or ``None`` if unrated.
        operator_notes: The operator's free-text notes, or ``None``.
        charge_weight_grams: The green/charge weight from the frozen profile
            (``RoastProfile.bean_weight_grams``), or ``None`` if unreadable.
        roasted_weight_grams: The operator-entered roasted-out weight (#388), or
            ``None`` if not weighed. With ``charge_weight_grams`` it yields the
            ``weight_loss_percent`` corpus label.
        roaster_driver: The roaster driver id (the known Hottop default; see
            ``_ROASTER_DRIVER``).
        telemetry: Tick-ordered telemetry rows (``tick`` / ``elapsed_seconds`` /
            ``bean_temp_c`` / ``env_temp_c`` / ``heat_level_percent`` /
            ``fan_level_percent``).
        charge_seconds: ``t0_detected`` time, **rebased onto the run-relative
            telemetry clock** (absolute event monotonic − ``run_started``), or
            ``None``.
        first_crack_seconds: ``first_crack`` time, rebased, or ``None``.
        drop_seconds: The drop instant — the transition into cooling
            (``phase_changed`` with ``phase == "cooling"``) — rebased, or ``None``.
    """

    run_id: str
    operator_rating: int | None
    operator_notes: str | None
    charge_weight_grams: float | None
    roasted_weight_grams: float | None
    roaster_driver: str
    telemetry: list[dict[str, Any]]
    charge_seconds: float | None
    first_crack_seconds: float | None
    drop_seconds: float | None


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the store strictly read-only (never mutate the operator's roast data).

    Args:
        db_path: Path to the SQLite store.

    Returns:
        A read-only connection with a name-keyed row factory.

    Raises:
        FileNotFoundError: If the database file does not exist (the read-only
            ``file:`` URI would otherwise create an empty database).
    """
    if not db_path.exists():
        raise FileNotFoundError(f"no store at {db_path}")
    uri = f"file:{db_path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _resolve_run_id(connection: sqlite3.Connection, run_id: str | None) -> str:
    """Resolve the run to export: the given id, or the most-recent completed run.

    An **explicit** ``run_id`` resolves regardless of completion — the
    marks-presence check is the real gate, and a roast that dropped + cooled but
    never finalised (e.g. roast 2, whose MCP child segfaulted before COMPLETE so
    ``completed_at_utc`` is NULL) is still a scorable fixture. The completed-run
    filter applies only to the **no-arg auto-pick**, where "latest completed" is
    the sensible default and an in-progress run must not be grabbed mid-roast.

    Args:
        connection: An open store connection.
        run_id: An explicit ``roast_runs.id``, or ``None`` to pick the latest
            completed run.

    Returns:
        The resolved run id.

    Raises:
        FixtureConversionError: If the explicit id is unknown, or (auto-pick) the
            store has no completed run.
    """
    if run_id is not None:
        row = connection.execute(
            "SELECT id FROM roast_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise FixtureConversionError(f"no roast_run with id {run_id!r}")
        return str(row["id"])
    row = connection.execute(
        "SELECT id FROM roast_runs WHERE completed_at_utc IS NOT NULL"
        " ORDER BY completed_at_utc DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise FixtureConversionError("the store has no completed roast_runs")
    return str(row["id"])


def _event_time(connection: sqlite3.Connection, run_id: str, kind: str) -> float | None:
    """The earliest persisted controller-clock time for an event kind, or ``None``.

    Args:
        connection: An open store connection.
        run_id: The run id.
        kind: The ``roast_events.kind`` to look up.

    Returns:
        The event's ``monotonic_seconds`` (controller clock), or ``None`` when the
        run never recorded that event or recorded it without a timestamp.
    """
    row = connection.execute(
        "SELECT monotonic_seconds FROM roast_events"
        " WHERE run_id = ? AND kind = ? AND monotonic_seconds IS NOT NULL"
        " ORDER BY recorded_at_utc ASC, id ASC LIMIT 1",
        (run_id, kind),
    ).fetchone()
    # The SQL already filters monotonic_seconds IS NOT NULL, so a returned row
    # always carries a non-null value — only the no-row case needs guarding.
    if row is None:
        return None
    return float(row["monotonic_seconds"])


def _drop_time(connection: sqlite3.Connection, run_id: str) -> float | None:
    """The drop instant: the earliest transition INTO cooling, or ``None``.

    The drop is the ``phase_changed`` event whose payload ``phase`` is
    ``cooling`` (controller.py sets ``_drop_monotonic`` on that transition, #239),
    NOT ``run_completed`` (which fires after the cooling tail). Filtered with
    ``json_extract`` on the stored payload so a non-cooling ``phase_changed`` (the
    other transitions all emit the same kind) is never mistaken for the drop.

    Args:
        connection: An open store connection.
        run_id: The run id.

    Returns:
        The drop's ``monotonic_seconds`` (controller clock), or ``None`` when the
        run never transitioned into cooling (never dropped) or recorded it without
        a timestamp.
    """
    row = connection.execute(
        "SELECT monotonic_seconds FROM roast_events"
        " WHERE run_id = ? AND kind = ? AND monotonic_seconds IS NOT NULL"
        " AND json_extract(payload_json, '$.phase') = ?"
        " ORDER BY recorded_at_utc ASC, id ASC LIMIT 1",
        (run_id, _PHASE_CHANGED_KIND, _COOLING_PHASE),
    ).fetchone()
    # The SQL already filters monotonic_seconds IS NOT NULL (see _event_time).
    if row is None:
        return None
    return float(row["monotonic_seconds"])


def read_store_roast(db_path: Path, run_id: str | None = None) -> StoreRoast:
    """Read one completed roast out of the agent SQLite store (read-only).

    Args:
        db_path: Path to the SQLite store.
        run_id: An explicit run id, or ``None`` for the most-recent completed run.

    Returns:
        The roast's telemetry, marks, and operator label.

    Raises:
        FileNotFoundError: If the store file does not exist.
        FixtureConversionError: If no matching completed run exists.
    """
    connection = _connect_readonly(db_path)
    try:
        resolved = _resolve_run_id(connection, run_id)
        run_row = connection.execute(
            "SELECT operator_rating, operator_notes, roasted_weight_grams, profile_json"
            " FROM roast_runs WHERE id = ?",
            (resolved,),
        ).fetchone()
        telemetry_rows = connection.execute(
            "SELECT tick, elapsed_seconds, bean_temp_c, env_temp_c,"
            " heat_level_percent, fan_level_percent FROM telemetry_snapshots"
            " WHERE run_id = ? ORDER BY tick ASC, id ASC",
            (resolved,),
        ).fetchall()
        telemetry = [
            {
                "tick": int(row["tick"]),
                "elapsed_seconds": None
                if row["elapsed_seconds"] is None
                else float(row["elapsed_seconds"]),
                "bean_temp_c": row["bean_temp_c"],
                "env_temp_c": row["env_temp_c"],
                "heat_level_percent": row["heat_level_percent"],
                "fan_level_percent": row["fan_level_percent"],
            }
            for row in telemetry_rows
        ]
        # Reconcile the two clocks: roast_events.monotonic_seconds is ABSOLUTE
        # time.monotonic(), telemetry.elapsed_seconds is run-relative. Rebase every
        # event onto the telemetry clock by subtracting the run-start monotonic
        # (the run_started event). Without it the clocks cannot be reconciled.
        run_started = _event_time(connection, resolved, _RUN_STARTED_KIND)
        if run_started is None:
            raise FixtureConversionError(
                f"run {resolved} has no run_started event — event/telemetry clocks "
                f"cannot be reconciled"
            )
        charge_weight_grams: float | None = None
        if run_row is not None and run_row["profile_json"] is not None:
            try:
                profile = json.loads(str(run_row["profile_json"]))
                raw_charge = profile.get("bean_weight_grams")
                charge_weight_grams = None if raw_charge is None else float(raw_charge)
            except (ValueError, TypeError, AttributeError):
                charge_weight_grams = None
        return StoreRoast(
            run_id=resolved,
            operator_rating=None
            if run_row is None or run_row["operator_rating"] is None
            else int(run_row["operator_rating"]),
            operator_notes=None
            if run_row is None or run_row["operator_notes"] is None
            else str(run_row["operator_notes"]),
            charge_weight_grams=charge_weight_grams,
            roasted_weight_grams=None
            if run_row is None or run_row["roasted_weight_grams"] is None
            else float(run_row["roasted_weight_grams"]),
            roaster_driver=_ROASTER_DRIVER,
            telemetry=telemetry,
            charge_seconds=_rebase(_event_time(connection, resolved, _CHARGE_KIND), run_started),
            first_crack_seconds=_rebase(
                _event_time(connection, resolved, _FIRST_CRACK_KIND), run_started
            ),
            drop_seconds=_rebase(_drop_time(connection, resolved), run_started),
        )
    finally:
        connection.close()


def _rebase(event_seconds: float | None, run_started_seconds: float) -> float | None:
    """Rebase an absolute event time onto the run-relative telemetry clock.

    Args:
        event_seconds: An event's absolute ``time.monotonic()`` reading, or
            ``None`` when the event is absent.
        run_started_seconds: The ``run_started`` event's absolute monotonic time
            (the run-relative origin).

    Returns:
        ``event_seconds - run_started_seconds`` (run-relative), or ``None`` when
        the event was absent.
    """
    if event_seconds is None:
        return None
    return event_seconds - run_started_seconds


def _telemetry_seconds(row: dict[str, Any]) -> float:
    """The fixture ``monotonic_seconds`` for a telemetry row.

    Prefers the stored controller-clock ``elapsed_seconds`` (the same clock the
    events carry, so telemetry and events stay coherent); falls back to
    ``tick × tick_interval`` for a row that predates the column.

    Args:
        row: A telemetry row from :func:`read_store_roast`.

    Returns:
        The monotonic timestamp in seconds.
    """
    elapsed = row["elapsed_seconds"]
    if elapsed is not None:
        return float(elapsed)
    return float(row["tick"]) * _TICK_INTERVAL_SECONDS


def build_fixture_rows(roast: StoreRoast) -> list[dict[str, Any]]:
    """Build the ``roast.jsonl`` rows (telemetry + the three event rows).

    Args:
        roast: The roast read from the store.

    Returns:
        The ordered fixture rows.

    Raises:
        FixtureConversionError: If the roast lacks telemetry, or any of the
            charge / first-crack / drop marks the scorer requires.
    """
    if not roast.telemetry:
        raise FixtureConversionError(f"run {roast.run_id} has no telemetry snapshots")
    marks = {
        "beans_added": roast.charge_seconds,
        "first_crack_detected": roast.first_crack_seconds,
        "beans_dropped": roast.drop_seconds,
    }
    missing = sorted(kind for kind, when in marks.items() if when is None)
    if missing:
        raise FixtureConversionError(f"run {roast.run_id} lacks required marks: {missing}")

    charge_seconds = float(roast.charge_seconds)  # type: ignore[arg-type]  # guarded above
    first_crack_seconds = float(roast.first_crack_seconds)  # type: ignore[arg-type]
    drop_seconds = float(roast.drop_seconds)  # type: ignore[arg-type]
    # Fail closed on a mis-stamped run: a clock-reconciliation glitch or a bad
    # store could leave charge > first crack > drop, which would emit a NEGATIVE
    # development time / total time into the fixture and poison any scorer. The
    # corpus-integrity purpose means refusing > emitting garbage.
    if not charge_seconds <= first_crack_seconds <= drop_seconds:
        raise FixtureConversionError(
            f"run {roast.run_id} mark order invalid: charge {charge_seconds} "
            f"fc {first_crack_seconds} drop {drop_seconds}"
        )
    # The thermocouple-readable rows in recorded order (a null-temperature row
    # carries no signal for the scorer and would break ``load_roast``).
    readable = [
        row
        for row in roast.telemetry
        if row["bean_temp_c"] is not None and row["env_temp_c"] is not None
    ]
    if not readable:
        raise FixtureConversionError(
            f"run {roast.run_id} has no telemetry rows with temperature readings"
        )
    # Truncate at the drop — parity with ``alog_to_fixture``, which emits start→drop
    # inclusive (``range(drop_index + 1)`` where ``drop_index`` is the row NEAREST
    # the drop). The drop instant typically falls BETWEEN two samples, so a strict
    # time cutoff would drop the sample just after it — the one actually nearest the
    # drop — landing ``drop_temp_c`` one row early (off the true drop). Keep through
    # the nearest row instead; the cooling tail beyond it never enters the fixture.
    drop_index = min(
        range(len(readable)),
        key=lambda i: abs(_telemetry_seconds(readable[i]) - drop_seconds),
    )
    rows: list[dict[str, Any]] = [
        {
            "type": "telemetry",
            "monotonic_seconds": round(_telemetry_seconds(row), 3),
            "bean_temp_c": round(float(row["bean_temp_c"]), 1),
            "env_temp_c": round(float(row["env_temp_c"]), 1),
            "heat_level_percent": int(row["heat_level_percent"] or 0),
            "fan_level_percent": int(row["fan_level_percent"] or 0),
        }
        for row in readable[: drop_index + 1]
    ]
    for kind, when in (
        ("beans_added", roast.charge_seconds),
        ("first_crack_detected", roast.first_crack_seconds),
        ("beans_dropped", roast.drop_seconds),
    ):
        assert when is not None  # guarded by the missing-marks check above
        rows.append({"type": "event", "kind": kind, "monotonic_seconds": round(when, 3)})
    return rows


def _drop_temp_c(rows: list[dict[str, Any]], drop_seconds: float) -> float:
    """Bean temperature at the telemetry row nearest the drop instant."""
    telemetry = [r for r in rows if r["type"] == "telemetry"]
    nearest = min(telemetry, key=lambda r: abs(float(r["monotonic_seconds"]) - drop_seconds))
    return float(nearest["bean_temp_c"])


def _first_crack_temp_c(rows: list[dict[str, Any]], first_crack_seconds: float) -> float:
    """Bean temperature at the telemetry row nearest the first-crack instant."""
    telemetry = [r for r in rows if r["type"] == "telemetry"]
    nearest = min(telemetry, key=lambda r: abs(float(r["monotonic_seconds"]) - first_crack_seconds))
    return float(nearest["bean_temp_c"])


def build_summary(roast: StoreRoast, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the labelled ``summary.json`` for the fixture.

    Mirrors the ``alog_to_fixture`` summary keys (so the bake-off reads both
    sources identically) and adds the #300 outcome label: the operator rating /
    notes from the store and the shared drop-temperature ``degree``.

    Args:
        roast: The roast read from the store.
        rows: The fixture rows from :func:`build_fixture_rows`.

    Returns:
        The summary dict.
    """
    # All three marks are guaranteed non-None: build_fixture_rows raised otherwise.
    charge = float(roast.charge_seconds)  # type: ignore[arg-type]
    first_crack = float(roast.first_crack_seconds)  # type: ignore[arg-type]
    drop = float(roast.drop_seconds)  # type: ignore[arg-type]
    span = drop - charge
    dev = drop - first_crack
    drop_temp_c = round(_drop_temp_c(rows, drop), 1)
    return {
        "active": False,
        "phase": "complete",
        "source": "agent-store",
        "roaster_driver": roast.roaster_driver,
        "first_crack_temp_c": round(_first_crack_temp_c(rows, first_crack), 1),
        "drop_temp_c": drop_temp_c,
        "development_time_seconds": round(dev, 1),
        "development_time_percent": round(dev / span * 100, 1) if span > 0 else None,
        "total_roast_seconds": round(span, 1),
        # Outcome label (#300 / D42): the operator's per-roast rating + notes are
        # the corpus labels; degree is the shared drop-temperature rule.
        "operator_rating": roast.operator_rating,
        "operator_notes": roast.operator_notes,
        "degree": classify_degree(drop_temp_c),
        # Objective outcome label (#388): the roasted-out weight + derived
        # weight-loss % = (charge - roasted) / charge * 100 — predominantly
        # moisture, but also dry-matter loss (CO2, volatiles, chaff), so NOT pure
        # water loss. ``None`` when the roast was not weighed.
        "charge_weight_grams": roast.charge_weight_grams,
        "roasted_weight_grams": roast.roasted_weight_grams,
        "weight_loss_percent": _weight_loss_percent(
            roast.charge_weight_grams, roast.roasted_weight_grams
        ),
    }


def _weight_loss_percent(
    charge_weight_grams: float | None, roasted_weight_grams: float | None
) -> float | None:
    """Roast weight-loss % for the fixture label (#388).

    Mirrors ``models.weight_loss_percent`` (kept local so this standalone script
    has no app-package import): ``(charge - roasted) / charge * 100``, ``None``
    when either weight is absent / non-positive.

    Args:
        charge_weight_grams: The green/charge weight, or ``None``.
        roasted_weight_grams: The roasted-out weight, or ``None``.

    Returns:
        The weight-loss percentage rounded to two decimals, or ``None``.
    """
    if (
        charge_weight_grams is None
        or roasted_weight_grams is None
        or charge_weight_grams <= 0
        or roasted_weight_grams <= 0
    ):
        return None
    return round((charge_weight_grams - roasted_weight_grams) / charge_weight_grams * 100.0, 2)


def convert(db_path: Path, out_dir: Path, run_id: str | None = None) -> dict[str, Any]:
    """Convert one store roast into a fixture directory and return a manifest entry.

    Args:
        db_path: Path to the SQLite store (read-only).
        out_dir: The fixture directory to write ``roast.jsonl`` + ``summary.json``
            into (created if absent; gitignored working dir).
        run_id: An explicit run id, or ``None`` for the most-recent completed run.

    Returns:
        A small manifest entry (run id, drop temp, degree, rating, row count) — no
        raw telemetry, suitable for a local run log.

    Raises:
        FileNotFoundError: If the store file does not exist.
        FixtureConversionError: If the run is unusable (no telemetry / marks).
    """
    roast = read_store_roast(db_path, run_id)
    rows = build_fixture_rows(roast)
    summary = build_summary(roast, rows)
    out_dir.mkdir(parents=True, exist_ok=True)
    fixture = out_dir / "roast.jsonl"
    fixture.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return {
        "run_id": roast.run_id,
        "fixture": str(fixture),
        "drop_temp_c": summary["drop_temp_c"],
        "degree": summary["degree"],
        "operator_rating": summary["operator_rating"],
        "telemetry_rows": sum(1 for r in rows if r["type"] == "telemetry"),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI: convert a completed store roast into a labelled replay fixture.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv``).

    Returns:
        Process exit code (``0`` on success, ``1`` on a conversion error).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", type=Path, help="path to the agent SQLite store (read-only)")
    parser.add_argument(
        "--run-id",
        default=None,
        help="explicit roast_runs.id to export (default: the most-recent completed run)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="fixture directory to write roast.jsonl + summary.json (gitignored working dir)",
    )
    args = parser.parse_args(argv)
    try:
        entry = convert(args.db_path, args.out_dir, args.run_id)
    except (FileNotFoundError, FixtureConversionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"converted run {entry['run_id']} -> {entry['fixture']} "
        f"(drop {entry['drop_temp_c']} °C, degree {entry['degree']}, "
        f"rating {entry['operator_rating']}, rows {entry['telemetry_rows']})"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint guard
    raise SystemExit(main())
