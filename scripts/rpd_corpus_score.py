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
  ``telemetry_snapshots``, anchored on the LAST row tagged ``agent_phase =
  'development'`` for that run — the same clock-safe anchor
  :meth:`~roastpilot_agent.store.RoastStore._build_reference_roast` uses
  (design note §6.4a): development begins at first crack, so this is never the
  run's final row, which can land deep in the post-drop COOLING tail with a
  falling, physically meaningless bean temperature (the trap: a bare
  ``MAX(tick WHERE development_percent IS NOT NULL)`` lands there because
  ``development_percent`` freezes at drop and is echoed on every later row).
  **One refinement beyond a literal last-development-row read, verified
  against the real store:** the controller flips ``agent_phase`` to
  ``cooling`` SYNCHRONOUSLY within the same tick it executes the drop
  (``api._publish_and_persist_telemetry`` persists ``snapshot.phase`` AFTER
  the tick's transition), so the row immediately FOLLOWING the last
  ``development`` row — not that row itself — carries the true drop-instant
  ``bean_temp_c`` (one control-loop tick fresher) and the just-frozen
  ``development_percent``. Confirmed against the ratified #559 Conebosque A/B
  on the real store: the last-``development``-row read gives 188.0 °C / 19.97 %
  (baseline) and 189.0 °C / 23.34 % (treatment) — both ~1 tick short of the
  ratified 188/21 and 190/24 — while the immediately-following row gives
  188.0 °C / 20.997 % and 190.0 °C / 24.257 %, matching to the nearest
  integer. Used only when that following row has BOTH fields populated
  (never a later, genuinely-cooled-down reading with a missing field); falls
  back to the last ``development`` row's own reading otherwise (e.g. the run
  ended abruptly with no further tick logged, or the following row failed to
  record a temperature that tick).
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
``~/roasts/roastpilot.sqlite3`` directly: :func:`snapshot_store_to_temp`
(the same online-backup pattern ``bakeoff_reference_567.py`` uses) copies it
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
import sqlite3
import statistics
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))  # bakeoff_replay
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bakeoff_replay import JointWindowScore, joint_score_to_json, joint_window_score  # noqa: E402

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


