"""Synthetic behavioural and grammar tests for the offline joint-window validator."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import joint_window_validate as validator
import pytest

from roastpilot_agent.config import ControllerConfig, JointWindowPlanner
from roastpilot_agent.models import JointWindowStatus
from roastpilot_agent.post_fc_control import JointWindowInputs, plan_joint_window

_REPO_ROOT = Path(validator.__file__).resolve().parents[1]


def _write_fixture(
    tmp_path: Path,
    telemetry: list[tuple[float, float]],
    *,
    t0: float = 0.0,
    fc: float = 120.0,
    drop: float = 208.0,
    extra_events: list[dict[str, object]] | None = None,
    rows: Sequence[object] | None = None,
    name: str = "roast.jsonl",
) -> Path:
    """Write an obviously synthetic JSONL fixture entirely below ``tmp_path``."""
    fixture = tmp_path / name
    payload: list[object] = [
        {"type": "event", "kind": "beans_added", "monotonic_seconds": t0},
        {"type": "event", "kind": "first_crack_detected", "monotonic_seconds": fc},
        *(
            {"type": "telemetry", "monotonic_seconds": mono, "bean_temp_c": bean}
            for mono, bean in telemetry
        ),
        {"type": "event", "kind": "beans_dropped", "monotonic_seconds": drop},
    ]
    if extra_events:
        payload.extend(extra_events)
    if rows is not None:
        payload = list(rows)
    fixture.write_text("\n".join(json.dumps(row) for row in payload) + "\n", encoding="utf-8")
    return fixture


def _args(fixture: Path, *extra: str) -> list[str]:
    """Return ordinary explicit-target CLI arguments for one synthetic fixture."""
    return [
        "--fixture",
        str(fixture),
        "--target-drop-temp-c",
        "195",
        "--target-development-percent",
        "13",
        *extra,
    ]


def _run(fixture: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, str, str]:
    """Run the CLI on a synthetic fixture and return its captured result."""
    code = validator.main(_args(fixture, *extra))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _report_for_rows(rows: list[validator.ReportRow]) -> validator.ValidationReport:
    """Build a direct, data-free report for rendering and sink tests."""
    return validator.ValidationReport(
        target_drop_temp_c=195.0,
        target_development_percent=13.0,
        development_percent_min=10.0,
        development_percent_max=16.0,
        ceiling_temp_c=196.0,
        temp_margin_c=3.0,
        closing_horizon_seconds=30.0,
        rows=rows,
        limitations=(
            "Offline RoR is a recorded-series estimate, not the live smoothed input.",
            (
                "Statuses say what the planner would have said, not that another action would "
                "improve a roast."
            ),
        ),
    )


def _summary(report: validator.ValidationReport) -> dict[str, object]:
    """Return the public JSON summary for a direct synthetic report."""
    return cast("dict[str, object]", validator.report_to_json(report)["summary"])


def test_tail_selection_is_inclusive_contiguous_and_charge_rebased(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T31/T32: nearest drop row includes no cooling-tail telemetry."""
    telemetry = [
        (60.0, 150.0),
        (120.0, 160.0),
        (180.0, 170.0),
        (210.0, 180.0),
        (240.0, 9.123),
    ]
    fixture = _write_fixture(tmp_path, telemetry)
    output = tmp_path / "validator.json"
    code, stdout, _ = _run(fixture, capsys, "--json-out", str(output))
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0
    assert [row["bean_c"] for row in payload["rows"]] == [160.0, 170.0, 180.0]
    assert [row["charge_s"] for row in payload["rows"]] == [120.0, 180.0, 210.0]
    assert "9.123" not in stdout
    assert payload["summary"]["row_count"] == 3


