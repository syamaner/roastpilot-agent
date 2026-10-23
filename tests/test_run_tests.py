"""Focused tests for the explicit test-run profile helper (``scripts/run_tests.py``).

The helper only maps closed profile/lane/worker choices to fixed native pytest argv.
These tests never collect or run the real repository suite: the one real repository
subprocess is a ``--dry-run`` that spawns no pytest, and the real pytest witness runs
a three-test toy project under ``tmp_path``.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
import run_tests
import yaml
from run_tests import Lane, LanePlan, Profile, build_plan, main, run_plan

REPO_ROOT = Path(__file__).resolve().parents[1]
IGNORE = "--ignore=tests/test_packaging.py"
FAST_SERIAL = "serial and not stress and not exhaustive_only"
EXHAUSTIVE_SERIAL = "serial and not stress"
SINGLE_LANES = (Lane.ORDINARY, Lane.SERIAL, Lane.STRESS, Lane.PACKAGING)

EXPECTED_ARGV: dict[tuple[Profile, Lane], tuple[str, ...]] = {}
for _profile in Profile:
    EXPECTED_ARGV[(_profile, Lane.ORDINARY)] = (
        "PYX",
        "-m",
        "pytest",
        "-n",
        "4",
        "--dist",
        "worksteal",
        "-m",
        "not serial and not stress",
        IGNORE,
    )
    EXPECTED_ARGV[(_profile, Lane.STRESS)] = ("PYX", "-m", "pytest", "-m", "stress", IGNORE)
    EXPECTED_ARGV[(_profile, Lane.PACKAGING)] = ("PYX", "-m", "pytest", "tests/test_packaging.py")
EXPECTED_ARGV[(Profile.CI, Lane.SERIAL)] = ("PYX", "-m", "pytest", "-m", FAST_SERIAL, IGNORE)
EXPECTED_ARGV[(Profile.LOCAL, Lane.SERIAL)] = ("PYX", "-m", "pytest", "-m", FAST_SERIAL, IGNORE)
EXPECTED_ARGV[(Profile.NIGHTLY, Lane.SERIAL)] = (
    "PYX",
    "-m",
    "pytest",
    "-m",
    EXHAUSTIVE_SERIAL,
    IGNORE,
)


class FakeRunner:
    """Record runner calls and return scripted exit codes (default 0)."""

    def __init__(self, codes: tuple[int, ...] = ()) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self._codes = list(codes)

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[None]:
        self.calls.append((args, kwargs))
        code = self._codes.pop(0) if self._codes else 0
        return subprocess.CompletedProcess(args, code)


@pytest.fixture
def default_runner(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    """Replace the helper's default ``subprocess.run`` and clear ``PYTEST_ADDOPTS``."""
    fake = FakeRunner()
    monkeypatch.setattr(run_tests.subprocess, "run", fake)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    return fake


def _lane_lines(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith("run_tests: lane ")]


@pytest.mark.parametrize("lane", SINGLE_LANES, ids=lambda lane: lane.value)
@pytest.mark.parametrize("profile", list(Profile), ids=lambda profile: profile.value)
def test_single_lane_plan_is_the_exact_fixed_argv(profile: Profile, lane: Lane) -> None:
    """T1: each profile x single lane maps to the literal argv in the contract."""
    assert build_plan(profile, lane, 4, "PYX") == (LanePlan(lane, EXPECTED_ARGV[(profile, lane)]),)


@pytest.mark.parametrize("profile", list(Profile), ids=lambda profile: profile.value)
def test_all_lane_is_the_ordered_concatenation_of_single_lanes(profile: Profile) -> None:
    """T2: ``all`` runs ordinary, serial, stress, packaging in CI job order."""
    plan = build_plan(profile, Lane.ALL, 4, "PYX")
    expected = tuple(item for lane in SINGLE_LANES for item in build_plan(profile, lane, 4, "PYX"))
    assert plan == expected
    assert [item.lane.value for item in plan] == ["ordinary", "serial", "stress", "packaging"]


@pytest.mark.parametrize("workers", [1, 4, 7])
@pytest.mark.parametrize("profile", list(Profile), ids=lambda profile: profile.value)
def test_xdist_flags_appear_only_in_the_ordinary_lane(profile: Profile, workers: int) -> None:
    """T3: serial, stress and packaging never carry xdist distribution flags."""
    for item in build_plan(profile, Lane.ALL, workers, "PYX"):
        xdist = [
            token
            for token in item.argv
            if token in {"-n", "--dist", "--numprocesses"}
            or (token.startswith("-n") and token != "-n")
            or token.startswith("--dist=")
            or token.startswith("--numprocesses=")
        ]
        if item.lane is Lane.ORDINARY:
            assert xdist == ["-n", "--dist"]
            assert item.argv[item.argv.index("-n") + 1] == str(workers)
        else:
            assert xdist == []


