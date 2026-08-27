"""Report joint-window planner classifications from one local JSONL fixture.

This offline reporter exercises the planner's arithmetic and classification with
an RoR estimate from the recorded series; that estimate is not the live,
smoothed MCP input.  Its rows say what the planner would have said, not whether
another operator action would have improved a roast.  Synthetic tests prove
mechanism correctness only: they are not recorded-fixture acceptance and make
no RP-D physical-outcome claim.

The input grammar is deliberately strict because an operator-supplied local
fixture is still untrusted input.  Unknown telemetry columns and event kinds
are ignored because they cannot change a name-keyed boundary; an unknown row
type is refused because silently omitting a telemetry variant could do so.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast

from roastpilot_agent.config import ControllerConfig, JointWindowPlanner
from roastpilot_agent.models import JointWindowStatus
from roastpilot_agent.post_fc_control import JointWindowInputs, plan_joint_window

_MAX_FIXTURE_BYTES = 16 * 1024 * 1024
_MAX_LINE_BYTES = 64 * 1024
_MAX_LINES = 200_000
_ROR_WINDOW_SECONDS = 60.0
_REPO_ROOT = Path(__file__).resolve().parents[1]
_REQUIRED_EVENTS = frozenset({"beans_added", "first_crack_detected", "beans_dropped"})
_LIMITATIONS = (
    "Offline RoR is a recorded-series estimate, not the live smoothed input.",
    "Statuses say what the planner would have said, not that another action would improve a roast.",
)


class FixtureError(ValueError):
    """A data-free refusal of CLI, path, or fixture input."""


class _Parser(argparse.ArgumentParser):
    """Argument parser whose errors follow the validator's refusal grammar."""

    def error(self, message: str) -> NoReturn:
        """Raise a typed, data-free CLI refusal instead of printing usage.

        Args:
            message: The grammar detail, intentionally never echoed.
        """
        del message
        raise FixtureError("cli rule: invalid arguments")


@dataclass(frozen=True)
class TelemetryRow:
    """One validated telemetry sample in the fixture's recorded order."""

    monotonic_seconds: float
    bean_temp_c: float
    line_number: int


@dataclass(frozen=True)
class ParsedFixture:
    """Validated fixture records and the three named roast boundaries."""

    telemetry: list[TelemetryRow]
    t0_seconds: float
    first_crack_seconds: float
    drop_seconds: float


@dataclass(frozen=True)
class ReportRow:
    """One charge-rebased planner report row."""

    charge_s: float
    dev_s: float
    dtr_pct: float | None
    bean_c: float
    ror_c_min: float | None
    status: JointWindowStatus | None
    effective_target_temp_c: float | None
    window_open_runway_seconds: float | None
    window_close_runway_seconds: float | None
    projected_temp_at_open_c: float | None
    projected_temp_at_close_c: float | None


@dataclass(frozen=True)
class ValidationReport:
    """The complete data-free validator result, ready for text or JSON output."""

    target_drop_temp_c: float
    target_development_percent: float
    development_percent_min: float
    development_percent_max: float
    ceiling_temp_c: float
    temp_margin_c: float
    closing_horizon_seconds: float
    rows: list[ReportRow]
    limitations: tuple[str, str]


def _reject_json_constant(_value: str) -> NoReturn:
    """Reject JSON's non-standard non-finite numeric tokens.

    Args:
        _value: The parser-provided token, intentionally never echoed.
    """
    raise FixtureError("fixture rule: non-finite JSON constant")