def test_first_crack_lower_boundary_is_inclusive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T57: pre-first-crack rows are excluded and equality is retained."""
    fixture = _write_fixture(tmp_path, [(60.0, 140.0), (120.0, 160.0), (180.0, 170.0)])
    output = tmp_path / "rows.json"
    code, _, _ = _run(fixture, capsys, "--json-out", str(output))
    assert code == 0
    assert [row["charge_s"] for row in json.loads(output.read_text())["rows"]] == [120.0, 180.0]


@pytest.mark.parametrize(
    ("drop", "expected"),
    [(206.0, 210.0), (194.0, 180.0), (195.0, 180.0)],
)
def test_drop_anchor_is_nearest_and_earliest_on_tie(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], drop: float, expected: float
) -> None:
    """T58: event-first nearest-row selection handles both orientations and ties."""
    fixture = _write_fixture(
        tmp_path,
        [(120.0, 160.0), (180.0, 170.0), (210.0, 180.0)],
        drop=drop,
    )
    output = tmp_path / "anchor.json"
    code, _, _ = _run(fixture, capsys, "--json-out", str(output))
    assert code == 0
    assert json.loads(output.read_text())["rows"][-1]["charge_s"] == expected


def test_anchor_parity_with_bakeoff_loader(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T59: both consumers identify the same nearest synthetic drop row."""
    from bakeoff_replay import load_roast

    fixture = _write_fixture(tmp_path, [(120.0, 160.0), (180.0, 170.0), (210.0, 180.0)], drop=206.0)
    output = tmp_path / "parity.json"
    code, _, _ = _run(fixture, capsys, "--json-out", str(output))
    telemetry, ground = load_roast(fixture)
    expected = min(
        telemetry,
        key=lambda row: abs(float(row["monotonic_seconds"]) - ground.drop_seconds),
    )
    assert code == 0
    assert json.loads(output.read_text())["rows"][-1]["bean_c"] == expected["bean_temp_c"]


def test_temp_short_summary_and_zero_close_boundary() -> None:
    """T33/G41: the evidence field is planner-runway based and strictly positive."""
    short = validator.ReportRow(
        200.0,
        80.0,
        40.0,
        180.0,
        0.0,
        JointWindowStatus.TEMP_SHORT,
        195.0,
        1.0,
        2.0,
        180.0,
        180.0,
    )
    boundary = validator.ReportRow(
        210.0,
        90.0,
        42.857,
        180.0,
        0.0,
        JointWindowStatus.TEMP_SHORT,
        195.0,
        0.0,
        0.0,
        180.0,
        180.0,
    )
    assert _summary(_report_for_rows([short]))["first_temp_short_before_window_close"] is True
    summary = _summary(_report_for_rows([boundary]))
    assert summary["first_temp_short_before_window_close"] is False
    assert summary["first_temp_short"] == {"charge_s": 210.0, "dev_s": 90.0, "dtr_pct": 42.857}


@pytest.mark.parametrize(
    "omitted",
    ["--target-drop-temp-c", "--target-development-percent"],
)
def test_required_targets_fail_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], omitted: str
) -> None:
    """T34/G19: targets are always explicit and never fixture-derived."""
    fixture = _write_fixture(tmp_path, [(120.0, 160.0), (180.0, 170.0)])
    args = _args(fixture)
    position = args.index(omitted)
    del args[position : position + 2]
    assert validator.main(args) == 2
    captured = capsys.readouterr()
    assert "target" in captured.err
    assert captured.out == ""


def test_output_never_leaks_fixture_identity_or_absolute_clock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T35/G36: path and fixture markers cannot reach either output surface."""
    marked_dir = tmp_path / "PARENT_MARKER"
    marked_dir.mkdir()
    fixture = _write_fixture(
        marked_dir,
        [(987600.0, 150.0), (987660.0, 160.0), (987720.0, 170.0)],
        t0=987500.0,
        fc=987600.0,
        drop=987715.0,
        name="FIXTURE_MARKER.jsonl",
        extra_events=[
            {
                "type": "event",
                "kind": "UNKNOWN_MARKER",
                "monotonic_seconds": 987700.0,
                "run_id": "RUN_MARKER",
                "bean": "BEAN_MARKER",
                "date": "DATE_MARKER",
            }
        ],
    )
    output = tmp_path / "identity.json"
    code, stdout, stderr = _run(fixture, capsys, "--json-out", str(output))
    assert code == 0
    all_output = stdout + stderr + output.read_text(encoding="utf-8")
    markers = (
        "PARENT_MARKER",
        "FIXTURE_MARKER",
        "RUN_MARKER",
        "BEAN_MARKER",
        "DATE_MARKER",
        "987",
    )
    for marker in markers:
        assert marker not in all_output


def test_json_out_inside_repository_is_refused_via_direct_symlink_and_dotdot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T36/G31: no private-derived JSON can be written below the repository."""
    fixture = _write_fixture(tmp_path, [(120.0, 160.0), (180.0, 170.0)])
    direct = _REPO_ROOT / "private-output.json"
    link = tmp_path / "repo-link"
    link.symlink_to(_REPO_ROOT, target_is_directory=True)
    dotdot = Path(str(tmp_path / ".." / _REPO_ROOT.name / "private-output.json"))
    for output in (direct, link / "private-output.json", dotdot):
        code, _, _ = _run(fixture, capsys, "--json-out", str(output))
        assert code == 2
        assert not output.exists()