@pytest.mark.parametrize("profile", list(Profile), ids=lambda profile: profile.value)
def test_only_ci_and_local_serial_exclude_the_exhaustive_inventory(profile: Profile) -> None:
    """T4: ci/local serial omits ``exhaustive_only``; nightly serial includes it."""
    plan = build_plan(profile, Lane.ALL, 4, "PYX")
    serial = next(item for item in plan if item.lane is Lane.SERIAL)
    expected = EXHAUSTIVE_SERIAL if profile is Profile.NIGHTLY else FAST_SERIAL
    assert serial.argv[serial.argv.index("-m", 3) + 1] == expected
    for item in plan:
        if item.lane is not Lane.SERIAL:
            assert not any("exhaustive_only" in token for token in item.argv)


@pytest.mark.parametrize(
    ("profile", "expected_lanes"),
    [
        ("local", ["ordinary"]),
        ("ci", ["ordinary", "serial", "stress", "packaging"]),
        ("nightly", ["ordinary", "serial", "stress", "packaging"]),
    ],
)
def test_profile_default_lanes_and_workers(
    profile: str,
    expected_lanes: list[str],
    default_runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T5: local defaults to the ordinary lane; ci/nightly default to all; workers 4."""
    assert main(["--profile", profile, "--dry-run"]) == 0
    lines = _lane_lines(capsys.readouterr().out)
    assert [line.split(": ", 2)[1].removeprefix("lane ") for line in lines] == expected_lanes
    ordinary = shlex.split(lines[0].split(": ", 2)[2])
    assert ordinary[ordinary.index("-n") + 1] == "4"
    assert default_runner.calls == []


def _ci_jobs() -> dict[str, dict[str, object]]:
    loaded = cast(
        dict[str, object], yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    )
    return cast(dict[str, dict[str, object]], loaded["jobs"])


def _pytest_step(job: dict[str, object]) -> dict[str, object]:
    steps = cast(list[dict[str, object]], job["steps"])
    (step,) = [s for s in steps if str(s.get("run", "")).startswith("python -m pytest")]
    return step


def _ci_tokens(step: dict[str, object]) -> list[str]:
    tokens = shlex.split(cast(str, step["run"]))
    assert tokens[:3] == ["python", "-m", "pytest"]
    retained: list[str] = []
    index = 3
    while index < len(tokens):
        token = tokens[index]
        if token == "-p" and tokens[index + 1 : index + 2] == ["scripts.ci_timing_report"]:
            index += 2
        elif token.startswith(("--cov", "--ci-timing-report=")):
            index += 1
        else:
            retained.append(token)
            index += 1
    return retained


def test_ordinary_serial_and_packaging_selection_match_ci_workflow() -> None:
    """T6: one ``ci.yml`` read ties the helper's selections to CI's, with no collection.

    The stress lane is excluded here: the helper uses the ``-m stress`` marker form and
    ``tests/test_pytest_governance.py`` already proves that marker selection equals
    CI's three-entry stress nodeid matrix.
    """
    jobs = _ci_jobs()
    plan = {item.lane: item.argv[3:] for item in build_plan(Profile.CI, Lane.ALL, 4, "PYX")}
    nightly = {item.lane: item.argv[3:] for item in build_plan(Profile.NIGHTLY, Lane.ALL, 4, "PYX")}

    assert _ci_tokens(_pytest_step(jobs["pytest-ordinary"])) == list(plan[Lane.ORDINARY])
    assert _ci_tokens(_pytest_step(jobs["package"])) == list(plan[Lane.PACKAGING])

    serial_step = _pytest_step(jobs["pytest-serial"])
    assert _ci_tokens(serial_step) == ["-m", "$SERIAL_SELECTION", IGNORE]
    assert list(plan[Lane.SERIAL]) == ["-m", FAST_SERIAL, IGNORE]
    assert list(nightly[Lane.SERIAL]) == ["-m", EXHAUSTIVE_SERIAL, IGNORE]
    selection = cast(dict[str, str], serial_step["env"])["SERIAL_SELECTION"]
    assert f"&& '{EXHAUSTIVE_SERIAL}' || '{FAST_SERIAL}' }}}}" in selection


def test_run_plan_calls_runner_shell_free_in_plan_order(capsys: pytest.CaptureFixture[str]) -> None:
    """T7: each lane is run as a list argv, without a shell, from ``cwd``, in order."""
    plan = build_plan(Profile.CI, Lane.ALL, 4, "PYX")
    fake = FakeRunner()
    assert run_plan(plan, cwd=REPO_ROOT, runner=fake) == 0
    assert fake.calls == [
        (list(item.argv), {"cwd": REPO_ROOT, "shell": False, "check": False}) for item in plan
    ]
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"run_tests: cwd {shlex.quote(str(REPO_ROOT))}"


def test_main_runs_default_runner_from_repository_root_with_launching_interpreter(
    default_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """T7: ``main`` uses the default runner, the repository root and ``sys.executable``."""
    monkeypatch.chdir(tmp_path)
    assert main(["--profile", "nightly"]) == 0
    assert len(default_runner.calls) == 4
    for args, kwargs in default_runner.calls:
        cwd = cast(Path, kwargs["cwd"])
        assert (cwd / "pyproject.toml").is_file()
        assert (cwd / "scripts" / "run_tests.py").is_file()
        assert cwd == REPO_ROOT
        assert kwargs["shell"] is False
        assert args[0] == sys.executable


def test_run_plan_stops_at_first_failure_and_names_unrun_lanes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T8: a non-zero child stops the run; its code is returned and later lanes named."""
    fake = FakeRunner((0, 3, 0, 0))
    plan = build_plan(Profile.CI, Lane.ALL, 4, "PYX")
    assert run_plan(plan, cwd=REPO_ROOT, runner=fake) == 3
    assert len(fake.calls) == 2
    captured = capsys.readouterr()
    assert "run_tests: lane serial exited 3; not run: stress, packaging" in captured.err
    assert "all selected lanes exited 0" not in captured.out


def test_main_propagates_first_failed_child_code_and_stops(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T13: through ``main``'s default runner, a failed first lane's code is returned."""
    fake = FakeRunner((3,))
    monkeypatch.setattr(run_tests.subprocess, "run", fake)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    assert main(["--profile", "ci"]) == 3
    assert len(fake.calls) == 1
    captured = capsys.readouterr()
    assert "run_tests: lane ordinary exited 3; not run: serial, stress, packaging" in captured.err
    assert "all selected lanes exited 0" not in captured.out


def test_run_plan_maps_signal_and_reports_success_caveat(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T9: a signal-killed child maps to 128+N; success states it is not the full gate."""
    plan = build_plan(Profile.CI, Lane.PACKAGING, 4, "PYX")
    assert run_plan(plan, cwd=REPO_ROOT, runner=FakeRunner((-9,))) == 137
    assert "not run: none" in capsys.readouterr().err
    assert run_plan(plan, cwd=REPO_ROOT, runner=FakeRunner((0,))) == 0
    final = capsys.readouterr().out.splitlines()[-1]
    assert "not the mandatory single-process full gate" in final


@pytest.mark.parametrize("profile", list(Profile), ids=lambda profile: profile.value)
def test_dry_run_prints_exact_argv_and_spawns_nothing(
    profile: Profile,
    default_runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T10: dry run never calls the runner and each printed command splits to its argv."""
    assert main(["--profile", profile.value, "--lane", "all", "--dry-run"]) == 0
    assert default_runner.calls == []
    stdout = capsys.readouterr().out
    assert stdout.splitlines()[0] == f"run_tests: cwd {shlex.quote(str(REPO_ROOT))}"
    plan = build_plan(profile, Lane.ALL, 4, sys.executable)
    lines = _lane_lines(stdout)
    assert len(lines) == len(plan)
    for line, item in zip(lines, plan, strict=True):
        assert shlex.split(line.split(": ", 2)[2]) == list(item.argv)


def test_help_states_the_coverage_neutral_not_full_gate_caveat(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--help`` carries the coverage-neutral, not-full-gate caveat."""
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "coverage-neutral" in help_text
    assert "never replaces the mandatory single-process, all-marker" in help_text


def _usage_failure(argv: list[str], runner: FakeRunner, capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 2
    assert runner.calls == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err
    return captured.err


@pytest.mark.parametrize(
    "argv",
    [["--profile", "bogus"], ["--profile", "ci", "--lane", "fast"], ["--lane", "all"]],
)
def test_unknown_profile_lane_or_missing_profile_fail_before_any_child(
    argv: list[str], default_runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    """N1: profile and lane choices are closed and ``--profile`` is required."""
    _usage_failure([*argv, "--dry-run"], default_runner, capsys)
    _usage_failure(argv, default_runner, capsys)


@pytest.mark.parametrize("workers", ["0", "-1", "+4", "04", "4.0", "abc", "", " 4"])
def test_invalid_workers_fail_before_any_child(
    workers: str, default_runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    """N2: workers must be a plain positive decimal integer."""
    err = _usage_failure(["--profile", "local", "--workers", workers], default_runner, capsys)
    assert "--workers" in err
    _usage_failure(["--profile", "local", f"--workers={workers}"], default_runner, capsys)


@pytest.mark.parametrize(
    "argv",
    [
        ["--profile", "ci", "--profile", "nightly"],
        ["--profile", "ci", "--profile", "ci"],
        ["--profile", "ci", "--lane", "serial", "--lane", "serial"],
        ["--profile", "ci", "--workers", "2", "--workers", "2"],
        ["--profile", "ci", "--dry-run", "--dry-run"],
        ["--prof", "ci"],
        ["--profile", "ci", "extra"],
        ["--profile", "ci", "--", "-k", "x"],
        ["--profile", "ci", "--"],
    ],
)
def test_repeated_abbreviated_positional_or_passthrough_fail_before_any_child(
    argv: list[str], default_runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    """N3: each option at most once; no abbreviation, positional or ``--`` passthrough."""
    _usage_failure(argv, default_runner, capsys)


@pytest.mark.parametrize("lane", ["serial", "stress", "packaging"])
def test_explicit_workers_without_ordinary_lane_fail_before_any_child(
    lane: str, default_runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    """N4: ``--workers`` contradicts a lane set that has no ordinary lane."""
    argv = ["--profile", "ci", "--lane", lane, "--workers", "2"]
    err = _usage_failure(argv, default_runner, capsys)
    assert "--workers applies only to the ordinary lane" in err
    _usage_failure([*argv, "--dry-run"], default_runner, capsys)


def test_workers_controls_are_accepted_where_ordinary_runs(
    default_runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N4 positive controls: default workers on serial, explicit workers with ``all``.

    The interpreter path contains ``-n`` as a substring, so only an exact ``-n`` argv
    token may identify the worker count. Every call is a dry run; it is never executed.
    """
    monkeypatch.setattr(sys, "executable", "/tmp/python-native")
    assert main(["--profile", "ci", "--lane", "serial", "--dry-run"]) == 0
    assert main(["--profile", "ci", "--lane", "all", "--workers", "2", "--dry-run"]) == 0
    assert main(["--profile", "local", "--workers=7", "--dry-run"]) == 0
    commands = [
        shlex.split(line.split(": ", 2)[2]) for line in _lane_lines(capsys.readouterr().out)
    ]
    workers = [argv[argv.index("-n") + 1] for argv in commands if "-n" in argv]
    assert workers == ["2", "7"]
    assert default_runner.calls == []


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "execute"])
def test_non_empty_pytest_addopts_fails_before_any_child(
    dry_run: bool,
    default_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """N5: a non-empty ``PYTEST_ADDOPTS`` would change every child, so it is rejected."""
    monkeypatch.setenv("PYTEST_ADDOPTS", "-n 8")
    argv = ["--profile", "ci", *(["--dry-run"] if dry_run else [])]
    assert "PYTEST_ADDOPTS" in _usage_failure(argv, default_runner, capsys)


def test_empty_pytest_addopts_is_allowed(
    default_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N5 positive control: an empty ``PYTEST_ADDOPTS`` changes nothing."""
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    assert main(["--profile", "local"]) == 0
    assert len(default_runner.calls) == 1


@pytest.mark.slow
def test_real_script_dry_run_and_usage_failure() -> None:
    """T11: the real script dry-runs through the launching interpreter; no pytest spawns."""
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_ADDOPTS"}
    script = [sys.executable, "scripts/run_tests.py", "--profile", "nightly"]
    dry = subprocess.run(
        [*script, "--dry-run"], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False
    )
    assert dry.returncode == 0, dry.stderr
    lines = _lane_lines(dry.stdout)
    assert len(lines) == 4
    assert all(shlex.split(line.split(": ", 2)[2])[0] == sys.executable for line in lines)
    invalid = subprocess.run(
        [*script, "--workers", "0", "--dry-run"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode == 2
    assert "lane" not in invalid.stdout


@pytest.mark.slow
def test_real_pytest_child_keeps_skips_and_failures_visible_and_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """T12: a real pytest child's skip and failure stay visible; later lanes do not run.

    Colour is forced on in the inherited environment, as a valid caller may do; only
    the toy child's own ``--color=no`` keeps its summary text assertable.
    """
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.setenv("PY_COLORS", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "test_toy.py").write_text(
        "import pytest\n\n"
        "def test_passes():\n    pass\n\n"
        "def test_skips():\n    pytest.skip('toy skip reason')\n\n"
        "def test_fails():\n    assert False, 'toy failure'\n"
    )
    plan = (
        LanePlan(
            Lane.ORDINARY,
            (
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "--color=no",
                "-rs",
                "test_toy.py",
            ),
        ),
        LanePlan(Lane.SERIAL, (sys.executable, "-c", "raise SystemExit(0)")),
    )
    assert run_plan(plan, cwd=tmp_path) == 1
    captured = capfd.readouterr()
    assert "SKIPPED" in captured.out
    assert "toy skip reason" in captured.out
    assert "1 failed, 1 passed, 1 skipped" in captured.out
    assert "run_tests: lane ordinary exited 1; not run: serial" in captured.err
    assert "run_tests: lane serial" not in captured.out
