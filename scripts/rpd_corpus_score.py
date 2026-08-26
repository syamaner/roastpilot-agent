"""RP-D joint-objective offline corpus scorer (#711, plan D124, PR-D2).

Scores every finished, non-excluded roast in the real operator SQLite store
against the RP-D joint-window metric (:func:`bakeoff_replay.joint_window_score`,
PR-D1) and prints a per-roast table plus a corpus aggregate. Pure offline: no
LLM, no network, no store WRITE of any kind.

**Authoritative inputs, read never recomputed:**

- ``target_drop_temp_c`` / ``target_development_percent`` come from the run's
  frozen ``roast_runs.profile_json`` (the profile instantiated at roast start,
  immutable thereafter).
- The achieved drop bean temperature and achieved DTR are read off
  ``telemetry_snapshots``, **primarily anchored on the executed ``drop_beans``
  command event's own ``recorded_at_utc``** (from ``roast_events``): the
  telemetry row (whatever phase it happens to be tagged) whose
  ``recorded_at_utc`` is NEAREST that instant is the true drop-instant
  reading. Confirmed against the ratified #559 Conebosque A/B on the real
  store: the drop event and the nearest telemetry row are within a
  millisecond of each other for both arms (188.0 °C / 20.997 % baseline,
  190.0 °C / 24.257 % treatment — matching the ratified 188/21 and 190/24 to
  the nearest integer). **Fallback (drop event missing/unparseable, or no row
  near it has both fields populated):** the LAST row tagged ``agent_phase =
  'development'`` — the same clock-safe anchor
  :meth:`~roastpilot_agent.store.RoastStore._build_reference_roast` uses
  (design note §6.4a): development begins at first crack, so this is never the
  run's final row, which can land deep in the post-drop COOLING tail with a
  falling, physically meaningless bean temperature (the trap: a bare
  ``MAX(tick WHERE development_percent IS NOT NULL)`` lands there because
  ``development_percent`` freezes at drop and is echoed on every later row).
  The fallback returns that row's OWN reading directly (Codex P2, round 3) —
  it deliberately does NOT try the row immediately following it, since
  without a correlated timestamp that successor could be an ordinary
  PERIODIC cooling sample several seconds (and several degrees) after the
  true drop on exactly the HISTORICAL, pre-force-persist stores this
  fallback exists for. **KNOWN LIMITATION** (Codex P1, round 3; documented,
  not redesigned — see :func:`_extract_drop_reading`'s docstring for the
  full analysis): the event-anchored PRIMARY read assumes the drop event's
  timestamp is stamped at the true drop instant, verified true for every
  advisor/policy/ceiling-guard drop in the real corpus (all 15 scored runs),
  but an OPERATOR-initiated drop's event may be flush-stamped up to one
  controller tick late — acceptable for this offline eval tool today; no
  operator-drop run has entered the corpus yet.
  Telemetry rows are read in durable INSERTION-id order, never ``(tick, id)``
  — a restart resets the process-local tick counter to zero, so ordering by
  tick can interleave a post-restart run's low ticks ahead of a pre-restart
  run's high ticks (mirrors
  :meth:`~roastpilot_agent.store.RoastStore.read_telemetry_points`'s own
  documented insertion-id rule). Every timestamp compare normalizes a naive
  (offset-less) ISO string to UTC before subtracting, so a legacy row's naive
  ``recorded_at_utc`` never raises against the drop event's offset-aware one.
  The event-anchored read is tried FIRST and unconditionally, even when the
  run has NO ``development``-phase row at all: a same-tick ceiling-guard drop
  (first crack detected at/above the ceiling guard temperature) transitions
  DEVELOPMENT → COOLING within the SAME tick it fires the drop, so only a
  single COOLING-tagged row is ever persisted — that guard-forced failure
  must still be counted in the corpus as an abnormal 0.00 MISS, not silently
  excluded.
- ``terminated_abnormally`` is ``True`` whenever ``outcome != 'completed'``
  (aborted/faulted), OR — within an otherwise ``'completed'`` run — a
  cleanly-detectable guard/emergency termination: an ``emergency_stop``
  :class:`~roastpilot_agent.safety.SafetyVerdict` anywhere in the run's
  ``safety_evaluations``, or a ceiling-guard drop (a persisted
  ``command_executed`` event whose payload's typed ``reason`` is
  ``DropReason.CEILING_GUARD.value``, D88 amendment A1). Both are queried
  directly off typed enum values bound as SQL parameters (D15: never a raw
  string literal) — no heuristic re-derivation from temperature/DTR.

**Read-only store isolation.** Never opens the operator's real
``~/roasts/roastpilot.sqlite3`` directly: :func:`store_snapshot.snapshot_store_to_temp`
(the shared helper every offline store-reading script uses, #726) copies it
to a private temp file first, against a strictly ``mode=ro`` source
connection, and every :class:`~roastpilot_agent.store.RoastStore` call in this
script operates on that copy.

**The #711 Goodhart guard:** every rendered score is printed alongside the
D42 operator rating — the scalar is never shown on its own, so a corpus
summary cannot be read as "the metric says this roast was good" independent
of what the operator actually thought of the cup.

Usage::

    python scripts/rpd_corpus_score.py
    python scripts/rpd_corpus_score.py --store ~/roasts/roastpilot.sqlite3 \\
        --json /tmp/rpd-corpus.json
    python scripts/rpd_corpus_score.py --run-ids 55f6a034218b4d8cb697996cce56b8eb \\
        3ca102f851de4381b450e9be7172dc98
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import math
import os
import sqlite3
import statistics
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))  # bakeoff_replay, store_snapshot
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bakeoff_replay import JointWindowScore, joint_score_to_json, joint_window_score  # noqa: E402
from store_snapshot import snapshot_store_to_temp  # noqa: E402

from roastpilot_agent.ambient_evidence import (  # noqa: E402
    AMBIENT_EVIDENCE_CLAIM,
    AmbientDoctrineEvidence,
    AmbientEvidenceVerdict,
    FractionBasis,
    derive_ambient_doctrine_evidence,
)
from roastpilot_agent.models import (  # noqa: E402
    DropReason,
    RoastCommand,
    RoastEventKind,
    RoastPhase,
)
from roastpilot_agent.safety import SafetyVerdict  # noqa: E402
from roastpilot_agent.store import RoastStore  # noqa: E402

#: Default operator store path (mirrors every other offline bake-off script).
DEFAULT_STORE = Path.home() / "roasts" / "roastpilot.sqlite3"


@dataclasses.dataclass(frozen=True)
class ScoredRun:
    """One roast's RP-D score plus the D42 Goodhart-guard identity fields.

    Attributes:
        run_id: The ``roast_runs.id`` (uuid4).
        bean_name: The frozen profile's ``name``.
        ambient_temp_c: The captured ambient temperature (°C), or ``None``
            when the probe was unavailable/disabled for this roast.
        rating: The D42 operator rating (1-5), or ``None`` when never rated.
            ALWAYS rendered alongside the scalar (the #711 Goodhart guard).
        score: The :class:`~bakeoff_replay.JointWindowScore`.
    """

    run_id: str
    bean_name: str
    ambient_temp_c: float | None
    rating: int | None
    score: JointWindowScore
    ambient_evidence: AmbientDoctrineEvidence


@dataclasses.dataclass(frozen=True)
class SkippedRun:
    """A run that could not be scored, with a logged reason.

    Attributes:
        run_id: The ``roast_runs.id`` that was skipped.
        reason: A short, human-readable reason (e.g. "no development-phase
            telemetry row").
    """

    run_id: str
    reason: str


@dataclasses.dataclass(frozen=True)
class CorpusReport:
    """The full corpus scoring result: every scored run plus every skip.

    Attributes:
        scored: Every successfully scored run, in discovery order.
        skipped: Every run that could not be scored, with its reason.
    """

    scored: list[ScoredRun]
    skipped: list[SkippedRun]


def _optional_float(value: object) -> float | None:
    """Read a nullable numeric SQLite column as ``float | None``."""
    return None if value is None else float(cast("float", value))


def _finite_or_none(value: float | None) -> float | None:
    """Normalize a non-finite float to ``None`` (JSON ``null`` / "unknown" display).

    A historical ``ambient_temp_c`` of SQLite ``+/-Infinity`` round-trips
    faithfully as IEEE-754 (unlike ``NaN``, which SQLite silently stores as
    ``NULL`` on write — verified round-trip), so it can reach this scorer as
    a genuine non-finite float. ``json.dumps`` would then emit a literal
    ``Infinity`` token — not valid JSON per RFC 8259 — which a strict
    ``JSON.parse`` rejects for the WHOLE report over one display-only field
    (Codex P2, round 3). Ambient is advisory/display-only (the score itself
    never reads it), so treating a non-finite reading as "unknown" costs
    nothing.

    Args:
        value: The raw optional float.

    Returns:
        ``value`` unchanged when it is ``None`` or finite; ``None`` when it
        is ``inf``/``-inf`` (or, defensively, ``nan``).
    """
    if value is None or not math.isfinite(value):
        return None
    return value


@dataclasses.dataclass(frozen=True)
class DropReading:
    """The achieved drop bean temperature and DTR read off telemetry.

    Attributes:
        bean_temp_c: The achieved drop bean temperature (°C).
        development_percent: The achieved DTR as a percentage.
    """

    bean_temp_c: float
    development_percent: float


async def _fetch_telemetry_rows(store: RoastStore, run_id: str) -> list[sqlite3.Row]:
    """Every telemetry row for ``run_id``, in durable INSERTION-id order.

    Ordered by ``id`` alone, never ``(tick, id)``: a restart resets the
    process-local tick counter to zero
    (:meth:`~roastpilot_agent.store.RoastStore.read_telemetry_points`'s own
    documented rule), so ordering by ``tick`` first can interleave a
    post-restart run's low ticks ahead of a pre-restart run's high ticks —
    the durable autoincrement ``id`` is the only column insertion order (and
    therefore chronology) is guaranteed to track.
    """
    async with store.connection.execute(
        "SELECT agent_phase, bean_temp_c, development_percent, recorded_at_utc"
        " FROM telemetry_snapshots WHERE run_id = ? ORDER BY id ASC",
        (run_id,),
    ) as cursor:
        return list(await cursor.fetchall())


def _development_row_indices(rows: list[sqlite3.Row]) -> list[int]:
    """Indices (into ``rows``) of every ``development``-phase telemetry row."""
    return [
        index
        for index, row in enumerate(rows)
        if RoastPhase(str(row["agent_phase"])) is RoastPhase.DEVELOPMENT
    ]


def _parse_recorded_at(value: str) -> datetime | None:
    """Parse an ISO-8601 ``recorded_at_utc`` string as a timezone-AWARE ``datetime``.

    Every current write path stamps an offset-aware instant (``_utc_now()``
    is ``datetime.now(UTC).isoformat()``), but ``datetime.fromisoformat``
    faithfully parses a NAIVE timestamp too (no UTC offset) — e.g. a legacy
    or hand-constructed row. Subtracting a naive ``datetime`` from an aware
    one raises ``TypeError``, which a bare ``except ValueError`` around the
    parse alone does NOT catch — one such row would otherwise raise
    uncaught out of :func:`_nearest_reading_to_timestamp` and abort the
    whole corpus (Codex P2, round 3). A naive result is assumed UTC (the
    only timezone this store ever writes) and stamped accordingly, so every
    returned value is always safely subtractable from another.

    Args:
        value: The ISO-8601 timestamp string to parse.

    Returns:
        A timezone-aware ``datetime``, or ``None`` if ``value`` does not
        parse as ISO-8601 at all.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _nearest_reading_to_timestamp(
    rows: list[sqlite3.Row], target_recorded_at_utc: str
) -> DropReading | None:
    """The telemetry row (any phase) whose ``recorded_at_utc`` is nearest ``target``.

    Considers every row with BOTH ``bean_temp_c`` and ``development_percent``
    populated and returns the one whose ``recorded_at_utc`` is closest in
    absolute time to ``target_recorded_at_utc`` — the drop-instant reading,
    independent of which phase label the controller happened to persist that
    tick under. Every timestamp is parsed via :func:`_parse_recorded_at`
    (normalized to timezone-aware), so a naive-vs-aware pair compares safely
    instead of raising.

    Args:
        rows: Every telemetry row for the run (see :func:`_fetch_telemetry_rows`).
        target_recorded_at_utc: The anchor instant (ISO-8601 UTC), typically
            the executed ``drop_beans`` command event's own ``recorded_at_utc``.

    Returns:
        The nearest :class:`DropReading`, or ``None`` when
        ``target_recorded_at_utc`` does not parse as ISO-8601, or no row has
        both fields populated with a parseable timestamp.
    """
    target = _parse_recorded_at(target_recorded_at_utc)
    if target is None:
        return None
    best_diff_seconds: float | None = None
    best_reading: DropReading | None = None
    for row in rows:
        temp = _optional_float(row["bean_temp_c"])
        dtr = _optional_float(row["development_percent"])
        if temp is None or dtr is None:
            continue
        row_time = _parse_recorded_at(str(row["recorded_at_utc"]))
        if row_time is None:
            continue
        diff_seconds = abs((row_time - target).total_seconds())
        if best_diff_seconds is None or diff_seconds < best_diff_seconds:
            best_diff_seconds = diff_seconds
            best_reading = DropReading(bean_temp_c=temp, development_percent=dtr)
    return best_reading


def _extract_drop_reading(
    rows: list[sqlite3.Row],
    development_indices: list[int],
    drop_event_recorded_at_utc: str | None,
) -> DropReading | None:
    """The achieved drop reading given pre-fetched rows and the drop event's timestamp.

    **Primary: event-anchored (Codex P1).** When
    ``drop_event_recorded_at_utc`` is available, :func:`_nearest_reading_to_timestamp`
    picks the telemetry row (whatever phase it is tagged) closest in time to
    the executed ``drop_beans`` command event — the true drop-instant
    reading, regardless of which control-loop tick or phase label persisted
    it. Verified against the real store's ratified #559 Conebosque A/B: the
    drop event and the nearest telemetry row land within a millisecond of
    each other for both arms. Tried FIRST and unconditionally (Codex P1,
    round 3): it needs only the drop event and nearby telemetry, so it alone
    can score a run with NO ``development``-phase row at all — a same-tick
    ceiling-guard drop (first crack detected at/above the ceiling guard
    temperature) has the controller transition DEVELOPMENT -> COOLING
    within the SAME tick it fires the drop, so only a single
    COOLING-tagged row is ever persisted. A prior version of this scorer
    gated on ``development_indices`` being non-empty BEFORE ever trying the
    event anchor, silently excluding every such guard-forced failure from
    the corpus instead of counting it as the abnormal 0.00 MISS it is.

    **KNOWN LIMITATION (Codex P1, round 3 — verified zero current corpus
    impact, documented rather than redesigned again):** this primary path
    assumes the drop event's ``recorded_at_utc`` is stamped at (or
    immediately after) the true drop instant. Verified true for every
    advisor/policy/ceiling-guard drop path
    (:meth:`RoastController._execute_drop` /
    ``_maybe_ceiling_guard_drop``) — confirmed against the real store: all
    15 currently-scored corpus runs are advisor-driven, and every
    event-anchored reading lands 0-2 °C AT OR ABOVE the last-development
    row's own temperature, never on a fallen (post-drop-cooling) reading. An
    OPERATOR-initiated drop's command event may be flush-stamped up to one
    controller tick late (unverified either way — no operator-drop run is in
    the corpus yet), which could pull the event-anchored read onto an
    already-cooling sample a few degrees short. Reconstructing an exact
    sub-sample drop instant from ~5 s telemetry is inherently approximate for
    an offline yardstick; acceptable here, revisit if/when an operator-drop
    run enters the corpus (a real trace, not a synthetic worry, would let
    this be measured rather than argued).

    **Fallback: last-development row's own reading.** Used only when the
    drop event's timestamp is missing/unparseable, or no row near it has
    both fields populated — e.g. a HISTORICAL run recorded before
    phase-boundary telemetry was force-persisted on every D96-state change.
    Returns the LAST ``development``-phase telemetry row's OWN reading
    directly (Codex P2, round 3) — the same clock-safe anchor
    :meth:`~roastpilot_agent.store.RoastStore._build_reference_roast` uses
    (design note §6.4a): never the run's chronologically final row (a
    post-drop COOLING-tail sample with a falling bean temperature), and
    never a bare ``MAX(tick)`` over rows with a non-null
    ``development_percent`` (a predicate cooling-tail rows also satisfy,
    since ``development_percent`` freezes at drop and is echoed on every
    later row). Deliberately does NOT try the row immediately following the
    last ``development`` row: without a correlated timestamp to bound it,
    that successor could be an ordinary PERIODIC cooling sample several
    seconds (and several degrees) after the true drop, on exactly the
    HISTORICAL pre-force-persist stores this fallback exists for — a
    "blind" successor guess is less trustworthy than the last confirmed
    development-phase measurement itself. Skipped entirely (returns
    ``None``) when ``development_indices`` is empty AND the event anchor
    already failed — there is nothing left to fall back to.

    Args:
        rows: Every telemetry row for the run, in insertion-id order (see
            :func:`_fetch_telemetry_rows`).
        development_indices: Indices of every ``development``-phase row (see
            :func:`_development_row_indices`); MAY be empty (a same-tick
            guard-drop run has none).
        drop_event_recorded_at_utc: The executed ``drop_beans`` command
            event's ``recorded_at_utc``, or ``None`` when unavailable.

    Returns:
        The :class:`DropReading`, or ``None`` when neither the event-anchored
        read nor the fallback heuristic can find a row with both fields.
    """
    if drop_event_recorded_at_utc is not None:
        nearest = _nearest_reading_to_timestamp(rows, drop_event_recorded_at_utc)
        if nearest is not None:
            return nearest

    if not development_indices:
        return None

    last_dev = rows[development_indices[-1]]
    last_dev_temp = _optional_float(last_dev["bean_temp_c"])
    last_dev_dtr = _optional_float(last_dev["development_percent"])
    if last_dev_temp is None or last_dev_dtr is None:
        return None
    return DropReading(bean_temp_c=last_dev_temp, development_percent=last_dev_dtr)


async def _terminated_abnormally(store: RoastStore, run_id: str, outcome: str | None) -> bool:
    """Whether ``run_id`` ended in a guard-drop, emergency stop, or fault.

    ``outcome != 'completed'`` (aborted/faulted) is always abnormal. Within an
    otherwise ``'completed'`` run, this additionally checks for the two
    cleanly-detectable in-run guard events: an ``emergency_stop``
    :class:`~roastpilot_agent.safety.SafetyVerdict` anywhere in
    ``safety_evaluations`` for the run, or a ceiling-guard drop — a
    ``command_executed`` :class:`~roastpilot_agent.models.RoastEventKind`
    event whose payload's ``reason`` key is
    :attr:`~roastpilot_agent.models.DropReason.CEILING_GUARD`'s value (D88
    amendment A1's decoupled bitter-line safety anchor,
    :meth:`~roastpilot_agent.controller.RoastController._maybe_ceiling_guard_drop`).
    Both typed enum values are bound as query parameters (D15: never a raw
    SQL string literal for a verdict/reason comparison).

    Args:
        store: The (snapshot-backed) store to read.
        run_id: The run to check.
        outcome: The run's ``roast_runs.outcome`` (``None`` for an unfinished
            run, though callers only ever pass a finished run's outcome here).

    Returns:
        ``True`` if the run's termination was abnormal per the rule above.
    """
    if outcome != "completed":
        return True
    async with store.connection.execute(
        "SELECT 1 FROM safety_evaluations WHERE run_id = ? AND verdict = ? LIMIT 1",
        (run_id, SafetyVerdict.EMERGENCY_STOP.value),
    ) as cursor:
        if await cursor.fetchone() is not None:
            return True
    async with store.connection.execute(
        "SELECT 1 FROM roast_events WHERE run_id = ? AND kind = ?"
        " AND json_extract(payload_json, '$.reason') = ? LIMIT 1",
        (run_id, RoastEventKind.COMMAND_EXECUTED.value, DropReason.CEILING_GUARD.value),
    ) as cursor:
        if await cursor.fetchone() is not None:
            return True
    return False


async def _drop_event_recorded_at(store: RoastStore, run_id: str) -> str | None:
    """The ``recorded_at_utc`` of ``run_id``'s executed ``drop_beans`` command event.

    A drop is a ``command_executed`` :class:`~roastpilot_agent.models.RoastEventKind`
    event whose payload ``command`` is
    :attr:`~roastpilot_agent.models.RoastCommand.DROP_BEANS`'s value — emitted
    for a policy, advisor, operator, OR ceiling-guard drop
    (:meth:`RoastController._execute_drop` / ``_maybe_ceiling_guard_drop``), so
    it is the single signal that the beans were actually dropped. A run that
    reaches DEVELOPMENT and is then cooled/ended WITHOUT a drop (e.g. an operator
    ``start_cooling`` recovery) has development telemetry but no drop event; its
    post-development rows are not a drop reading and must not be scored as one.
    One query serves both :func:`score_run`'s drop-gate (``None`` ⇒ no drop
    ever happened) and :func:`_extract_drop_reading`'s event-anchored read
    (a non-``None`` timestamp). The command value is bound as a query
    parameter (mirrors the typed-value discipline of the guard/verdict
    queries); ordered by ``id`` (insertion order) so the FIRST executed drop
    wins on the (structurally unreachable today) chance more than one is ever
    persisted.

    Args:
        store: The (snapshot-backed) store to read.
        run_id: The run to check.

    Returns:
        The event's ``recorded_at_utc``, or ``None`` if no executed
        ``drop_beans`` command event exists.
    """
    async with store.connection.execute(
        "SELECT recorded_at_utc FROM roast_events WHERE run_id = ? AND kind = ?"
        " AND json_extract(payload_json, '$.command') = ? ORDER BY id ASC LIMIT 1",
        (run_id, RoastEventKind.COMMAND_EXECUTED.value, RoastCommand.DROP_BEANS.value),
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else str(row["recorded_at_utc"])


async def score_run(store: RoastStore, run_id: str) -> ScoredRun | SkippedRun:
    """Score one run, or explain why it could not be scored.

    Args:
        store: The (snapshot-backed) store to read.
        run_id: The ``roast_runs.id`` to score.

    Returns:
        A :class:`ScoredRun` on success, or a :class:`SkippedRun` naming the
        reason (run not found, an unparseable/legacy frozen profile missing
        the required drop/DTR targets, no executed ``drop_beans`` command
        event, no usable drop reading from EITHER the event-anchored read or
        the ``development``-phase fallback, or a non-finite achieved/target
        value the metric itself rejects).
    """
    try:
        detail = await store.read_run(run_id)
    except ValueError as exc:
        return SkippedRun(run_id=run_id, reason=f"could not parse frozen profile: {exc}")
    if detail is None:
        return SkippedRun(run_id=run_id, reason="run not found")
    rows = await _fetch_telemetry_rows(store, run_id)
    # The drop-gate is checked BEFORE the development-row check (Codex P1,
    # round 3): a same-tick ceiling-guard drop has NO development-phase row
    # at all (see _extract_drop_reading), so gating on development_indices
    # first would silently exclude every such guard-forced failure from the
    # corpus. Presence of an executed drop event is the correct, independent
    # gate — a run that reached DEVELOPMENT and cooled/ended WITHOUT ever
    # dropping (e.g. an operator start_cooling recovery) is still excluded.
    drop_event_recorded_at_utc = await _drop_event_recorded_at(store, run_id)
    if drop_event_recorded_at_utc is None:
        return SkippedRun(
            run_id=run_id,
            reason="no drop_beans command event (run cooled/ended without a bean drop)",
        )
    development_indices = _development_row_indices(rows)
    reading = _extract_drop_reading(rows, development_indices, drop_event_recorded_at_utc)
    if reading is None:
        return SkippedRun(
            run_id=run_id,
            reason="no drop reading (no telemetry near the drop event and no usable "
            "development-phase row to fall back on)",
        )
    terminated_abnormally = await _terminated_abnormally(store, run_id, detail.outcome)
    try:
        score = joint_window_score(
            drop_temp_c=reading.bean_temp_c,
            target_drop_temp_c=detail.profile.target_drop_temp_c,
            dtr_percent=reading.development_percent,
            target_dtr_percent=detail.profile.target_development_percent,
            terminated_abnormally=terminated_abnormally,
        )
    except ValueError as exc:
        # joint_window_score fails closed on a non-finite achieved/target value
        # (an inf/nan from a corrupt historical trace or profile) — one corrupt
        # run must not abort the whole corpus; skip it like every other
        # per-run data problem.
        return SkippedRun(run_id=run_id, reason=f"corrupt trace or profile: {exc}")
    try:
        ambient_evidence = await store.read_ambient_doctrine_evidence(run_id)
    except Exception:  # noqa: BLE001 - evidence must never discard a valid RP-D score
        ambient_evidence = derive_ambient_doctrine_evidence(None, (), ())
    return ScoredRun(
        run_id=run_id,
        bean_name=detail.profile.name,
        # A historical +/-Infinity ambient reading (SQLite round-trips it,
        # unlike NaN, which is silently stored as NULL) would otherwise reach
        # report_to_json as a non-standard JSON "Infinity" token, rejected by
        # a strict JSON.parse — normalized to None (display-only field; the
        # score itself never reads it).
        ambient_temp_c=_finite_or_none(detail.ambient_temp_c),
        rating=detail.rating,
        score=score,
        ambient_evidence=ambient_evidence,
    )


async def _discover_finished_run_ids(store: RoastStore) -> list[str]:
    """Discover every finished, non-excluded run id WITHOUT parsing profiles.

    Reads ids directly from ``roast_runs`` (finished ``outcome IS NOT NULL``,
    ``excluded = 0`` per #582) rather than :meth:`RoastStore.list_runs`, which
    validates every run's frozen profile and would raise on the FIRST legacy or
    malformed one — aborting the whole corpus instead of skipping that one run.
    Deferring the profile read to :func:`score_run` keeps a single bad run a
    per-run :class:`SkippedRun`, not a corpus-wide failure.

    Args:
        store: The (snapshot-backed) store to read.

    Returns:
        Finished, non-excluded run ids in ``started_at_utc`` order.
    """
    async with store.connection.execute(
        "SELECT id FROM roast_runs WHERE outcome IS NOT NULL AND excluded = 0"
        " ORDER BY started_at_utc, id"
    ) as cursor:
        return [str(row["id"]) for row in await cursor.fetchall()]


async def score_corpus(store: RoastStore, run_ids: list[str] | None = None) -> CorpusReport:
    """Score every finished, non-excluded run in the store (or an explicit subset).

    With ``run_ids=None``, discovers every finished run (``outcome IS NOT
    NULL``, ``excluded = 0``) via :func:`_discover_finished_run_ids` — an
    id-only query that does NOT parse profiles, so one legacy/malformed frozen
    profile is skipped per-run (#582 soft-discards never contribute) rather than
    aborting the whole corpus. With an explicit ``run_ids``, a repeated id is
    de-duplicated (first occurrence kept — a duplicate would otherwise score
    the same run twice and double-count it in the aggregate), and each
    surviving id is independently checked for existence, exclusion,
    finished-ness, and a parseable frozen profile (so an explicit request for
    an excluded/still-active/malformed run is skipped with a reason, not
    silently bypassed or allowed to abort the whole invocation).

    Args:
        store: The (snapshot-backed) store to read.
        run_ids: An explicit subset of run ids to score, or ``None`` to
            auto-discover every eligible run.

    Returns:
        The full :class:`CorpusReport` (every scored run + every skip, in
        discovery/request order).
    """
    skipped: list[SkippedRun] = []
    candidate_ids: list[str]
    if run_ids is None:
        candidate_ids = await _discover_finished_run_ids(store)
    else:
        candidate_ids = []
        for run_id in dict.fromkeys(run_ids):  # de-dupe, preserve first occurrence
            try:
                detail = await store.read_run(run_id)
            except ValueError as exc:
                skipped.append(
                    SkippedRun(run_id=run_id, reason=f"could not parse frozen profile: {exc}")
                )
                continue
            if detail is None:
                skipped.append(SkippedRun(run_id=run_id, reason="run not found"))
                continue
            if detail.excluded:
                skipped.append(
                    SkippedRun(run_id=run_id, reason="run is excluded (soft-discarded, #582)")
                )
                continue
            if detail.outcome is None:
                skipped.append(
                    SkippedRun(run_id=run_id, reason="run has not finished (outcome is null)")
                )
                continue
            candidate_ids.append(run_id)

    scored: list[ScoredRun] = []
    for run_id in candidate_ids:
        # Categorical resilience: one corrupt run must never abort the whole
        # corpus. score_run already turns the expected failure modes into a
        # SkippedRun, but a store row can carry data no typed accessor
        # anticipates (a non-numeric REAL, malformed payload_json, an
        # incompatible timestamp), and those surface as a raw ValueError /
        # OperationalError / TypeError from deep in the read path. Rather than
        # enumerate every such source at every call site, the corpus loop
        # converts ANY per-run failure into a SkippedRun so the rest still
        # scores. The pure metric core (bakeoff_replay.joint_window_score) is
        # separately unit-tested; this catch guards the per-run I/O, not the
        # arithmetic.
        try:
            result = await score_run(store, run_id)
        except Exception as exc:  # noqa: BLE001 - deliberate per-run resilience backstop
            skipped.append(
                SkippedRun(run_id=run_id, reason=f"scoring failed ({type(exc).__name__}): {exc}")
            )
            continue
        if isinstance(result, ScoredRun):
            scored.append(result)
        else:
            skipped.append(result)
    return CorpusReport(scored=scored, skipped=skipped)


def aggregate_stats(report: CorpusReport) -> dict[str, Any]:
    """The corpus aggregate: N scored, HIT count/rate, mean scalar, rating coverage.

    Args:
        report: The scored corpus.

    Returns:
        A dict with ``n_scored``, ``n_skipped``, ``hits``, ``hit_rate``,
        ``mean_scalar``, and ``rated`` — how many of the scored runs carry a
        D42 operator rating (``0``/``0.0`` for the numeric fields when
        nothing was scored). ``rated`` is the #711 Goodhart-guard evidence
        for ``mean_scalar``: the module's own stated invariant is that the
        scalar is never presented without the rating, so the aggregate must
        say how much of it the mean scalar actually reflects, not just the
        per-row table.
    """
    n_scored = len(report.scored)
    hits = sum(1 for run in report.scored if run.score.hit)
    rated = sum(1 for run in report.scored if run.rating is not None)
    mean_scalar = statistics.fmean(run.score.scalar for run in report.scored) if n_scored else 0.0
    return {
        "n_scored": n_scored,
        "n_skipped": len(report.skipped),
        "hits": hits,
        "hit_rate": (hits / n_scored) if n_scored else 0.0,
        "mean_scalar": mean_scalar,
        "rated": rated,
        "ambient_evidence_observed_runs": sum(
            1
            for run in report.scored
            if run.ambient_evidence.verdict is AmbientEvidenceVerdict.OBSERVED
        ),
        "ambient_evidence_scored_runs": n_scored,
    }


def _fmt_optional(value: float | None, *, precision: int = 1) -> str:
    """Render an optional float, or ``"—"`` when ``None``."""
    return "—" if value is None else f"{value:.{precision}f}"


def _escape_markdown_cell(text: str) -> str:
    """Escape a value for safe interpolation into a Markdown table cell.

    A bean ``name``, a skip ``reason`` (sourced from an exception message),
    or a ``run_id`` (an explicit ``--run-ids`` value need not exist in the
    store, so a bogus/adversarial id reaches the skipped-row table verbatim,
    Codex P3) is uncontrolled operator/corpus text: an embedded ``|`` would
    be read as an extra column delimiter, corrupting every column after it,
    and an embedded newline would break the row out of the table entirely.
    Escapes ``|`` and collapses any newline to a space.

    Args:
        text: The raw cell text.

    Returns:
        The text, safe to place inside a single ``| ... |`` table cell.
    """
    return text.replace("|", "\\|").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _ambient_evidence_cell(evidence: AmbientDoctrineEvidence) -> str:
    """Render the deliberately narrow ambient claim for one Markdown row."""
    if evidence.verdict is AmbientEvidenceVerdict.OBSERVED:
        return (
            "observed "
            f"({evidence.fresh_retained_development_snapshot_count}/"
            f"{evidence.retained_development_snapshot_count})"
        )
    reason = evidence.not_proven_reason
    if reason is None:
        return "not proven (unknown)"
    return f"not proven ({reason.value})"


def render_markdown_table(report: CorpusReport) -> str:
    """Render the per-roast markdown table + corpus aggregate line.

    Every row shows the D42 operator rating alongside the scalar (the #711
    Goodhart guard: the scalar is never shown without the rating); the
    aggregate line extends that guard to the corpus-level mean scalar by also
    stating how many scored runs are rated.

    Args:
        report: The scored corpus.

    Returns:
        A markdown string: the per-roast table, a blank line, then the
        aggregate summary line.
    """
    header = (
        "| Run | Bean | Ambient °C | Target drop/DTR | Achieved drop/DTR | HIT | Scalar | Rating "
        "| Ambient evidence (retained DEVELOPMENT telemetry-snapshot coverage) |"
    )
    separator = "|---|---|---|---|---|---|---|---|---|"
    lines = [header, separator]
    for run in report.scored:
        score = run.score
        run_id = _escape_markdown_cell(run.run_id[:8])
        bean_name = _escape_markdown_cell(run.bean_name)
        lines.append(
            f"| {run_id} | {bean_name} | {_fmt_optional(run.ambient_temp_c)} "
            f"| {score.target_drop_temp_c:.1f} °C / {score.target_dtr_percent:.1f}% "
            f"| {score.drop_temp_c:.1f} °C / {score.dtr_percent:.1f}% "
            f"| {'HIT' if score.hit else 'MISS'} | {score.scalar:.2f} "
            f"| {'—' if run.rating is None else f'{run.rating}★'} "
            f"| {_escape_markdown_cell(_ambient_evidence_cell(run.ambient_evidence))} |"
        )
    for skip in report.skipped:
        skip_run_id = _escape_markdown_cell(skip.run_id[:8])
        reason = _escape_markdown_cell(skip.reason)
        lines.append(f"| {skip_run_id} | (skipped: {reason}) | | | | | | | |")
    stats = aggregate_stats(report)
    aggregate_line = (
        f"\nN scored: {stats['n_scored']} (skipped: {stats['n_skipped']}) | "
        f"HIT: {stats['hits']}/{stats['n_scored']} "
        f"({stats['hit_rate'] * 100:.1f}%) | mean scalar: {stats['mean_scalar']:.4f} "
        f"(rated: {stats['rated']}/{stats['n_scored']}) | ambient evidence observed RUNS: "
        f"{stats['ambient_evidence_observed_runs']}/{stats['ambient_evidence_scored_runs']} "
        "scored RUNS"
    )
    evidence_note = _escape_markdown_cell(
        f"Ambient evidence: {AMBIENT_EVIDENCE_CLAIM} "
        f"({FractionBasis.RETAINED_DEVELOPMENT_SNAPSHOTS.value})"
    )
    return "\n".join(lines) + "\n" + aggregate_line + "\n" + evidence_note


def report_to_json(report: CorpusReport) -> dict[str, Any]:
    """Serialize the full corpus report to a JSON-ready dict.

    Args:
        report: The scored corpus.

    Returns:
        ``{"runs": [...], "skipped": [...], "aggregate": {...}}`` — each run
        entry is :func:`~bakeoff_replay.joint_score_to_json` plus
        ``run_id``/``bean_name``/``ambient_temp_c``/``operator_rating``.
    """
    runs: list[dict[str, Any]] = []
    for run in report.scored:
        entry = joint_score_to_json(run.score)
        entry["run_id"] = run.run_id
        entry["bean_name"] = run.bean_name
        entry["ambient_temp_c"] = run.ambient_temp_c
        entry["operator_rating"] = run.rating
        entry["ambient_doctrine_evidence"] = run.ambient_evidence.model_dump(mode="json")
        runs.append(entry)
    return {
        "ambient_evidence_claim": AMBIENT_EVIDENCE_CLAIM,
        "runs": runs,
        "skipped": [{"run_id": skip.run_id, "reason": skip.reason} for skip in report.skipped],
        "aggregate": aggregate_stats(report),
    }


#: SQLite's live-WAL-mode sidecar filename suffixes (appended directly to the
#: database filename, not a new extension): the write-ahead log, the shared
#: memory index, and the legacy rollback journal. Each is as live/mutable as
#: the database file itself while the operator's agent has the store open.
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _same_file_or_path(a: Path, b: Path) -> bool:
    """Whether ``a`` and ``b`` name the same file.

    Prefers inode identity (:func:`os.path.samefile`) when BOTH paths exist —
    the only way to catch a hard link, which has a different path string but
    the same inode, so a resolved-path string compare alone would miss it.
    Falls back to a resolved-path compare (handles a symlink, and the common
    case where ``a`` does not exist yet, e.g. a ``--json`` output path that
    has never been written).

    Args:
        a: The first path.
        b: The second path.

    Returns:
        ``True`` if the two paths refer to the same file.
    """
    if a.exists() and b.exists():
        try:
            return os.path.samefile(a, b)
        except OSError:  # pragma: no cover - a TOCTOU race, not reachable in tests
            return False
    return a.resolve() == b.resolve()


def _json_out_targets_store(json_out: Path, store_path: Path) -> bool:
    """Whether writing ``json_out`` would touch the store or a live SQLite sidecar.

    Blocks four cases (Codex P1, data loss): the store file itself; either of
    its live WAL-mode sidecars (``-wal``/``-shm``) or the legacy rollback
    journal (``-journal``) — each as mutable as the database while the
    operator's agent holds the store open; a SYMLINK to any of those (caught
    by :func:`_same_file_or_path`'s resolved-path fallback); and a HARD LINK
    to any of those (caught by :func:`_same_file_or_path`'s
    :func:`os.path.samefile` check — a hard link has a different path string
    but the identical inode, so a resolved-path compare alone would miss it).

    Args:
        json_out: The proposed ``--json`` output path.
        store_path: The ``--store`` path.

    Returns:
        ``True`` if ``json_out`` would write to the store or a sidecar.
    """
    resolved_store = store_path.resolve()
    forbidden = [
        store_path,
        *(
            resolved_store.with_name(resolved_store.name + suffix)
            for suffix in _SQLITE_SIDECAR_SUFFIXES
        ),
    ]
    return any(_same_file_or_path(json_out, candidate) for candidate in forbidden)


async def run_corpus_score(store_path: Path, run_ids: list[str] | None) -> CorpusReport:
    """Snapshot the operator store and score its corpus (the script's real work).

    Args:
        store_path: The real operator store path (never opened directly).
        run_ids: An explicit subset of run ids, or ``None`` for full discovery.

    Returns:
        The :class:`CorpusReport`.
    """
    with tempfile.TemporaryDirectory(prefix="rpd-corpus-score-") as tmp_dir:
        snapshot_path = snapshot_store_to_temp(store_path, Path(tmp_dir))
        store = RoastStore(snapshot_path)
        await store.initialize()
        try:
            return await score_corpus(store, run_ids)
        finally:
            await store.close()


async def main() -> int:
    """CLI entrypoint: score the corpus and print/write the report.

    Returns:
        ``0`` always (a run with zero scorable roasts still prints a valid,
        empty report rather than failing).
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--store",
        type=Path,
        default=DEFAULT_STORE,
        help=f"path to the real operator SQLite store (default: {DEFAULT_STORE}); "
        "NEVER opened directly — a private temp snapshot copy is opened instead",
    )
    parser.add_argument(
        "--json",
        dest="json_out",
        type=Path,
        default=None,
        help="also write the full JSON report here",
    )
    parser.add_argument(
        "--run-ids",
        nargs="+",
        default=None,
        help="explicit roast_runs.id(s) to score (default: auto-discover every "
        "finished, non-excluded run)",
    )
    args = parser.parse_args()

    store_path = cast("Path", args.store)
    json_out = cast("Path | None", args.json_out)
    # Refuse a --json path that targets the source store, a live WAL-mode
    # sidecar, or either via a symlink/hard link (see
    # _json_out_targets_store): writing JSON there would truncate and destroy
    # the operator's SQLite database. Check BEFORE any read so a
    # fat-fingered path fails fast.
    if json_out is not None and _json_out_targets_store(json_out, store_path):
        parser.error(
            "--json must not point at the source store or a live SQLite sidecar "
            "(it would overwrite the database)"
        )

    report = await run_corpus_score(store_path, cast("list[str] | None", args.run_ids))
    print(render_markdown_table(report))

    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(report_to_json(report), indent=2), encoding="utf-8")
        print(f"\nwrote JSON report -> {json_out}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint guard
    sys.exit(asyncio.run(main()))