@pytest.mark.parametrize("missing_kind", ["beans_added", "first_crack_detected", "beans_dropped"])
def test_missing_required_events_are_path_free(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], missing_kind: str
) -> None:
    """T37/G23: every named boundary is mandatory."""
    rows = [
        {"type": "event", "kind": kind, "monotonic_seconds": timestamp}
        for kind, timestamp in (
            ("beans_added", 0.0),
            ("first_crack_detected", 120.0),
            ("beans_dropped", 180.0),
        )
        if kind != missing_kind
    ] + [{"type": "telemetry", "monotonic_seconds": 120.0, "bean_temp_c": 160.0}]
    fixture = _write_fixture(tmp_path, [], rows=rows, name="PRIVATE_MARKER.jsonl")
    code, _, stderr = _run(fixture, capsys)
    assert code == 2
    assert "required events" in stderr
    assert "PRIVATE_MARKER" not in stderr


def test_docstring_and_report_limitations_keep_the_acceptance_boundary() -> None:
    """T38: synthetic tests prove mechanism only, with two explicit limitations."""
    docstring = validator.__doc__ or ""
    assert "mechanism correctness only" in docstring
    assert "not recorded-fixture acceptance" in docstring
    assert "no RP-D physical-outcome claim" in docstring
    rendered = validator.render_report(_report_for_rows([]))
    assert "Offline RoR" in rendered
    assert "planner would have said" in rendered


def test_resource_bounds_and_non_regular_paths_refuse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """T60/G24: all bounded-resource guards refuse before a report is made."""
    fixture = _write_fixture(tmp_path, [(120.0, 160.0), (180.0, 170.0)])
    monkeypatch.setattr(validator, "_MAX_FIXTURE_BYTES", 10)
    assert _run(fixture, capsys)[0] == 2
    monkeypatch.setattr(validator, "_MAX_FIXTURE_BYTES", 16 * 1024 * 1024)
    monkeypatch.setattr(validator, "_MAX_LINE_BYTES", 10)
    assert _run(fixture, capsys)[0] == 2
    monkeypatch.setattr(validator, "_MAX_LINE_BYTES", 64 * 1024)
    monkeypatch.setattr(validator, "_MAX_LINES", 1)
    assert _run(fixture, capsys)[0] == 2
    assert _run(tmp_path, capsys)[0] == 2


def test_running_byte_bound_does_not_trust_the_initial_stat(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The streaming byte counter remains the enforcing bound after a stale stat."""
    fixture = _write_fixture(tmp_path, [(120.0, 160.0), (180.0, 170.0)])
    original_stat = Path.stat

    def stale_size(path: Path) -> object:
        """Report the real mode but a deliberately stale small size."""
        result = original_stat(path)
        return type("Stat", (), {"st_mode": result.st_mode, "st_size": 0})()

    monkeypatch.setattr(Path, "stat", stale_size)
    monkeypatch.setattr(validator, "_MAX_FIXTURE_BYTES", 10)
    assert _run(fixture, capsys)[0] == 2


def test_invalid_committed_config_is_a_typed_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A future configuration-invariant breach cannot expose a traceback."""
    fixture = _write_fixture(tmp_path, [(120.0, 160.0), (180.0, 170.0)])

    class InvalidConfig:
        """Synthetic constructor matching the failure direction of Pydantic validation."""

        def __init__(self, **_kwargs: object) -> None:
            """Refuse the synthetic invalid configuration."""
            raise ValueError("invalid")

    monkeypatch.setattr(validator, "ControllerConfig", InvalidConfig)
    code, _, stderr = _run(fixture, capsys)
    assert code == 2
    assert "configuration rule" in stderr


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_json_constants_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], constant: str
) -> None:
    """T61/G25: Python's permissive JSON constants never become telemetry."""
    fixture = tmp_path / "constants.jsonl"
    fixture.write_text(
        '{"type":"telemetry","monotonic_seconds":120,"bean_temp_c":' + constant + "}\n",
        encoding="utf-8",
    )
    code, _, stderr = _run(fixture, capsys)
    assert code == 2
    assert "line 1" in stderr


