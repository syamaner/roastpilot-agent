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

**Three clocks — reconciled deliberately.** Telemetry ``elapsed_seconds`` is
**run-relative**, while ``roast_events.monotonic_seconds`` is the absolute
``time.monotonic()`` reading. Event-row fallbacks are rebased by subtracting the
``run_started`` event's monotonic timestamp. The controller's backdated T0/first
crack truths are wall-clock UTC instants instead: each is mapped onto the
run-relative clock through the nearest telemetry row's paired
``recorded_at_utc`` / ``elapsed_seconds`` values. MCP monotonic timestamps are a
foreign clock domain and are never mixed with the agent clocks.

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
import math
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

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
        charge_weight_grams: The EFFECTIVE green/charge weight (#520): the
            operator-corrected value (``roast_runs.corrected_charge_grams``)
            when present, else the frozen profile's
            ``RoastProfile.bean_weight_grams``. ``None`` if the frozen weight
            is unreadable. A corrected roast must never feed the corpus its
            wrong physical truth — the same class of fix #522 applied to
            tastings, on this sibling value. See :attr:`corrected_charge_grams`
            for the raw correction alone.
        corrected_charge_grams: The raw operator charge-weight correction
            (#520), or ``None`` when never corrected. Exposed separately so a
            downstream reader can distinguish "ran with the frozen default"
            from "ran with a correction" — :attr:`charge_weight_grams` is
            already the effective value either way.
        roasted_weight_grams: The operator-entered roasted-out weight (#388), or
            ``None`` if not weighed. With ``charge_weight_grams`` it yields the
            ``weight_loss_percent`` corpus label.
        roaster_driver: The roaster driver id (the known Hottop default; see
            ``_ROASTER_DRIVER``).
        telemetry: Tick-ordered telemetry rows (``tick`` / ``elapsed_seconds`` /
            ``bean_temp_c`` / ``env_temp_c`` / ``heat_level_percent`` /
            ``fan_level_percent``).
        charge_seconds: T0 time on the run-relative telemetry clock, preferring
            ``roast_runs.t0_detected_at_utc`` and falling back to the rebased
            event row, or ``None``.
        charge_anchor: ``"run_row_utc"`` or ``"event_row"`` according to the
            source used for ``charge_seconds``; ``None`` iff the mark is absent.
        first_crack_seconds: First-crack time on the run-relative clock,
            preferring ``raw_state_json.first_crack_status.detected_at_utc`` and
            falling back to the rebased event row, or ``None``.
        first_crack_anchor: ``"fc_status_utc"`` or ``"event_row"`` according
            to the source used for ``first_crack_seconds``; ``None`` iff the
            mark is absent.
        drop_seconds: The drop instant — the transition into cooling
            (``phase_changed`` with ``phase == "cooling"``) — rebased, or ``None``.
        tastings: Every persisted tasting entry (#522, D91), oldest first, as
            plain dicts mirroring ``models.RoastTasting``'s JSON shape
            (``stars`` / ``notes`` / ``tasted_at_utc`` / ``brew_method`` /
            ``grind_note`` / ``attributes`` / ``defects``) — the multi-entry
            corpus signal the operator_rating/notes pair alone cannot carry
            (a revisit tasting is a SEPARATE entry, not an overwrite). Empty
            list for an untasted roast or a pre-#522 store whose schema
            predates the ``roast_tastings`` table.
        completed_at_utc: The run's ``roast_runs.completed_at_utc`` (UTC
            ISO-8601), or ``None`` for the unfinalised roast-2 shape (a run
            with no ``completed_at_utc`` cannot reach this converter's
            completed-only default lookup, but an explicit ``--run-id`` can
            still target one). Used only to derive each tasting's
            ``degassing_offset_hours`` (#522 round 4) — never a control input.
    """

    run_id: str
    operator_rating: int | None
    operator_notes: str | None
    charge_weight_grams: float | None
    corrected_charge_grams: float | None
    roasted_weight_grams: float | None
    roaster_driver: str
    telemetry: list[dict[str, Any]]
    charge_seconds: float | None
    charge_anchor: str | None
    first_crack_seconds: float | None
    first_crack_anchor: str | None
    drop_seconds: float | None
    tastings: list[dict[str, Any]]
    completed_at_utc: str | None


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

    #582 corpus hygiene: a soft-DISCARDED run (``roast_runs.excluded = 1``) must
    never re-enter the learning corpus through this exporter — that would
    silently defeat the whole point of discarding it. The no-arg auto-pick
    filters ``excluded = 0`` (an excluded run is simply never a candidate,
    mirroring the store's own ``list_runs``/reference-retrieval filter); an
    **explicit** ``--run-id`` that names a discarded run is refused outright
    with a clear error rather than silently exported — there is no override
    flag (the operator can ``POST .../restore`` first if they genuinely want
    it back in the corpus, which is the same reversible action #582 already
    exposes).

    Guarded with the same ``PRAGMA table_info`` back-compat check the
    ``roasted_weight_grams``/``corrected_charge_grams`` reads use just below
    (a real pre-#582 store predates the ``excluded`` column entirely and has
    no discarded runs to worry about).

    Args:
        connection: An open store connection.
        run_id: An explicit ``roast_runs.id``, or ``None`` to pick the latest
            completed run.

    Returns:
        The resolved run id.

    Raises:
        FixtureConversionError: If the explicit id is unknown, the explicit id
            names a discarded run, or (auto-pick) the store has no
            non-discarded completed run.
    """
    has_excluded_column = any(
        row[1] == "excluded"
        for row in connection.execute("PRAGMA table_info(roast_runs)").fetchall()
    )
    if run_id is not None:
        row = connection.execute(
            "SELECT id, excluded FROM roast_runs WHERE id = ?"
            if has_excluded_column
            else "SELECT id, 0 AS excluded FROM roast_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise FixtureConversionError(f"no roast_run with id {run_id!r}")
        if row["excluded"]:
            raise FixtureConversionError(
                f"roast_run {run_id!r} is discarded (#582) — restore it "
                f"(POST /api/roasts/{run_id}/restore) before exporting it into the corpus"
            )
        return str(row["id"])
    row = connection.execute(
        "SELECT id FROM roast_runs WHERE completed_at_utc IS NOT NULL"
        + (" AND excluded = 0" if has_excluded_column else "")
        + " ORDER BY completed_at_utc DESC, rowid DESC LIMIT 1"
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


def _first_crack_event_source(connection: sqlite3.Connection, run_id: str) -> str | None:
    """Read provenance from the accepted first-crack event payload.

    The selected row uses the same ordering and timestamp requirement as
    :func:`_event_time`, so provenance gates the exact event that supplies the
    fallback mark. Missing, malformed, non-object, null, and non-string payload
    values all fail safe as unknown provenance.

    Args:
        connection: An open store connection.
        run_id: The run id.

    Returns:
        The payload's string ``source``, or ``None`` when it is not usable.
    """
    row = connection.execute(
        "SELECT payload_json FROM roast_events"
        " WHERE run_id = ? AND kind = ? AND monotonic_seconds IS NOT NULL"
        " ORDER BY recorded_at_utc ASC, id ASC LIMIT 1",
        (run_id, _FIRST_CRACK_KIND),
    ).fetchone()
    if row is None or row["payload_json"] is None:
        return None
    try:
        payload: Any = json.loads(row["payload_json"])
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    source = cast(dict[str, object], payload).get("source")
    return source if isinstance(source, str) else None


def _parse_utc(value: str) -> datetime | None:
    """Parse an ISO-8601 instant, treating a missing offset as UTC.

    Args:
        value: ISO-8601 timestamp; Python 3.11 accepts both ``Z`` and explicit
            offsets.

    Returns:
        An offset-aware datetime, or ``None`` when ``value`` is unparseable.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _utc_to_run_seconds(
    connection: sqlite3.Connection, run_id: str, target_iso: str
) -> float | None:
    """Map a wall-clock UTC instant onto the run-relative telemetry clock.

    The nearest usable telemetry row supplies the paired wall/elapsed anchor.
    Iteration follows tick/id order and updates only for a strictly closer row,
    so the earliest row deterministically wins an equal-distance tie.

    Args:
        connection: An open store connection.
        run_id: The run id.
        target_iso: ISO-8601 wall-clock instant to map.

    Returns:
        Run-relative seconds, or ``None`` when the target or every telemetry
        wall timestamp is unparseable, or no row has both required clocks.
    """
    target = _parse_utc(target_iso)
    if target is None:
        return None
    rows = connection.execute(
        "SELECT recorded_at_utc, elapsed_seconds FROM telemetry_snapshots"
        " WHERE run_id = ? AND elapsed_seconds IS NOT NULL"
        " AND recorded_at_utc IS NOT NULL ORDER BY tick ASC, id ASC",
        (run_id,),
    ).fetchall()
    nearest_elapsed: float | None = None
    nearest_delta_seconds: float | None = None
    nearest_distance: float | None = None
    for row in rows:
        recorded = _parse_utc(str(row["recorded_at_utc"]))
        if recorded is None:
            continue
        delta_seconds = (target - recorded).total_seconds()
        distance = abs(delta_seconds)
        if nearest_distance is None or distance < nearest_distance:
            nearest_distance = distance
            nearest_delta_seconds = delta_seconds
            nearest_elapsed = float(row["elapsed_seconds"])
    if nearest_elapsed is None or nearest_delta_seconds is None:
        return None
    mapped = nearest_elapsed + nearest_delta_seconds
    return None if mapped < 0.0 else mapped


def _telemetry_clock_violations(connection: sqlite3.Connection, run_id: str) -> list[str]:
    """Describe the first persisted telemetry-clock violation on each axis.

    Insertion order is the only trustworthy ordering because both ``tick`` and
    ``elapsed_seconds`` can reset after an agent restart. Null elapsed values are
    legitimate legacy rows and are skipped on that axis; non-finite values are
    invalid clocks in their own right.

    Args:
        connection: An open store connection.
        run_id: The run id.

    Returns:
        At most one detailed violation for ``tick`` and one for
        ``elapsed_seconds``, in that deterministic axis order.
    """
    rows = connection.execute(
        "SELECT id, tick, elapsed_seconds FROM telemetry_snapshots"
        " WHERE run_id = ? ORDER BY id ASC",
        (run_id,),
    ).fetchall()
    tick_violation: str | None = None
    elapsed_violation: str | None = None
    previous_tick: int | None = None
    previous_elapsed: float | None = None
    for row in rows:
        row_id = int(row["id"])
        current_tick = int(row["tick"])
        if tick_violation is None and previous_tick is not None and current_tick < previous_tick:
            tick_violation = f"tick {previous_tick} -> {current_tick} at telemetry row id {row_id}"
        previous_tick = current_tick

        if row["elapsed_seconds"] is None:
            continue
        current_elapsed = float(row["elapsed_seconds"])
        if not math.isfinite(current_elapsed):
            if elapsed_violation is None:
                elapsed_violation = (
                    "elapsed_seconds has non-finite value "
                    f"{current_elapsed!r} at telemetry row id {row_id}"
                )
            continue
        if (
            elapsed_violation is None
            and previous_elapsed is not None
            and current_elapsed < previous_elapsed
        ):
            elapsed_violation = (
                f"elapsed_seconds {previous_elapsed} -> {current_elapsed} "
                f"at telemetry row id {row_id}"
            )
        previous_elapsed = current_elapsed
    return [violation for violation in (tick_violation, elapsed_violation) if violation is not None]


def _first_crack_onset_utc(connection: sqlite3.Connection, run_id: str) -> tuple[str | None, int]:
    """Read the earliest distinct MCP first-crack onset from raw telemetry state.

    Malformed JSON and missing/null/non-string paths are skipped per row so one
    bad snapshot cannot hide a later valid onset. Distinctness is keyed by the
    **parsed UTC instant** (#788), not the raw string: ``Z`` and an equivalent
    ``+00:00``/shifted-offset rendering of the same instant collapse to one
    onset, with no tolerance — two instants a microsecond apart still count as
    two. A value that fails to parse keeps its raw string as its own identity,
    so genuinely unparseable sources are still counted distinctly. When a
    resumed MCP session legitimately re-stamps the onset at a distinct instant,
    the earliest parseable UTC value wins.

    Args:
        connection: An open store connection.
        run_id: The run id.

    Returns:
        A pair of the chosen onset (or ``None`` when absent) and the number of
        distinct onsets seen (distinct parsed instants plus distinct
        unparseable raw strings). If every value is unparseable, the first
        encountered value is returned so normal unparseable-source fallback and
        warning behavior still applies.
    """
    rows = connection.execute(
        "SELECT raw_state_json FROM telemetry_snapshots"
        " WHERE run_id = ? AND raw_state_json IS NOT NULL"
        " ORDER BY tick ASC, id ASC",
        (run_id,),
    ).fetchall()
    # Keyed by the parsed instant (not the raw string) so equal-instant
    # renderings in different ISO-8601 forms collapse to one onset; the first
    # raw string seen for a given instant is retained verbatim for the
    # downstream warning/mark (#788).
    parsed_by_instant: dict[datetime, str] = {}
    unparseable_seen: set[str] = set()
    unparseable_order: list[str] = []
    for row in rows:
        try:
            document: Any = json.loads(row["raw_state_json"])
        except (ValueError, TypeError):
            continue
        if not isinstance(document, dict):
            continue
        document_map = cast(dict[str, object], document)
        status = document_map.get("first_crack_status")
        if not isinstance(status, dict):
            continue
        status_map = cast(dict[str, object], status)
        detected_at = status_map.get("detected_at_utc")
        if not isinstance(detected_at, str) or not detected_at:
            continue
        parsed = _parse_utc(detected_at)
        if parsed is not None:
            instant = parsed.astimezone(UTC)
            if instant not in parsed_by_instant:
                parsed_by_instant[instant] = detected_at
        elif detected_at not in unparseable_seen:
            unparseable_seen.add(detected_at)
            unparseable_order.append(detected_at)
    count = len(parsed_by_instant) + len(unparseable_order)
    if count == 0:
        return None, 0
    if parsed_by_instant:
        earliest_instant = min(parsed_by_instant)
        return parsed_by_instant[earliest_instant], count
    return unparseable_order[0], count


@dataclass(frozen=True)
class _AnchorResolution:
    """One resolved mark plus private fallback-warning provenance."""

    seconds: float | None
    anchor: str | None
    fallback_reason: str | None


def _resolve_utc_mark(
    connection: sqlite3.Connection,
    run_id: str,
    preferred_iso: str | None,
    preferred_anchor: str,
    event_kind: str,
    run_started: float,
) -> _AnchorResolution:
    """Resolve a preferred UTC mark, falling back to its established event row."""
    fallback_reason = "source absent"
    if preferred_iso is not None:
        if _parse_utc(preferred_iso) is None:
            fallback_reason = "source unparseable"
        else:
            mapped = _utc_to_run_seconds(connection, run_id, preferred_iso)
            if mapped is not None:
                return _AnchorResolution(mapped, preferred_anchor, None)
            fallback_reason = "source unmappable"
    event_seconds = _rebase(_event_time(connection, run_id, event_kind), run_started)
    return _AnchorResolution(
        event_seconds,
        None if event_seconds is None else "event_row",
        fallback_reason,
    )


@dataclass(frozen=True)
class _StoreReadResult:
    """Private reader result carrying warning/cross-check data into ``convert``."""

    roast: StoreRoast
    charge_fallback_reason: str | None
    first_crack_fallback_reason: str | None
    first_crack_onset_warning: str | None
    frozen_development_percent: float | None


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


def _read_store_roast(db_path: Path, run_id: str | None = None) -> _StoreReadResult:
    """Read one roast plus private conversion provenance from the store.

    Args:
        db_path: Path to the SQLite store.
        run_id: An explicit run id, or ``None`` for the most-recent completed run.

    Returns:
        The roast, fallback reasons, and frozen achieved DTR.

    Raises:
        FileNotFoundError: If the store file does not exist.
        FixtureConversionError: If no matching completed run exists or its
            telemetry clock is invalid or non-monotonic.
    """
    connection = _connect_readonly(db_path)
    try:
        resolved = _resolve_run_id(connection, run_id)
        clock_violations = _telemetry_clock_violations(connection, resolved)
        if clock_violations:
            raise FixtureConversionError(
                f"run {resolved} telemetry clock cannot be exported: "
                f"{'; '.join(clock_violations)}; agent restarts reset the "
                "run-relative clock, so clock violations are refused rather "
                "than stitched"
            )
        # Guard: ``roasted_weight_grams`` was added in store schema v7 (#388),
        # ``corrected_charge_grams`` in v12 (#520). Real stores from roasts 3–6
        # are at v6 (both columns absent); treat either as NULL so the
        # converter does not crash on the real operator store.
        _store_cols = {
            row[1] for row in connection.execute("PRAGMA table_info(roast_runs)").fetchall()
        }
        _weight_col = (
            "roasted_weight_grams"
            if "roasted_weight_grams" in _store_cols
            else "NULL AS roasted_weight_grams"
        )
        _corrected_charge_col = (
            "corrected_charge_grams"
            if "corrected_charge_grams" in _store_cols
            else "NULL AS corrected_charge_grams"
        )
        _t0_col = (
            "t0_detected_at_utc"
            if "t0_detected_at_utc" in _store_cols
            else "NULL AS t0_detected_at_utc"
        )
        run_row = connection.execute(
            f"SELECT operator_rating, operator_notes, {_weight_col},"
            f" {_corrected_charge_col}, {_t0_col}, profile_json, completed_at_utc"
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
        # Guard: roast_tastings was added in store schema v11 (#522). A store
        # predating it (no table at all) has no tastings to read — same
        # back-compat shape as the v7 roasted_weight_grams column guard above.
        _tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        tastings: list[dict[str, Any]] = []
        if "roast_tastings" in _tables:
            tasting_rows = connection.execute(
                "SELECT tasted_at_utc, recorded_at_utc, stars, notes, brew_method,"
                " grind_note, attributes_json, defects_json FROM roast_tastings"
                " WHERE run_id = ? ORDER BY id ASC",
                (resolved,),
            ).fetchall()
            tastings = [
                {
                    "tasted_at_utc": row["tasted_at_utc"],
                    "recorded_at_utc": row["recorded_at_utc"],
                    "stars": int(row["stars"]),
                    "notes": row["notes"],
                    "brew_method": row["brew_method"],
                    "grind_note": row["grind_note"],
                    "attributes": []
                    if row["attributes_json"] is None
                    else json.loads(row["attributes_json"]),
                    "defects": []
                    if row["defects_json"] is None
                    else json.loads(row["defects_json"]),
                }
                for row in tasting_rows
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
        charge_source = (
            None
            if run_row is None or run_row["t0_detected_at_utc"] is None
            else str(run_row["t0_detected_at_utc"])
        )
        charge = _resolve_utc_mark(
            connection,
            resolved,
            charge_source,
            "run_row_utc",
            _CHARGE_KIND,
            run_started,
        )
        accepted_first_crack_source = _first_crack_event_source(connection, resolved)
        first_crack_source: str | None = None
        first_crack_onset_count = 0
        if accepted_first_crack_source == "mcp":
            first_crack_source, first_crack_onset_count = _first_crack_onset_utc(
                connection, resolved
            )
        first_crack_onset_warning = (
            f"first crack source has {first_crack_onset_count} distinct onset values; "
            f"chose earliest {first_crack_source}"
            if first_crack_onset_count > 1 and first_crack_source is not None
            else None
        )
        first_crack = _resolve_utc_mark(
            connection,
            resolved,
            first_crack_source,
            "fc_status_utc",
            _FIRST_CRACK_KIND,
            run_started,
        )
        # #788: the generic "source absent" reason from _resolve_utc_mark is
        # only true when the preferred source WAS consulted (accepted
        # provenance == "mcp") and genuinely produced nothing — that is the
        # only case where the source is truly absent. Every other acceptance
        # provenance means the source was never consulted at all, so it gets
        # its own fixed, non-reflecting reason: a closed two-way split
        # (operator override vs. everything else, including unreadable
        # provenance) rather than echoing the accepted source string into
        # operator-facing stderr.
        if accepted_first_crack_source == "mcp":
            first_crack_fallback_reason = first_crack.fallback_reason
        elif accepted_first_crack_source == "operator":
            first_crack_fallback_reason = (
                "acceptance is operator-sourced, so MCP onset is not authoritative"
            )
        else:
            first_crack_fallback_reason = (
                "acceptance is not verified MCP-sourced, so MCP onset is not authoritative"
            )
        frozen_row = connection.execute(
            "SELECT development_percent FROM telemetry_snapshots"
            " WHERE run_id = ? AND development_percent IS NOT NULL"
            " ORDER BY id DESC LIMIT 1",
            (resolved,),
        ).fetchone()
        frozen_development_percent = (
            None if frozen_row is None else float(frozen_row["development_percent"])
        )
        frozen_charge_weight_grams: float | None = None
        if run_row is not None and run_row["profile_json"] is not None:
            try:
                profile = json.loads(str(run_row["profile_json"]))
                raw_charge = profile.get("bean_weight_grams")
                frozen_charge_weight_grams = None if raw_charge is None else float(raw_charge)
            except (ValueError, TypeError, AttributeError):
                frozen_charge_weight_grams = None
        corrected_charge_grams: float | None = (
            None
            if run_row is None or run_row["corrected_charge_grams"] is None
            else float(run_row["corrected_charge_grams"])
        )
        # #520 round-2 P1: the corpus must never carry the run's WRONG physical
        # truth — a corrected roast exports the EFFECTIVE charge (the
        # correction when present), never the stale frozen default alone, the
        # same class of fix #522 applied to tastings on the sibling value.
        effective_charge_weight_grams = corrected_charge_grams or frozen_charge_weight_grams
        roast = StoreRoast(
            run_id=resolved,
            operator_rating=None
            if run_row is None or run_row["operator_rating"] is None
            else int(run_row["operator_rating"]),
            operator_notes=None
            if run_row is None or run_row["operator_notes"] is None
            else str(run_row["operator_notes"]),
            charge_weight_grams=effective_charge_weight_grams,
            corrected_charge_grams=corrected_charge_grams,
            roasted_weight_grams=None
            if run_row is None or run_row["roasted_weight_grams"] is None
            else float(run_row["roasted_weight_grams"]),
            roaster_driver=_ROASTER_DRIVER,
            telemetry=telemetry,
            charge_seconds=charge.seconds,
            charge_anchor=charge.anchor,
            first_crack_seconds=first_crack.seconds,
            first_crack_anchor=first_crack.anchor,
            drop_seconds=_rebase(_drop_time(connection, resolved), run_started),
            tastings=tastings,
            completed_at_utc=None
            if run_row is None or run_row["completed_at_utc"] is None
            else str(run_row["completed_at_utc"]),
        )
        return _StoreReadResult(
            roast=roast,
            charge_fallback_reason=charge.fallback_reason,
            first_crack_fallback_reason=first_crack_fallback_reason,
            first_crack_onset_warning=first_crack_onset_warning,
            frozen_development_percent=frozen_development_percent,
        )
    finally:
        connection.close()


def read_store_roast(db_path: Path, run_id: str | None = None) -> StoreRoast:
    """Read one completed roast out of the agent SQLite store (read-only).

    Args:
        db_path: Path to the SQLite store.
        run_id: An explicit run id, or ``None`` for the most-recent completed run.

    Returns:
        The roast's telemetry, resolved marks, anchor provenance, and operator
        label.

    Raises:
        FileNotFoundError: If the store file does not exist.
        FixtureConversionError: If no matching completed run exists or its
            telemetry clock is invalid or non-monotonic.
    """
    return _read_store_roast(db_path, run_id).roast


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
        #
        # ``charge_weight_grams`` is the EFFECTIVE charge (#520 round-2 P1): the
        # operator correction when present, else the frozen profile default —
        # the corpus must never carry a corrected roast's stale/wrong physical
        # truth. ``corrected_charge_grams`` is exposed separately so a
        # downstream reader can distinguish "ran with the frozen default" from
        # "ran with a correction"; ``None`` when never corrected (the .alog
        # adapter has no correction concept and always emits ``None``, parity
        # with this key set).
        "charge_weight_grams": roast.charge_weight_grams,
        "corrected_charge_grams": roast.corrected_charge_grams,
        "roasted_weight_grams": roast.roasted_weight_grams,
        "weight_loss_percent": _weight_loss_percent(
            roast.charge_weight_grams, roast.roasted_weight_grams
        ),
        # Multi-entry tasting corpus label (#522, D91) — the signal
        # operator_rating/notes alone cannot carry (a revisit tasting is an
        # ADDITIONAL entry, never an overwrite). Empty list for an untasted
        # roast or a pre-#522 store. The .alog adapter has no tasting concept
        # and always emits an empty list (parity with this key set). Each
        # entry carries a derived degassing_offset_hours (#522 round 4): the
        # fixture's own clock is roast-relative, so the raw absolute
        # tasted_at_utc alone gives a downstream reader no way to compute the
        # degassing offset the field exists to capture — the derived hours
        # figure is the corpus-usable shape, not the two absolute instants.
        "tastings": _tastings_with_offset(roast.tastings, roast.completed_at_utc),
    }


def _tastings_with_offset(
    tastings: list[dict[str, Any]], completed_at_utc: str | None
) -> list[dict[str, Any]]:
    """Annotate each tasting with its degassing offset from roast completion.

    Args:
        tastings: The raw persisted tasting entries (``StoreRoast.tastings``).
        completed_at_utc: The run's completion instant, or ``None`` when
            unknown (an unfinalised roast-2-shaped run) — every entry then
            carries a ``None`` offset rather than a fabricated one.

    Returns:
        The same entries, each with an added ``degassing_offset_hours`` key:
        hours between ``completed_at_utc`` and the entry's ``tasted_at_utc``,
        rounded to two decimals, or ``None`` when either instant is absent.
    """
    completed = None if completed_at_utc is None else datetime.fromisoformat(completed_at_utc)
    annotated: list[dict[str, Any]] = []
    for entry in tastings:
        offset: float | None = None
        tasted_at = entry.get("tasted_at_utc")
        if completed is not None and tasted_at is not None:
            offset = round(
                (datetime.fromisoformat(tasted_at) - completed).total_seconds() / 3600.0, 2
            )
        annotated.append({**entry, "degassing_offset_hours": offset})
    return annotated


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
    if roasted_weight_grams > charge_weight_grams:  # tare/scale error; physically impossible
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
        FixtureConversionError: If the run is unusable (including an invalid or
            non-monotonic telemetry clock, no telemetry, or missing marks).
    """
    read_result = _read_store_roast(db_path, run_id)
    roast = read_result.roast
    rows = build_fixture_rows(roast)
    summary = build_summary(roast, rows)
    if roast.charge_anchor == "event_row":
        print(
            f"warning: run {roast.run_id} charge uses event_row because preferred "
            f"run_row_utc {read_result.charge_fallback_reason}; late charge shortens "
            "the denominator, so DTR overstated",
            file=sys.stderr,
        )
    if roast.first_crack_anchor == "event_row":
        print(
            f"warning: run {roast.run_id} first crack uses event_row because preferred "
            f"fc_status_utc {read_result.first_crack_fallback_reason}; late first crack "
            "shortens development, so DTR understated",
            file=sys.stderr,
        )
    if read_result.first_crack_onset_warning is not None:
        print(
            f"warning: run {roast.run_id} {read_result.first_crack_onset_warning}",
            file=sys.stderr,
        )
    exported_development_percent = summary["development_time_percent"]
    frozen_development_percent = read_result.frozen_development_percent
    preferred_anchors = [
        label
        for anchor, label in (
            (roast.charge_anchor, "charge=run_row_utc"),
            (roast.first_crack_anchor, "first_crack=fc_status_utc"),
        )
        if anchor not in (None, "event_row")
    ]
    if frozen_development_percent is None and preferred_anchors:
        print(
            f"warning: run {roast.run_id} development_time_percent could not be "
            "cross-checked because no frozen development_percent exists; preferred "
            f"anchors: {', '.join(preferred_anchors)}",
            file=sys.stderr,
        )
    elif frozen_development_percent is not None and exported_development_percent is None:
        # #788: frozen truth exists but span (drop - charge) is non-positive, so
        # the exported DTR is null and the cross-check cannot run. Warn rather
        # than refuse (the mark-order guard already tolerates charge == fc ==
        # drop) — a null DTR must never enter the corpus silently.
        print(
            f"warning: run {roast.run_id} development_time_percent could not be "
            f"cross-checked against frozen development_percent {frozen_development_percent}: "
            "the exported value is null because the roast span (drop - charge) is not positive",
            file=sys.stderr,
        )
    elif (
        frozen_development_percent is not None
        and exported_development_percent is not None
        and abs(float(exported_development_percent) - frozen_development_percent) > 0.5
    ):
        print(
            f"warning: run {roast.run_id} development_time_percent cross-check differs: "
            f"exported {exported_development_percent}, frozen "
            f"{frozen_development_percent}",
            file=sys.stderr,
        )
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
        "charge_anchor": roast.charge_anchor,
        "first_crack_anchor": roast.first_crack_anchor,
        "development_time_percent": summary["development_time_percent"],
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
        f"rating {entry['operator_rating']}, rows {entry['telemetry_rows']}, "
        f"charge anchor {entry['charge_anchor']}, first-crack anchor "
        f"{entry['first_crack_anchor']})"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint guard
    raise SystemExit(main())
