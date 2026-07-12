"""Convert Artisan ``.alog`` roast logs into bake-off replay fixtures.

The advisor bake-off (:mod:`bakeoff_replay`) replays a *known-good* roast
tick-by-tick and scores a candidate model's advice against what the human
actually did. Its first run used only the two 7-Jun live-roast captures
(``tests/fixtures/live-roast-2026-06-07``), giving ``N=2`` — one drop instance
each, so the drop-F1 was coarse. This adapter expands the test set from the
operator's annotated Artisan ``.alog`` history.

Artisan ``.alog`` format (a ``ast.literal_eval``-able Python ``dict``):

- ``timex`` — the sample timeline in seconds from record start.
- ``temp1`` — environment temperature (ET) series, parallel to ``timex``.
- ``temp2`` — bean temperature (BT) series, parallel to ``timex``.
- ``timeindex`` — ``[CHARGE, DRYe, FCs, FCe, SCs, SCe, DROP, COOL]``: each entry
  is an *index into* ``timex`` (``0`` = not marked).
- ``specialevents`` / ``specialeventstype`` / ``specialeventsvalue`` — the
  manual control track. ``type`` ``3`` = Burner (heat), ``0`` = Air (fan); the
  stored ``value`` decodes to a 0–100 % setpoint as ``(value - 1) * 10`` (the
  Artisan slider encoding). Events are sparse step-changes — the setpoint at any
  tick is the most recent prior event of that type (carry-forward).

The emitted fixture matches what :func:`bakeoff_replay.load_roast` consumes: a
``roast.jsonl`` of ``telemetry`` rows plus three ``event`` rows
(``beans_added`` / ``first_crack_detected`` / ``beans_dropped``), and a sibling
``summary.json`` for parity with the live-roast fixtures.

Temperatures are Celsius throughout (the Hottop/Artisan logs are already °C).

These fixtures derive from the operator's personal roast logs, so per
``AGENTS.md`` they are **not** committed — the adapter writes them to a local
working directory (``--out-dir``, gitignored). Only the adapter, the scorecard,
and the run manifest are committed.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))

from roast_degree import classify_degree  # noqa: E402

# Artisan timeindex slots.
_CHARGE, _FCS, _DROP = 0, 2, 6
# Artisan special-event types.
_TYPE_FAN = 0
_TYPE_HEAT = 3
# Sentinel Artisan writes for "no reading" in a temperature series.
_ARTISAN_NODATA = -1.0


@dataclass(frozen=True)
class RoastMarks:
    """The charge / first-crack / drop reference points of one roast.

    Attributes:
        charge_seconds: ``beans_added`` time (``timex`` at the CHARGE index).
        first_crack_seconds: First-crack time (``timex`` at the FCs index).
        drop_seconds: Bean-drop time (``timex`` at the DROP index).
        first_crack_temp_c: Bean temperature at the first-crack sample.
        drop_temp_c: Bean temperature at the drop sample.
    """

    charge_seconds: float
    first_crack_seconds: float
    drop_seconds: float
    first_crack_temp_c: float
    drop_temp_c: float


def _decode_setpoint(raw: float) -> int:
    """Decode an Artisan ``specialeventsvalue`` into a 0–100 % setpoint.

    Args:
        raw: The stored event value (Artisan encodes a slider ``p`` as
            ``p / 10 + 1``).

    Returns:
        The setpoint percentage, clamped to ``[0, 100]``.
    """
    percent = round((raw - 1.0) * 10.0)
    return max(0, min(100, percent))


def _step_track(
    timex: list[float],
    events: list[int],
    types: list[int],
    values: list[float],
    wanted_type: int,
) -> list[tuple[float, int]]:
    """Build a sorted ``(time_seconds, setpoint_percent)`` step track for a lever.

    Args:
        timex: The roast timeline.
        events: ``specialevents`` — indices into ``timex``.
        types: ``specialeventstype`` parallel to ``events``.
        values: ``specialeventsvalue`` parallel to ``events``.
        wanted_type: ``_TYPE_HEAT`` or ``_TYPE_FAN``.

    Returns:
        The lever's step changes in time order.
    """
    track: list[tuple[float, int]] = []
    for index, etype, value in zip(events, types, values, strict=False):
        if etype != wanted_type:
            continue
        if not 0 <= index < len(timex):
            continue
        track.append((float(timex[index]), _decode_setpoint(float(value))))
    track.sort(key=lambda item: item[0])
    return track


def _level_at(track: list[tuple[float, int]], when: float) -> int:
    """Carry-forward setpoint of a step ``track`` at time ``when`` (0 before first)."""
    level = 0
    for event_time, value in track:
        if event_time <= when:
            level = value
        else:
            break
    return level


def load_alog(path: Path) -> dict[str, Any]:
    """Parse an Artisan ``.alog`` into its raw ``dict``.

    Args:
        path: The ``.alog`` file.

    Returns:
        The parsed profile dictionary.

    Raises:
        ValueError: If the file does not parse into a ``dict``.
    """
    parsed = ast.literal_eval(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} did not parse into a dict")
    return cast("dict[str, Any]", parsed)


def extract_marks(profile: dict[str, Any]) -> RoastMarks:
    """Extract the charge / first-crack / drop marks from a parsed ``.alog``.

    Args:
        profile: A parsed Artisan profile dict.

    Returns:
        The roast's reference marks.

    Raises:
        ValueError: If the log lacks a marked first crack or drop, or the
            timeline is too short to resolve them.
    """
    timex: list[float] = [float(value) for value in profile.get("timex", [])]
    bean: list[float] = [float(value) for value in profile.get("temp2", [])]
    timeindex: list[int] = [int(value) for value in profile.get("timeindex", [])]
    if len(timeindex) <= _DROP:
        raise ValueError("timeindex too short")
    charge_i, fc_i, drop_i = timeindex[_CHARGE], timeindex[_FCS], timeindex[_DROP]
    if fc_i <= 0 or drop_i <= 0:
        raise ValueError("log lacks a marked first crack or drop")
    if drop_i >= len(timex) or drop_i >= len(bean):
        raise ValueError("drop index past the timeline")
    return RoastMarks(
        charge_seconds=float(timex[charge_i]),
        first_crack_seconds=float(timex[fc_i]),
        drop_seconds=float(timex[drop_i]),
        first_crack_temp_c=float(bean[fc_i]),
        drop_temp_c=float(bean[drop_i]),
    )


def _last_valid(series: list[float], index: int, fallback: float) -> float:
    """Return ``series[index]`` or the nearest prior non-sentinel value."""
    value = series[index]
    if value > _ARTISAN_NODATA:
        return value
    for past in range(index - 1, -1, -1):
        if series[past] > _ARTISAN_NODATA:
            return series[past]
    return fallback


def build_fixture_rows(profile: dict[str, Any], marks: RoastMarks) -> list[dict[str, Any]]:
    """Build the ``roast.jsonl`` rows (telemetry + events) for one roast.

    Emits one telemetry row per ``timex`` sample from record start through the
    drop (inclusive), each carrying the carry-forward heat/fan setpoint, then the
    three event rows the scorer requires.

    Args:
        profile: A parsed Artisan profile dict.
        marks: The roast's reference marks.

    Returns:
        The ordered fixture rows.
    """
    timex: list[float] = [float(value) for value in profile.get("timex", [])]
    env: list[float] = [float(value) for value in profile.get("temp1", [])]
    bean: list[float] = [float(value) for value in profile.get("temp2", [])]
    events = [int(value) for value in profile.get("specialevents", [])]
    types = [int(value) for value in profile.get("specialeventstype", [])]
    values = [float(value) for value in profile.get("specialeventsvalue", [])]
    heat_track = _step_track(timex, events, types, values, _TYPE_HEAT)
    fan_track = _step_track(timex, events, types, values, _TYPE_FAN)

    drop_index = min(range(len(timex)), key=lambda i: abs(timex[i] - marks.drop_seconds))
    rows: list[dict[str, Any]] = []
    for index in range(drop_index + 1):
        when = timex[index]
        rows.append(
            {
                "type": "telemetry",
                "monotonic_seconds": round(when, 3),
                "bean_temp_c": round(_last_valid(bean, index, 20.0), 1),
                "env_temp_c": round(_last_valid(env, index, 20.0), 1),
                "heat_level_percent": _level_at(heat_track, when),
                "fan_level_percent": _level_at(fan_track, when),
            }
        )
    rows.extend(
        [
            {
                "type": "event",
                "kind": "beans_added",
                "monotonic_seconds": round(marks.charge_seconds, 3),
            },
            {
                "type": "event",
                "kind": "first_crack_detected",
                "monotonic_seconds": round(marks.first_crack_seconds, 3),
            },
            {
                "type": "event",
                "kind": "beans_dropped",
                "monotonic_seconds": round(marks.drop_seconds, 3),
            },
        ]
    )
    return rows


def _summary(profile: dict[str, Any], marks: RoastMarks) -> dict[str, Any]:
    """Build a minimal ``summary.json`` mirroring the live-roast fixtures."""
    span = marks.drop_seconds - marks.charge_seconds
    dev = marks.drop_seconds - marks.first_crack_seconds
    drop_temp_c = round(marks.drop_temp_c, 1)
    return {
        "active": False,
        "phase": "complete",
        "source": "artisan-alog",
        "roaster_driver": "hottop_kn8828b_2k_plus",
        "first_crack_temp_c": round(marks.first_crack_temp_c, 1),
        "drop_temp_c": drop_temp_c,
        "development_time_seconds": round(dev, 1),
        "development_time_percent": round(dev / span * 100, 1) if span > 0 else None,
        "total_roast_seconds": round(span, 1),
        # Outcome label (#300): an .alog has no operator rating, so it is null;
        # degree is the shared drop-temperature rule.
        "operator_rating": None,
        "operator_notes": None,
        "degree": classify_degree(drop_temp_c),
        # Objective outcome label (#388): this adapter does not extract Artisan's
        # in/out weight, so the weight-loss label is null here — kept in the key set
        # for parity with store_to_fixture (the bake-off reads both identically).
        # Extracting .alog weight is a separate enhancement.
        "charge_weight_grams": None,
        "roasted_weight_grams": None,
        "weight_loss_percent": None,
        # Tasting corpus label (#522): an .alog has no tasting concept — empty
        # list, kept in the key set for parity with store_to_fixture.
        "tastings": [],
    }


def origin_family(stem: str) -> str:
    """Derive a non-sensitive origin label from a roast filename.

    Strips the date/time/batch tokens (the only arguably-personal parts) and
    keeps the bean-origin words, so the committed manifest can describe a roast
    by origin (``kona``, ``costarica-hermosa``) without leaking exact session
    timestamps.

    Args:
        stem: The ``.alog`` filename stem.

    Returns:
        A hyphenated origin label, or ``"roast"`` if none survives.
    """
    cleaned = re.sub(r"\d{2}-\d{2}-\d{2}", "", stem)  # dates
    cleaned = re.sub(r"_\d{3,4}", "", cleaned)  # HHMM
    cleaned = re.sub(r"\d+", "", cleaned)  # batch numbers
    parts = [token for token in re.split(r"[-_]+", cleaned) if len(token) > 1]
    return "-".join(parts) or "roast"


def convert(path: Path, out_root: Path, label: str, origin: str) -> dict[str, Any]:
    """Convert one ``.alog`` into an anonymized fixture directory.

    Args:
        path: The source ``.alog``.
        out_root: The working directory to write ``<label>/roast.jsonl`` under.
        label: The anonymized fixture id (e.g. ``artisan-07``) — used as the
            directory name, so it is the roast name in the scorecard.
        origin: The non-sensitive origin family for the committed manifest.

    Returns:
        A manifest entry describing the converted roast (no source filename or
        timestamp — those stay in the gitignored source map).

    Raises:
        ValueError: Propagated from :func:`extract_marks` for unusable logs.
    """
    profile = load_alog(path)
    marks = extract_marks(profile)
    rows = build_fixture_rows(profile, marks)
    out_dir = out_root / label
    out_dir.mkdir(parents=True, exist_ok=True)
    fixture = out_dir / "roast.jsonl"
    fixture.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    summary = _summary(profile, marks)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    span = marks.drop_seconds - marks.charge_seconds
    dev = marks.drop_seconds - marks.first_crack_seconds
    return {
        "label": label,
        "origin": origin,
        "fixture": str(fixture),
        "drop_temp_c": round(marks.drop_temp_c, 1),
        "development_time_ratio_percent": round(dev / span * 100, 1) if span > 0 else None,
        "telemetry_rows": sum(1 for r in rows if r["type"] == "telemetry"),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI: convert a directory of ``.alog`` logs, filtered by drop temperature.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv``).

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("alog_dir", type=Path, help="directory of .alog files")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(".artisan-fixtures"),
        help="working directory for emitted fixtures (gitignored)",
    )
    parser.add_argument(
        "--max-drop-c",
        type=float,
        default=198.0,
        help="exclude roasts that dropped at or above this bean temp (operator ceiling)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="optional path to write the JSON manifest of selected roasts",
    )
    args = parser.parse_args(argv)

    # Collect the qualifying roasts first, then order by drop temperature and
    # assign stable anonymized labels (``artisan-NN``) — so the committed
    # scorecard never carries roast dates.
    qualifying: list[tuple[Path, RoastMarks]] = []
    skipped: list[dict[str, str]] = []
    for path in sorted(args.alog_dir.glob("*.alog")):
        try:
            marks = extract_marks(load_alog(path))
        except (ValueError, SyntaxError) as exc:
            skipped.append({"source": path.name, "reason": str(exc)})
            continue
        if marks.drop_temp_c >= args.max_drop_c:
            skipped.append(
                {
                    "source": path.name,
                    "reason": f"drop {marks.drop_temp_c:.0f} >= {args.max_drop_c:.0f}",
                }
            )
            continue
        qualifying.append((path, marks))

    qualifying.sort(key=lambda item: item[1].drop_temp_c)
    selected: list[dict[str, Any]] = []
    source_map: dict[str, str] = {}
    for index, (path, _marks) in enumerate(qualifying, start=1):
        label = f"artisan-{index:02d}"
        entry = convert(path, args.out_dir, label, origin_family(path.stem))
        selected.append(entry)
        source_map[label] = path.name

    print(
        f"selected {len(selected)} roasts (drop < {args.max_drop_c:.0f} °C); skipped {len(skipped)}"
    )
    for entry in selected:
        print(
            f"  {entry['label']:11} {entry['origin']:22} drop {entry['drop_temp_c']:5.0f}  "
            f"DTR {entry['development_time_ratio_percent']}%  rows {entry['telemetry_rows']}"
        )
    # The label -> source-filename map stays beside the gitignored fixtures.
    (args.out_dir / ".source-map.json").write_text(
        json.dumps(source_map, indent=2), encoding="utf-8"
    )
    if args.manifest is not None:
        args.manifest.write_text(
            json.dumps({"selected": selected, "skipped_count": len(skipped)}, indent=2),
            encoding="utf-8",
        )
        print(f"manifest -> {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