def test_decreasing_telemetry_clock_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T62/G26: recorded order is never sorted or repaired."""
    fixture = _write_fixture(tmp_path, [(120.0, 160.0), (110.0, 161.0), (180.0, 170.0)])
    code, _, stderr = _run(fixture, capsys)
    assert code == 2
    assert "non-decreasing" in stderr


@pytest.mark.parametrize(
    "bad_row",
    [
        {"monotonic_seconds": 120.0, "bean_temp_c": 160.0},
        {"type": 1, "monotonic_seconds": 120.0, "bean_temp_c": 160.0},
        {"type": "other", "monotonic_seconds": 120.0, "bean_temp_c": 160.0},
        [],
        {"type": "telemetry", "monotonic_seconds": True, "bean_temp_c": 160.0},
        {"type": "telemetry", "monotonic_seconds": 120.0, "bean_temp_c": False},
    ],
)
def test_type_and_number_grammar_is_strict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], bad_row: object
) -> None:
    """T63/G27: unknown types, non-objects, and bool numbers fail closed."""
    fixture = _write_fixture(tmp_path, [], rows=[bad_row])
    assert _run(fixture, capsys)[0] == 2


def test_unknown_event_kind_and_telemetry_columns_are_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T63: harmless name-keyed additions leave the report unchanged."""
    telemetry = [(120.0, 160.0), (180.0, 170.0)]
    plain = _write_fixture(tmp_path, telemetry, name="plain.jsonl")
    extra_rows = [
        {"type": "event", "kind": "beans_added", "monotonic_seconds": 0.0},
        {"type": "event", "kind": "first_crack_detected", "monotonic_seconds": 120.0},
        {"type": "telemetry", "monotonic_seconds": 120.0, "bean_temp_c": 160.0, "extra": "ignored"},
        {"type": "telemetry", "monotonic_seconds": 180.0, "bean_temp_c": 170.0, "extra": "ignored"},
        {"type": "event", "kind": "other", "monotonic_seconds": 150.0, "extra": "ignored"},
        {"type": "event", "kind": "beans_dropped", "monotonic_seconds": 180.0},
    ]
    extra = _write_fixture(tmp_path, [], rows=extra_rows, name="extra.jsonl")
    assert _run(plain, capsys)[0] == 0
    plain_output = capsys.readouterr().out
    assert _run(extra, capsys)[0] == 0
    assert capsys.readouterr().out == plain_output


@pytest.mark.parametrize("kind", ["beans_dropped", "first_crack_detected"])
def test_duplicate_required_event_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    """T64/G28: required boundary duplication is ambiguous."""
    fixture = _write_fixture(
        tmp_path,
        [(120.0, 160.0), (180.0, 170.0)],
        extra_events=[{"type": "event", "kind": kind, "monotonic_seconds": 180.0}],
    )
    code, _, stderr = _run(fixture, capsys)
    assert code == 2
    assert "duplicate" in stderr


@pytest.mark.parametrize("t0,fc,drop", [(100.0, 90.0, 180.0), (0.0, 120.0, 110.0)])
def test_inverted_marks_refuse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], t0: float, fc: float, drop: float
) -> None:
    """T65/G29: development clocks never begin before their named boundaries."""
    fixture = _write_fixture(tmp_path, [(120.0, 160.0), (180.0, 170.0)], t0=t0, fc=fc, drop=drop)
    assert _run(fixture, capsys)[0] == 2