def snapshot_store_to_temp(store_path: Path, tmp_dir: Path) -> Path:
    """Copy the operator store to a private temp file; this script opens ONLY the copy.

    Mirrors :func:`bakeoff_reference_567.snapshot_store_to_temp` exactly: uses
    SQLite's own online backup API (:meth:`sqlite3.Connection.backup`) against
    a strictly read-only (``mode=ro``) source connection, so the snapshot is a
    fully consistent point-in-time copy (including anything still only in the
    source's WAL) without ever acquiring a write lock on the operator's file.
    ``RoastStore.initialize()`` opens read-write and applies WAL/migrations —
    the normal, safe thing for the live agent to do to ITS OWN store — so this
    isolation is what keeps the operator's live database untouched even though
    this script only ever calls read methods on the (temp-copy) store.

    Args:
        store_path: The real operator store to copy.
        tmp_dir: A scratch directory the caller owns and will clean up.

    Returns:
        The path to the private snapshot copy.

    Raises:
        FileNotFoundError: If ``store_path`` does not exist.
    """
    if not store_path.exists():
        raise FileNotFoundError(f"no store at {store_path}")
    snapshot_path = tmp_dir / "store-snapshot.sqlite3"
    source = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(snapshot_path))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return snapshot_path


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
    """Every telemetry row for ``run_id``, in persisted (tick, id) order."""
    async with store.connection.execute(
        "SELECT agent_phase, bean_temp_c, development_percent FROM telemetry_snapshots"
        " WHERE run_id = ? ORDER BY tick ASC, id ASC",
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


def _extract_drop_reading(
    rows: list[sqlite3.Row], development_indices: list[int]
) -> DropReading | None:
    """The achieved drop reading given pre-fetched rows and their development indices.

    Anchored on the LAST ``development``-phase telemetry row — the same
    clock-safe anchor
    :meth:`~roastpilot_agent.store.RoastStore._build_reference_roast` uses
    (design note §6.4a): this is never the run's chronologically final row (a
    post-drop COOLING-tail sample with a falling bean temperature), and never
    a bare ``MAX(tick)`` over rows with a non-null ``development_percent`` (a
    predicate cooling-tail rows also satisfy, since ``development_percent``
    freezes at drop and is echoed on every later row — that trap misreads the
    drop far into the tail).

    **Refinement, verified against the real store (module docstring):** the
    row immediately FOLLOWING the last ``development`` row is preferred when
    it has both fields populated — the controller flips ``agent_phase`` to
    ``cooling`` synchronously within the drop tick, so that following row (not
    the last ``development``-tagged one) carries the true drop-instant
    ``bean_temp_c`` and the just-frozen ``development_percent``. Falls back to
    the last ``development`` row's own reading when there is no following row,
    or it is missing either field (e.g. a failed telemetry read that tick, or
    a later, already-cooling reading with no fresh percentage).

    Args:
        rows: Every telemetry row for the run, in ``(tick, id)`` order (see
            :func:`_fetch_telemetry_rows`).
        development_indices: Indices of every ``development``-phase row (see
            :func:`_development_row_indices`); must be non-empty.

    Returns:
        The :class:`DropReading`, or ``None`` when neither the last
        ``development`` row nor its immediate successor has both fields.
    """
    last_dev_index = development_indices[-1]

    if last_dev_index + 1 < len(rows):
        transition_row = rows[last_dev_index + 1]
        transition_temp = _optional_float(transition_row["bean_temp_c"])
        transition_dtr = _optional_float(transition_row["development_percent"])
        if transition_temp is not None and transition_dtr is not None:
            return DropReading(bean_temp_c=transition_temp, development_percent=transition_dtr)

    last_dev = rows[last_dev_index]
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


async def _has_drop_command(store: RoastStore, run_id: str) -> bool:
    """Whether ``run_id`` recorded an executed bean drop.

    A drop is a ``command_executed`` :class:`~roastpilot_agent.models.RoastEventKind`
    event whose payload ``command`` is
    :attr:`~roastpilot_agent.models.RoastCommand.DROP_BEANS`'s value — emitted
    for a policy, advisor, operator, OR ceiling-guard drop
    (:meth:`RoastController._execute_drop` / ``_maybe_ceiling_guard_drop``), so
    it is the single signal that the beans were actually dropped. A run that
    reaches DEVELOPMENT and is then cooled/ended WITHOUT a drop (e.g. an operator
    ``start_cooling`` recovery) has development telemetry but no drop event; its
    post-development rows are not a drop reading and must not be scored as one.
    The command value is bound as a query parameter (mirrors the typed-value
    discipline of the guard/verdict queries).

    Args:
        store: The (snapshot-backed) store to read.
        run_id: The run to check.

    Returns:
        ``True`` if an executed ``drop_beans`` command event exists.
    """
    async with store.connection.execute(
        "SELECT 1 FROM roast_events WHERE run_id = ? AND kind = ?"
        " AND json_extract(payload_json, '$.command') = ? LIMIT 1",
        (run_id, RoastEventKind.COMMAND_EXECUTED.value, RoastCommand.DROP_BEANS.value),
    ) as cursor:
        return await cursor.fetchone() is not None


async def score_run(store: RoastStore, run_id: str) -> ScoredRun | SkippedRun:
    """Score one run, or explain why it could not be scored.

    Args:
        store: The (snapshot-backed) store to read.
        run_id: The ``roast_runs.id`` to score.

    Returns:
        A :class:`ScoredRun` on success, or a :class:`SkippedRun` naming the
        reason (run not found, an unparseable/legacy frozen profile missing
        the required drop/DTR targets, no executed ``drop_beans`` command event,
        no ``development``-phase telemetry row at all, or a development-phase row
        missing ``bean_temp_c``/``development_percent``).
    """
    try:
        detail = await store.read_run(run_id)
    except ValueError as exc:
        return SkippedRun(run_id=run_id, reason=f"could not parse frozen profile: {exc}")
    if detail is None:
        return SkippedRun(run_id=run_id, reason="run not found")
    rows = await _fetch_telemetry_rows(store, run_id)
    development_indices = _development_row_indices(rows)
    if not development_indices:
        return SkippedRun(run_id=run_id, reason="no development-phase telemetry row")
    if not await _has_drop_command(store, run_id):
        return SkippedRun(
            run_id=run_id,
            reason="no drop_beans command event (run cooled/ended without a bean drop)",
        )
    reading = _extract_drop_reading(rows, development_indices)
    if reading is None:
        return SkippedRun(
            run_id=run_id,
            reason="development-phase telemetry missing bean_temp_c or development_percent",
        )
    terminated_abnormally = await _terminated_abnormally(store, run_id, detail.outcome)
    score = joint_window_score(
        drop_temp_c=reading.bean_temp_c,
        target_drop_temp_c=detail.profile.target_drop_temp_c,
        dtr_percent=reading.development_percent,
        target_dtr_percent=detail.profile.target_development_percent,
        terminated_abnormally=terminated_abnormally,
    )
    return ScoredRun(
        run_id=run_id,
        bean_name=detail.profile.name,
        ambient_temp_c=detail.ambient_temp_c,
        rating=detail.rating,
        score=score,
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
    aborting the whole corpus. With an explicit ``run_ids``, each id is
    independently checked for existence, exclusion, and finished-ness (so an
    explicit request for an excluded or still-active run is skipped with a
    reason, not silently bypassed).

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
        for run_id in run_ids:
            detail = await store.read_run(run_id)
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
        result = await score_run(store, run_id)
        if isinstance(result, ScoredRun):
            scored.append(result)
        else:
            skipped.append(result)
    return CorpusReport(scored=scored, skipped=skipped)


def aggregate_stats(report: CorpusReport) -> dict[str, Any]:
    """The corpus aggregate: N scored, HIT count/rate, mean scalar.

    Args:
        report: The scored corpus.

    Returns:
        A dict with ``n_scored``, ``n_skipped``, ``hits``, ``hit_rate``, and
        ``mean_scalar`` (``0.0`` for both when nothing was scored).
    """
    n_scored = len(report.scored)
    hits = sum(1 for run in report.scored if run.score.hit)
    mean_scalar = statistics.fmean(run.score.scalar for run in report.scored) if n_scored else 0.0
    return {
        "n_scored": n_scored,
        "n_skipped": len(report.skipped),
        "hits": hits,
        "hit_rate": (hits / n_scored) if n_scored else 0.0,
        "mean_scalar": mean_scalar,
    }


def _fmt_optional(value: float | None, *, precision: int = 1) -> str:
    """Render an optional float, or ``"—"`` when ``None``."""
    return "—" if value is None else f"{value:.{precision}f}"


def render_markdown_table(report: CorpusReport) -> str:
    """Render the per-roast markdown table + corpus aggregate line.

    Every row shows the D42 operator rating alongside the scalar (the #711
    Goodhart guard: the scalar is never shown without the rating).

    Args:
        report: The scored corpus.

    Returns:
        A markdown string: the per-roast table, a blank line, then the
        aggregate summary line.
    """
    header = (
        "| Run | Bean | Ambient °C | Target drop/DTR | Achieved drop/DTR | HIT | Scalar | Rating |"
    )
    separator = "|---|---|---|---|---|---|---|---|"
    lines = [header, separator]
    for run in report.scored:
        score = run.score
        lines.append(
            f"| {run.run_id[:8]} | {run.bean_name} | {_fmt_optional(run.ambient_temp_c)} "
            f"| {score.target_drop_temp_c:.1f} °C / {score.target_dtr_percent:.1f}% "
            f"| {score.drop_temp_c:.1f} °C / {score.dtr_percent:.1f}% "
            f"| {'HIT' if score.hit else 'MISS'} | {score.scalar:.2f} "
            f"| {'—' if run.rating is None else f'{run.rating}★'} |"
        )
    for skip in report.skipped:
        lines.append(f"| {skip.run_id[:8]} | (skipped: {skip.reason}) | | | | | | |")
    stats = aggregate_stats(report)
    aggregate_line = (
        f"\nN scored: {stats['n_scored']} (skipped: {stats['n_skipped']}) | "
        f"HIT: {stats['hits']}/{stats['n_scored']} "
        f"({stats['hit_rate'] * 100:.1f}%) | mean scalar: {stats['mean_scalar']:.4f}"
    )
    return "\n".join(lines) + "\n" + aggregate_line


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
        runs.append(entry)
    return {
        "runs": runs,
        "skipped": [{"run_id": skip.run_id, "reason": skip.reason} for skip in report.skipped],
        "aggregate": aggregate_stats(report),
    }


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
    # Refuse a --json path that resolves to the source store (directly or via a
    # symlink): writing JSON there would truncate and destroy the operator's
    # SQLite database. Check BEFORE any read so a fat-fingered path fails fast.
    if json_out is not None and json_out.resolve() == store_path.resolve():
        parser.error("--json must not point at the source store (it would overwrite the database)")

    report = await run_corpus_score(store_path, cast("list[str] | None", args.run_ids))
    print(render_markdown_table(report))

    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(report_to_json(report), indent=2))
        print(f"\nwrote JSON report -> {json_out}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint guard
    sys.exit(asyncio.run(main()))