def _number(value: object, field: str, line_number: int) -> float:
    """Return a finite JSON number or raise a line-numbered refusal.

    Args:
        value: Candidate decoded JSON value.
        field: Required field name for the refusal rule.
        line_number: One-based fixture line number.

    Returns:
        A finite floating-point value.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FixtureError(f"fixture rule: {field} must be a number at line {line_number}")
    try:
        numeric = float(value)
    except OverflowError as error:
        raise FixtureError(f"fixture rule: {field} must be finite at line {line_number}") from error
    if not math.isfinite(numeric):
        raise FixtureError(f"fixture rule: {field} must be finite at line {line_number}")
    return numeric


def _parse_line(line: str, line_number: int) -> dict[str, Any] | None:
    """Decode one fixture line under the closed JSON object grammar.

    Args:
        line: Strictly decoded UTF-8 fixture text.
        line_number: One-based fixture line number.

    Returns:
        The decoded object, or ``None`` for a blank line.
    """
    if not line.strip():
        return None
    try:
        parsed = json.loads(line, parse_constant=_reject_json_constant)
    except FixtureError as error:
        raise FixtureError(f"fixture rule: invalid JSON constant at line {line_number}") from error
    except json.JSONDecodeError as error:
        raise FixtureError(f"fixture rule: invalid JSON at line {line_number}") from error
    except (RecursionError, ValueError) as error:
        raise FixtureError(f"fixture rule: invalid JSON at line {line_number}") from error
    if not isinstance(parsed, dict):
        raise FixtureError(f"fixture rule: JSON object required at line {line_number}")
    return cast("dict[str, Any]", parsed)


def read_fixture(fixture: Path) -> ParsedFixture:
    """Read one local roast JSONL fixture with bounded, strict parsing.

    Args:
        fixture: Operator-supplied fixture path.

    Returns:
        Validated telemetry and required event timestamps.

    Raises:
        FixtureError: If the path, input size, grammar, or ordering is invalid.
    """
    try:
        fixture_path = fixture.expanduser()
        fixture_stat = fixture_path.stat()
    except (OSError, RuntimeError, ValueError) as error:
        raise FixtureError("fixture rule: existing regular file required") from error
    if not stat.S_ISREG(fixture_stat.st_mode):
        raise FixtureError("fixture rule: regular file required")
    if fixture_stat.st_size > _MAX_FIXTURE_BYTES:
        raise FixtureError("fixture rule: file size exceeds limit")

    telemetry: list[TelemetryRow] = []
    events: dict[str, float] = {}
    byte_count = 0
    line_number = 0
    try:
        with fixture_path.open("rb") as source:
            while raw_line := source.readline(_MAX_LINE_BYTES + 1):
                line_number += 1
                if line_number > _MAX_LINES:
                    raise FixtureError("fixture rule: line count exceeds limit")
                byte_count += len(raw_line)
                if byte_count > _MAX_FIXTURE_BYTES:
                    raise FixtureError("fixture rule: file size exceeds limit")
                if len(raw_line) > _MAX_LINE_BYTES:
                    raise FixtureError(
                        f"fixture rule: line length exceeds limit at line {line_number}"
                    )
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise FixtureError(
                        f"fixture rule: UTF-8 required at line {line_number}"
                    ) from error
                row = _parse_line(line, line_number)
                if row is None:
                    continue
                row_type = row.get("type")
                if not isinstance(row_type, str) or row_type not in {"telemetry", "event"}:
                    raise FixtureError(
                        f"fixture rule: known string type required at line {line_number}"
                    )
                if row_type == "telemetry":
                    telemetry.append(
                        TelemetryRow(
                            monotonic_seconds=_number(
                                row.get("monotonic_seconds"), "monotonic_seconds", line_number
                            ),
                            bean_temp_c=_number(row.get("bean_temp_c"), "bean_temp_c", line_number),
                            line_number=line_number,
                        )
                    )
                    continue
                kind = row.get("kind")
                if not isinstance(kind, str):
                    raise FixtureError(
                        f"fixture rule: event kind must be a string at line {line_number}"
                    )
                timestamp = _number(row.get("monotonic_seconds"), "monotonic_seconds", line_number)
                if kind in _REQUIRED_EVENTS:
                    if kind in events:
                        raise FixtureError(
                            f"fixture rule: duplicate required event at line {line_number}"
                        )
                    events[kind] = timestamp
    except FixtureError:
        raise
    except OSError as error:
        raise FixtureError("fixture rule: could not read regular file") from error

    missing_events = _REQUIRED_EVENTS - events.keys()
    if missing_events:
        raise FixtureError("fixture rule: all required events must be present")
    if not telemetry:
        raise FixtureError("fixture rule: telemetry required")
    if any(
        current.monotonic_seconds < previous.monotonic_seconds
        for previous, current in zip(telemetry, telemetry[1:], strict=False)
    ):
        raise FixtureError("fixture rule: telemetry clock must be non-decreasing")

    t0_seconds = events["beans_added"]
    first_crack_seconds = events["first_crack_detected"]
    drop_seconds = events["beans_dropped"]
    if not t0_seconds <= first_crack_seconds <= drop_seconds:
        raise FixtureError("fixture rule: required event order must be charge, first crack, drop")
    return ParsedFixture(telemetry, t0_seconds, first_crack_seconds, drop_seconds)


def _ror_series(telemetry: list[TelemetryRow]) -> list[float | None]:
    """Estimate RoR for every recorded sample in one forward cursor pass.

    Args:
        telemetry: Full recorded telemetry, including pre-first-crack history.

    Returns:
        °C/min values rounded to three places, or ``None`` without sufficient
        history. For each row, the anchor is the nearest earlier persisted row
        at least sixty seconds old.
    """
    estimates: list[float | None] = []
    cursor = 0
    for index, now in enumerate(telemetry):
        while (
            cursor + 1 < index
            and now.monotonic_seconds - telemetry[cursor + 1].monotonic_seconds
            >= _ROR_WINDOW_SECONDS
        ):
            cursor += 1
        if cursor >= index:
            estimates.append(None)
            continue
        elapsed_seconds = now.monotonic_seconds - telemetry[cursor].monotonic_seconds
        if elapsed_seconds < _ROR_WINDOW_SECONDS:
            estimates.append(None)
            continue
        ror_c_per_min = (now.bean_temp_c - telemetry[cursor].bean_temp_c) / elapsed_seconds * 60.0
        estimates.append(round(ror_c_per_min, 3) if math.isfinite(ror_c_per_min) else None)
    return estimates


def _controller_config() -> ControllerConfig:
    """Construct the enforced joint-window configuration exactly once per report.

    Returns:
        The repository's current joint-window configuration.

    Raises:
        FixtureError: If the committed cross-field configuration is invalid.
    """
    try:
        return ControllerConfig(joint_window_planner=JointWindowPlanner(enabled=True))
    except ValueError as error:  # pragma: no cover - a committed config invariant cannot fail here
        raise FixtureError("configuration rule: enabled joint-window planner is invalid") from error


def _validate_targets(
    target_drop_temp_c: float, target_development_percent: float, config: ControllerConfig
) -> tuple[float, float]:
    """Validate explicit targets and derive the one committed DTR window.

    Args:
        target_drop_temp_c: Explicit profile temperature target, °C.
        target_development_percent: Explicit profile development target, percent.
        config: The committed controller configuration.

    Returns:
        The DTR window's lower and upper percentage bounds.

    Raises:
        FixtureError: If a target or derived window fraction is unusable.
    """
    if not math.isfinite(target_drop_temp_c) or target_drop_temp_c <= 0.0:
        raise FixtureError("cli rule: target-drop-temp-c must be finite and greater than zero")
    if (
        not math.isfinite(target_development_percent)
        or not 0.0 < target_development_percent < 100.0
    ):
        raise FixtureError("cli rule: target-development-percent must be finite and in (0, 100)")
    margin = config.drop_dev_margin_percent
    minimum = target_development_percent - margin
    maximum = target_development_percent + margin
    if not 0.0 < minimum / 100.0 < 1.0 or not 0.0 < maximum / 100.0 < 1.0:
        raise FixtureError("cli rule: derived development window fractions must be in (0, 1)")
    return minimum, maximum


def _same_file_or_path(first: Path, second: Path) -> bool:
    """Return whether two paths resolve to one filesystem object.

    Args:
        first: First candidate path.
        second: Second candidate path.

    Returns:
        ``True`` for resolved-path or inode identity.
    """
    try:
        if "\x00" in os.fspath(first) or "\x00" in os.fspath(second):
            raise ValueError("embedded null byte")
        if first.exists() and second.exists():
            return os.path.samefile(first, second)
    except (OSError, RuntimeError, ValueError) as error:
        raise FixtureError("output rule: path resolution failed") from error
    return first == second


def validate_json_out_path(fixture: Path, json_out: Path | None) -> Path | None:
    """Validate an optional output path before opening the fixture.

    Args:
        fixture: Proposed input fixture path.
        json_out: Proposed JSON output path, if any.

    Returns:
        Resolved output path, or ``None`` when JSON output was not requested.

    Raises:
        FixtureError: If the path is in the repository, destructive, or unusable.
    """
    if json_out is None:
        return None
    try:
        resolved_output = json_out.expanduser().resolve()
        fixture_path = fixture.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise FixtureError("output rule: path resolution failed") from error
    if resolved_output.is_relative_to(_REPO_ROOT):
        raise FixtureError("output rule: json-out must be outside the repository")
    if _same_file_or_path(resolved_output, fixture_path):
        raise FixtureError("output rule: json-out must not be the fixture")
    if not resolved_output.parent.is_dir():
        raise FixtureError("output rule: json-out parent must be an existing directory")
    return resolved_output


def build_report(
    fixture: ParsedFixture,
    *,
    target_drop_temp_c: float,
    target_development_percent: float,
    config: ControllerConfig,
    development_percent_min: float,
    development_percent_max: float,
) -> ValidationReport:
    """Replay selected fixture telemetry through the authoritative planner.

    Args:
        fixture: Strictly parsed local fixture.
        target_drop_temp_c: Explicit profile temperature target, °C.
        target_development_percent: Explicit profile DTR target, percent.
        config: Committed controller configuration.
        development_percent_min: Config-derived DTR window lower bound.
        development_percent_max: Config-derived DTR window upper bound.

    Returns:
        A complete, data-free report.

    Raises:
        FixtureError: If no first-crack-through-drop telemetry sample exists.
    """
    drop_index = min(
        range(len(fixture.telemetry)),
        key=lambda index: abs(fixture.telemetry[index].monotonic_seconds - fixture.drop_seconds),
    )
    selected = [
        (index, row)
        for index, row in enumerate(fixture.telemetry)
        if index <= drop_index and row.monotonic_seconds >= fixture.first_crack_seconds
    ]
    if not selected:
        raise FixtureError("fixture rule: no telemetry rows from first crack through drop")

    planner_config = config.joint_window_planner
    rows: list[ReportRow] = []
    ror_values = _ror_series(fixture.telemetry)
    for index, telemetry_row in selected:
        charge_s = telemetry_row.monotonic_seconds - fixture.t0_seconds
        dev_s = telemetry_row.monotonic_seconds - fixture.first_crack_seconds
        dtr_pct = dev_s / charge_s * 100.0 if charge_s > 0.0 else None
        ror_c_min = ror_values[index]
        plan = plan_joint_window(
            JointWindowInputs(
                bean_temp_c=telemetry_row.bean_temp_c,
                bean_ror_c_per_min=ror_c_min,
                charge_elapsed_seconds=charge_s,
                development_elapsed_seconds=dev_s,
                target_drop_temp_c=target_drop_temp_c,
                ceiling_temp_c=config.post_first_crack_control.ceiling_guard_temp_c,
                development_percent_min=development_percent_min,
                development_percent_max=development_percent_max,
            ),
            temp_margin_c=planner_config.temp_margin_c,
            closing_horizon_seconds=planner_config.closing_horizon_seconds,
        )
        if plan is None:
            rows.append(
                ReportRow(
                    charge_s,
                    dev_s,
                    dtr_pct,
                    telemetry_row.bean_temp_c,
                    ror_c_min,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            )
            continue
        rows.append(
            ReportRow(
                charge_s,
                dev_s,
                dtr_pct,
                telemetry_row.bean_temp_c,
                ror_c_min,
                plan.status,
                plan.effective_target_temp_c,
                plan.window_open_runway_seconds,
                plan.window_close_runway_seconds,
                plan.projected_temp_at_open_c,
                plan.projected_temp_at_close_c,
            )
        )
    return ValidationReport(
        target_drop_temp_c=target_drop_temp_c,
        target_development_percent=target_development_percent,
        development_percent_min=development_percent_min,
        development_percent_max=development_percent_max,
        ceiling_temp_c=config.post_first_crack_control.ceiling_guard_temp_c,
        temp_margin_c=planner_config.temp_margin_c,
        closing_horizon_seconds=planner_config.closing_horizon_seconds,
        rows=rows,
        limitations=_LIMITATIONS,
    )


def _summary(report: ValidationReport) -> dict[str, object]:
    """Build the report summary in its fixed public order.

    Args:
        report: Complete planner report.

    Returns:
        JSON-safe summary values.
    """
    counts = Counter(row.status for row in report.rows if row.status is not None)
    first_temp_short = next(
        (row for row in report.rows if row.status is JointWindowStatus.TEMP_SHORT), None
    )
    first_summary: dict[str, float | None] | None = None
    before_window_close: bool | None = None
    if first_temp_short is not None:
        first_summary = {
            "charge_s": first_temp_short.charge_s,
            "dev_s": first_temp_short.dev_s,
            "dtr_pct": first_temp_short.dtr_pct,
        }
        before_window_close = cast(float, first_temp_short.window_close_runway_seconds) > 0.0
    return {
        "row_count": len(report.rows),
        "absent_row_count": sum(row.status is None for row in report.rows),
        "status_counts": {status.value: counts[status] for status in JointWindowStatus},
        "window_open_dtr_percent": report.development_percent_min,
        "window_close_dtr_percent": report.development_percent_max,
        "first_temp_short": first_summary,
        "first_temp_short_before_window_close": before_window_close,
    }


def report_to_json(report: ValidationReport) -> dict[str, object]:
    """Return the validator's privacy-safe JSON payload.

    Args:
        report: Complete planner report.

    Returns:
        Structured output suitable for strict JSON serialization.
    """
    payload: dict[str, object] = {
        "schema": "joint-window-validate/1",
        "parameters": {
            "target_drop_temp_c": report.target_drop_temp_c,
            "target_development_percent": report.target_development_percent,
            "development_percent_min": report.development_percent_min,
            "development_percent_max": report.development_percent_max,
            "ceiling_temp_c": report.ceiling_temp_c,
            "temp_margin_c": report.temp_margin_c,
            "closing_horizon_seconds": report.closing_horizon_seconds,
            "ror_window_seconds": _ROR_WINDOW_SECONDS,
        },
        "rows": [
            {
                "charge_s": row.charge_s,
                "dev_s": row.dev_s,
                "dtr_pct": row.dtr_pct,
                "bean_c": row.bean_c,
                "ror_c_min": row.ror_c_min,
                "status": row.status.value if row.status is not None else None,
                "effective_target_temp_c": row.effective_target_temp_c,
                "window_open_runway_seconds": row.window_open_runway_seconds,
                "window_close_runway_seconds": row.window_close_runway_seconds,
                "projected_temp_at_open_c": row.projected_temp_at_open_c,
                "projected_temp_at_close_c": row.projected_temp_at_close_c,
            }
            for row in report.rows
        ],
        "summary": _summary(report),
        "limitations": list(report.limitations),
    }
    json.dumps(payload, allow_nan=False)
    return payload


def _format_number(value: float | None) -> str:
    """Format one report number without leaking an absent value as zero.

    Args:
        value: Numeric report value, or ``None`` when absent.

    Returns:
        Stable three-decimal text or ``-``.
    """
    if value is None:
        return "-"
    return f"{0.0 if value == 0.0 else value:.3f}"


def render_report(report: ValidationReport) -> str:
    """Render the deterministic data-free table and summary.

    Args:
        report: Complete planner report.

    Returns:
        Plain-text output safe to share without fixture identity.
    """
    lines = [
        (
            "charge_s  dev_s  dtr_pct  bean_c  ror_c_min  status  open_s  close_s  "
            "proj_open_c  proj_close_c"
        )
    ]
    for row in report.rows:
        lines.append(
            "  ".join(
                (
                    _format_number(row.charge_s),
                    _format_number(row.dev_s),
                    _format_number(row.dtr_pct),
                    _format_number(row.bean_c),
                    _format_number(row.ror_c_min),
                    row.status.value if row.status is not None else "absent",
                    _format_number(row.window_open_runway_seconds),
                    _format_number(row.window_close_runway_seconds),
                    _format_number(row.projected_temp_at_open_c),
                    _format_number(row.projected_temp_at_close_c),
                )
            )
        )
    summary = _summary(report)
    first_temp_short = cast("dict[str, float | None] | None", summary["first_temp_short"])
    first_text = "none"
    if first_temp_short is not None:
        first_text = " ".join(
            f"{name}={_format_number(value)}" for name, value in first_temp_short.items()
        )
    before_close = summary["first_temp_short_before_window_close"]
    before_text = "n/a" if before_close is None else str(before_close).lower()
    counts = cast("dict[str, int]", summary["status_counts"])
    lines.extend(
        (
            "summary",
            f"row_count: {summary['row_count']}",
            f"absent_row_count: {summary['absent_row_count']}",
            "status_counts: "
            + " ".join(f"{status.value}={counts[status.value]}" for status in JointWindowStatus),
            f"window_open_dtr_percent: {_format_number(report.development_percent_min)}",
            f"window_close_dtr_percent: {_format_number(report.development_percent_max)}",
            f"first_temp_short: {first_text}",
            f"first_temp_short_before_window_close: {before_text}",
            "limitations:",
            *(f"- {limitation}" for limitation in report.limitations),
        )
    )
    return "\n".join(lines)


def _parser() -> _Parser:
    """Create the validator's closed command-line grammar.

    Returns:
        A parser with only the authorised input and output options.
    """
    parser = _Parser(add_help=False)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--target-drop-temp-c", type=float, required=True)
    parser.add_argument("--target-development-percent", type=float, required=True)
    parser.add_argument("--json-out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the validator and map every expected refusal to exit code two.

    Args:
        argv: Optional arguments excluding the program name.

    Returns:
        ``0`` for a produced report and ``2`` for a data-free refusal.
    """
    try:
        args = _parser().parse_args(argv)
        fixture = cast("Path", args.fixture)
        json_out = cast("Path | None", args.json_out)
        target_drop_temp_c = cast(float, args.target_drop_temp_c)
        target_development_percent = cast(float, args.target_development_percent)
        config = _controller_config()
        development_percent_min, development_percent_max = _validate_targets(
            target_drop_temp_c, target_development_percent, config
        )
        validated_json_out = validate_json_out_path(fixture, json_out)
        report = build_report(
            read_fixture(fixture),
            target_drop_temp_c=target_drop_temp_c,
            target_development_percent=target_development_percent,
            config=config,
            development_percent_min=development_percent_min,
            development_percent_max=development_percent_max,
        )
        rendered = render_report(report)
        print(rendered)
        if validated_json_out is not None:
            try:
                validated_json_out.write_text(
                    json.dumps(report_to_json(report), indent=2, allow_nan=False), encoding="utf-8"
                )
            except (OSError, ValueError) as error:
                raise FixtureError("output rule: could not write JSON report") from error
        return 0
    except FixtureError as error:
        print(f"joint-window-validate: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint guard
    raise SystemExit(main())