def test_blank_lines_are_skipped_and_faults_keep_physical_line_number(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T66: whitespace does not alter rows or obscure a later bad line."""
    fixture = tmp_path / "blank.jsonl"
    fixture.write_text("\n \t\n[]\n", encoding="utf-8")
    code, _, stderr = _run(fixture, capsys)
    assert code == 2
    assert "line 3" in stderr


def test_json_out_refuses_fixture_path_and_hard_link(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T67/G32: output cannot truncate its private source through either identity."""
    fixture = _write_fixture(tmp_path, [(120.0, 160.0), (180.0, 170.0)])
    original = fixture.read_bytes()
    assert _run(fixture, capsys, "--json-out", str(fixture))[0] == 2
    hard_link = tmp_path / "fixture-link.json"
    os.link(fixture, hard_link)
    assert _run(fixture, capsys, "--json-out", str(hard_link))[0] == 2
    assert fixture.read_bytes() == original


@pytest.mark.parametrize(
    ("temp", "dtr"),
    [
        ("0", "13"),
        ("-1", "13"),
        ("nan", "13"),
        ("195", "0"),
        ("195", "100"),
        ("195", "-1"),
        ("195", "nan"),
        ("195", "1"),
    ],
)
def test_target_ranges_refuse_before_fixture_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], temp: str, dtr: str
) -> None:
    """T68/G33: target and derived-window ranges cannot yield an all-absent report."""
    missing_fixture = tmp_path / "not-read.jsonl"
    args = [
        "--fixture",
        str(missing_fixture),
        "--target-drop-temp-c",
        temp,
        "--target-development-percent",
        dtr,
    ]
    code = validator.main(args)
    captured = capsys.readouterr()
    assert code == 2
    assert "cli rule" in captured.err
    assert captured.out == ""


def test_findings_do_not_change_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T69/G37: a missing TEMP_SHORT is report data, not a process failure."""
    no_short = _report_for_rows([])
    assert _summary(no_short)["first_temp_short"] is None
    assert _summary(no_short)["first_temp_short_before_window_close"] is None
    fixture = _write_fixture(tmp_path, [(60.0, 195.0), (120.0, 195.0), (180.0, 195.0)])
    assert _run(fixture, capsys)[0] == 0


def test_invalid_utf8_refuses(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """T70/G38: malformed bytes never become replacement characters."""
    fixture = tmp_path / "invalid.jsonl"
    fixture.write_bytes(b"\xff\n")
    code, _, stderr = _run(fixture, capsys)
    assert code == 2
    assert "UTF-8" in stderr


@pytest.mark.parametrize(
    "content",
    [
        "{not-json}\n",
        '{"type":"telemetry","monotonic_seconds":120,"bean_temp_c":1e999}\n',
    ],
)
def test_malformed_and_overflow_json_values_refuse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str
) -> None:
    """Additional parser failures remain typed and line-numbered."""
    fixture = tmp_path / "bad.jsonl"
    fixture.write_text(content, encoding="utf-8")
    code, _, stderr = _run(fixture, capsys)
    assert code == 2
    assert "line 1" in stderr


def test_missing_file_no_telemetry_no_selection_and_bad_event_kind_refuse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Structural parse guards have no uncaught or ambiguous branch."""
    assert _run(tmp_path / "missing.jsonl", capsys)[0] == 2
    events_only = _write_fixture(
        tmp_path,
        [],
        rows=[
            {"type": "event", "kind": "beans_added", "monotonic_seconds": 0.0},
            {"type": "event", "kind": "first_crack_detected", "monotonic_seconds": 120.0},
            {"type": "event", "kind": "beans_dropped", "monotonic_seconds": 180.0},
        ],
        name="events-only.jsonl",
    )
    assert _run(events_only, capsys)[0] == 2
    no_selection = _write_fixture(
        tmp_path,
        [(120.0, 160.0)],
        fc=200.0,
        drop=200.0,
        name="no-selection.jsonl",
    )
    assert _run(no_selection, capsys)[0] == 2
    bad_kind = _write_fixture(
        tmp_path,
        [],
        rows=[{"type": "event", "kind": 1, "monotonic_seconds": 0.0}],
        name="bad-kind.jsonl",
    )
    assert _run(bad_kind, capsys)[0] == 2


def test_json_write_error_is_a_typed_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filesystem write failure cannot produce a traceback."""
    fixture = _write_fixture(tmp_path, [(120.0, 160.0), (180.0, 170.0)])
    output = tmp_path / "output.json"

    def fail_write(_self: Path, _data: str, *, encoding: str) -> None:
        """Raise the synthetic filesystem failure after all path validation."""
        raise OSError("synthetic write failure")

    monkeypatch.setattr(Path, "write_text", fail_write)
    code, _, stderr = _run(fixture, capsys, "--json-out", str(output))
    assert code == 2
    assert "could not write" in stderr


def test_fixture_open_error_is_a_typed_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-stat filesystem read error cannot bypass the typed error boundary."""
    fixture = _write_fixture(tmp_path, [(120.0, 160.0), (180.0, 170.0)])

    def fail_open(_self: Path, *_args: object, **_kwargs: object) -> object:
        """Raise the synthetic TOCTOU-style read failure."""
        raise OSError("synthetic open failure")

    monkeypatch.setattr(Path, "open", fail_open)
    code, _, stderr = _run(fixture, capsys)
    assert code == 2
    assert "could not read" in stderr


def test_json_sink_rejects_nonfinite_report_values() -> None:
    """T71/G39: the structured sink fails rather than producing a NaN token."""
    report = _report_for_rows([])
    nonfinite = validator.ValidationReport(
        math.nan,
        report.target_development_percent,
        report.development_percent_min,
        report.development_percent_max,
        report.ceiling_temp_c,
        report.temp_margin_c,
        report.closing_horizon_seconds,
        report.rows,
        report.limitations,
    )
    with pytest.raises(ValueError):
        validator.report_to_json(nonfinite)


def test_report_values_are_authoritative_planner_values(tmp_path: Path) -> None:
    """T72/G40: config-owned parameters and all derived fields match the planner."""
    fixture = _write_fixture(tmp_path, [(60.0, 180.0), (120.0, 181.0), (180.0, 182.0)], drop=180.0)
    parsed = validator.read_fixture(fixture)
    config = ControllerConfig(joint_window_planner=JointWindowPlanner(enabled=True))
    minimum = 13.0 - config.drop_dev_margin_percent
    maximum = 13.0 + config.drop_dev_margin_percent
    report = validator.build_report(
        parsed,
        target_drop_temp_c=195.0,
        target_development_percent=13.0,
        config=config,
        development_percent_min=minimum,
        development_percent_max=maximum,
    )
    row = report.rows[-1]
    plan = plan_joint_window(
        JointWindowInputs(
            182.0,
            1.0,
            180.0,
            60.0,
            195.0,
            config.post_first_crack_control.ceiling_guard_temp_c,
            minimum,
            maximum,
        ),
        temp_margin_c=config.joint_window_planner.temp_margin_c,
        closing_horizon_seconds=config.joint_window_planner.closing_horizon_seconds,
    )
    assert plan is not None
    assert row.status is plan.status
    assert row.effective_target_temp_c == plan.effective_target_temp_c
    assert row.window_open_runway_seconds == plan.window_open_runway_seconds
    assert row.window_close_runway_seconds == plan.window_close_runway_seconds
    assert row.projected_temp_at_open_c == plan.projected_temp_at_open_c
    assert row.projected_temp_at_close_c == plan.projected_temp_at_close_c
    assert report.temp_margin_c == config.joint_window_planner.temp_margin_c
    assert report.closing_horizon_seconds == config.joint_window_planner.closing_horizon_seconds
    assert report.ceiling_temp_c == config.post_first_crack_control.ceiling_guard_temp_c


def test_absent_rows_are_atomic_and_counted(tmp_path: Path) -> None:
    """T73/G34: unavailable RoR and a degenerate charge clock expose no plan fragments."""
    fixture = _write_fixture(
        tmp_path, [(120.0, 160.0), (180.0, 170.0)], t0=120.0, fc=120.0, drop=180.0
    )
    parsed = validator.read_fixture(fixture)
    config = ControllerConfig(joint_window_planner=JointWindowPlanner(enabled=True))
    minimum = 13.0 - config.drop_dev_margin_percent
    maximum = 13.0 + config.drop_dev_margin_percent
    report = validator.build_report(
        parsed,
        target_drop_temp_c=195.0,
        target_development_percent=13.0,
        config=config,
        development_percent_min=minimum,
        development_percent_max=maximum,
    )
    absent = report.rows[0]
    assert absent.status is None
    planner_values = (
        absent.effective_target_temp_c,
        absent.window_open_runway_seconds,
        absent.window_close_runway_seconds,
        absent.projected_temp_at_open_c,
        absent.projected_temp_at_close_c,
    )
    assert planner_values == (None, None, None, None, None)
    assert _summary(report)["absent_row_count"] == 1


@pytest.mark.parametrize("previous_temp", [180.0, 184.0])
def test_zero_and_negative_ror_reach_planner(tmp_path: Path, previous_temp: float) -> None:
    """T74/G35: non-positive RoR is valid evidence and remains classified."""
    fixture = _write_fixture(
        tmp_path,
        [(60.0, previous_temp), (120.0, 180.0), (180.0, 180.0)],
        drop=180.0,
    )
    parsed = validator.read_fixture(fixture)
    config = ControllerConfig(joint_window_planner=JointWindowPlanner(enabled=True))
    minimum = 13.0 - config.drop_dev_margin_percent
    maximum = 13.0 + config.drop_dev_margin_percent
    report = validator.build_report(
        parsed,
        target_drop_temp_c=195.0,
        target_development_percent=13.0,
        config=config,
        development_percent_min=minimum,
        development_percent_max=maximum,
    )
    assert report.rows[-1].ror_c_min is not None
    assert report.rows[-1].ror_c_min <= 0.0
    assert report.rows[-1].status is JointWindowStatus.TEMP_SHORT


def test_output_is_deterministic_and_has_all_status_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T75: repeated synthetic runs produce byte-identical public output."""
    fixture = _write_fixture(tmp_path, [(60.0, 180.0), (120.0, 181.0), (180.0, 182.0)])
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    assert _run(fixture, capsys, "--json-out", str(first))[0] == 0
    stdout_first = capsys.readouterr().out
    assert _run(fixture, capsys, "--json-out", str(second))[0] == 0
    stdout_second = capsys.readouterr().out
    assert stdout_first == stdout_second
    assert first.read_bytes() == second.read_bytes()
    assert set(json.loads(first.read_text())["summary"]["status_counts"]) == {
        status.value for status in JointWindowStatus
    }


@pytest.mark.parametrize(
    ("previous_temp", "current_temp", "fc", "expected"),
    [
        (180.0, 180.0, 900.0, JointWindowStatus.TEMP_SHORT),
        (194.0, 194.0, 860.0, JointWindowStatus.CLOSING),
        (150.0, 210.0, 990.0, JointWindowStatus.AHEAD),
        (188.0, 190.0, 900.0, JointWindowStatus.ON_TRACK),
    ],
)
def test_each_joint_window_status_is_reachable(
    tmp_path: Path,
    previous_temp: float,
    current_temp: float,
    fc: float,
    expected: JointWindowStatus,
) -> None:
    """T76: synthetic trajectories exercise every authoritative classifier state."""
    fixture = _write_fixture(
        tmp_path,
        [(940.0, previous_temp), (1000.0, current_temp)],
        t0=0.0,
        fc=fc,
        drop=1000.0,
    )
    parsed = validator.read_fixture(fixture)
    config = ControllerConfig(joint_window_planner=JointWindowPlanner(enabled=True))
    minimum = 13.0 - config.drop_dev_margin_percent
    maximum = 13.0 + config.drop_dev_margin_percent
    report = validator.build_report(
        parsed,
        target_drop_temp_c=195.0,
        target_development_percent=13.0,
        config=config,
        development_percent_min=minimum,
        development_percent_max=maximum,
    )
    assert report.rows[-1].status is expected
