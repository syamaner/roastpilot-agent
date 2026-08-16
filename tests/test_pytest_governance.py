"""Fail-closed governance tests for the Python test-suite markers."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pytest_options() -> dict[str, object]:
    """Load the Pytest configuration from the project metadata."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as config_file:
        config = cast(dict[str, object], tomllib.load(config_file))
    tool = cast(dict[str, object], config["tool"])
    pytest_config = cast(dict[str, object], tool["pytest"])
    return cast(dict[str, object], pytest_config["ini_options"])


def _mapping(value: object) -> dict[str, object]:
    """Narrow an untyped YAML object to a string-keyed mapping."""
    assert isinstance(value, dict)
    mapping = cast(dict[object, object], value)
    assert all(isinstance(key, str) for key in mapping)
    return {cast(str, key): item for key, item in mapping.items()}


def _workflow() -> dict[str, object]:
    """Load the CI workflow as a structural mapping."""
    loaded = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    assert isinstance(loaded, dict)
    raw_workflow = cast(dict[object, object], loaded)
    normalized = {
        "on" if key is True else cast(str, key): value for key, value in raw_workflow.items()
    }
    return _mapping(normalized)


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    """Return the workflow steps for one job."""
    steps = job["steps"]
    assert isinstance(steps, list)
    return [_mapping(step) for step in cast(list[object], steps)]


def _pytest_lanes(workflow: dict[str, object]) -> list[tuple[str, list[str], str]]:
    """Expand each governed pytest lane into its test arguments and coverage file."""
    jobs = _mapping(workflow["jobs"])
    lanes: list[tuple[str, list[str], str]] = []
    for job_id in ("pytest-ordinary", "pytest-serial", "package", "pytest-stress"):
        job = _mapping(jobs[job_id])
        matrix_entries: list[dict[str, object]] = [{}]
        if job_id == "pytest-stress":
            strategy = _mapping(job["strategy"])
            matrix = _mapping(strategy["matrix"])
            include = matrix["include"]
            assert isinstance(include, list)
            matrix_entries = [_mapping(entry) for entry in cast(list[object], include)]
        for step in _steps(job):
            run = step.get("run")
            if not isinstance(run, str) or not run.startswith("python -m pytest"):
                continue
            env = _mapping(step["env"])
            coverage_file = env["COVERAGE_FILE"]
            assert isinstance(coverage_file, str)
            for matrix_entry in matrix_entries:
                command = run
                resolved_coverage_file = coverage_file
                for key, value in matrix_entry.items():
                    assert isinstance(value, str)
                    command = command.replace(f"${{{{ matrix.{key} }}}}", value)
                    resolved_coverage_file = resolved_coverage_file.replace(
                        f"${{{{ matrix.{key} }}}}", value
                    )
                arguments = shlex.split(command)
                assert arguments[:3] == ["python", "-m", "pytest"]
                lanes.append((job_id, arguments[3:], resolved_coverage_file))
    return lanes


def _collection_arguments(arguments: list[str]) -> list[str]:
    """Remove execution and coverage options before a lane collection probe."""
    retained: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"-n", "--dist"}:
            index += 2
        elif argument.startswith("--cov=") or argument.startswith("--cov-report="):
            index += 1
        else:
            retained.append(argument)
            index += 1
    return retained


def _collect_nodeids(arguments: list[str]) -> tuple[int, set[str], str]:
    """Collect one lane and return its status, node IDs, and diagnostic output."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "--override-ini",
            "addopts=",
            *_collection_arguments(arguments),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    nodeids = {
        line.strip()
        for line in result.stdout.splitlines()
        if re.fullmatch(r"tests/.*::.*", line.strip())
    }
    return result.returncode, nodeids, result.stdout + result.stderr


def test_pytest_markers_are_registered_and_full_gate_is_unfiltered() -> None:
    """Markers are documented, strict, and never deselected from the full gate."""
    options = _pytest_options()
    addopts = cast(str, options["addopts"])
    markers = cast(list[str], options["markers"])
    registered = {marker.partition(":")[0]: marker.partition(":")[2].strip() for marker in markers}

    assert {"slow", "stress", "serial"} <= registered.keys()
    assert all(registered[marker] for marker in ("slow", "stress", "serial"))
    assert "--strict-markers" in addopts.split()
    assert "-m" not in addopts.split()

    assert "-n" not in addopts.split()
    assert "--dist" not in addopts.split()

    with (REPO_ROOT / "pyproject.toml").open("rb") as config_file:
        project = cast(dict[str, object], tomllib.load(config_file))
    coverage = _mapping(_mapping(project["tool"])["coverage"])
    coverage_run = _mapping(coverage["run"])
    assert coverage_run["relative_files"] is True
    assert coverage_run["branch"] is True


def test_stress_collection_selects_real_parser_boundaries() -> None:
    """The stress selection contains both real opaque parser boundary tests."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-vv",
            "-p",
            "no:cacheprovider",
            "-m",
            "stress",
            "tests/test_capture_agent_usage.py",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "test_codex_opaque_event_boundary_is_exact" in result.stdout
    assert "test_codex_opaque_total_boundary_accepts_exact_limit" in result.stdout
    assert "test_codex_opaque_total_boundary_rejects_one_over" in result.stdout


def test_ci_lane_selections_partition_the_full_collection() -> None:
    """Every collected test belongs to exactly one explicit CI lane."""
    workflow = _workflow()
    by_job: dict[str, set[str]] = {}
    for job_id, arguments, _ in _pytest_lanes(workflow):
        returncode, nodeids, output = _collect_nodeids(arguments)
        assert returncode == 0, output
        by_job.setdefault(job_id, set()).update(nodeids)

    full_returncode, full, full_output = _collect_nodeids([])
    stress_returncode, stress, stress_output = _collect_nodeids(["-m", "stress"])
    assert full_returncode == 0, full_output
    assert stress_returncode == 0, stress_output
    assert set(by_job) == {"pytest-ordinary", "pytest-serial", "pytest-stress", "package"}
    assert all(by_job.values())

    lane_sets = list(by_job.values())
    for index, current in enumerate(lane_sets):
        for other in lane_sets[index + 1 :]:
            assert current.isdisjoint(other)
    assert set().union(*lane_sets) == full
    assert by_job["pytest-stress"] == stress

    package_nodeids = {nodeid for nodeid in full if nodeid.startswith("tests/test_packaging.py::")}
    assert by_job["package"] == package_nodeids
    assert package_nodeids.isdisjoint(by_job["pytest-ordinary"])
    assert package_nodeids.isdisjoint(by_job["pytest-serial"])
    assert full and len(full) > 4_000


def test_ci_lanes_are_bounded_parallel_and_write_unique_coverage_files() -> None:
    """CI lanes have fixed xdist bounds and preserve all coverage data files."""
    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    lanes = _pytest_lanes(workflow)
    ordinary_arguments = next(arguments for job, arguments, _ in lanes if job == "pytest-ordinary")
    assert ordinary_arguments.count("-n") == 1
    assert ordinary_arguments[ordinary_arguments.index("-n") + 1] == "4"
    assert ordinary_arguments[ordinary_arguments.index("--dist") + 1] == "worksteal"
    for job_id, arguments, _ in lanes:
        if job_id != "pytest-ordinary":
            assert "-n" not in arguments

    coverage_files = [coverage_file for _, _, coverage_file in lanes]
    assert all(coverage_file.startswith(".coverage.") for coverage_file in coverage_files)
    assert len(coverage_files) == len(set(coverage_files))
    for job_id in ("pytest-ordinary", "pytest-serial", "pytest-stress", "package"):
        for _, _, coverage_file in (lane for lane in lanes if lane[0] == job_id):
            uploads = [
                step
                for step in _steps(_mapping(jobs[job_id]))
                if step.get("uses") == "actions/upload-artifact@v4"
            ]
            assert len(uploads) == 1
            upload = _mapping(uploads[0]["with"])
            assert upload["include-hidden-files"] is True
            assert upload["if-no-files-found"] == "error"
            upload_path = cast(str, upload["path"])
            if job_id == "pytest-stress":
                case = coverage_file.removeprefix(".coverage.stress-")
                upload_path = upload_path.replace("${{ matrix.case }}", case)
            assert upload_path == coverage_file
            assert cast(str, upload["name"]).startswith("coverage-")

    coverage_job = _mapping(jobs["coverage"])
    download = next(
        step for step in _steps(coverage_job) if step.get("uses") == "actions/download-artifact@v4"
    )
    download_with = _mapping(download["with"])
    assert download_with["pattern"] == "coverage-*"
    combine = next(
        step["run"] for step in _steps(coverage_job) if step.get("name") == "Combine coverage data"
    )
    assert isinstance(combine, str)
    combined_files = {
        argument for argument in shlex.split(combine) if argument.startswith(".coverage.")
    }
    assert combined_files == set(coverage_files)

    with (REPO_ROOT / "pyproject.toml").open("rb") as config_file:
        project = cast(dict[str, object], tomllib.load(config_file))
    dev_dependencies = _mapping(project["dependency-groups"])["dev"]
    assert isinstance(dev_dependencies, list)
    assert "coverage>=7.6" in dev_dependencies
    assert "pytest-xdist==3.8.0" in dev_dependencies
    install_coverage = next(
        step["run"] for step in _steps(coverage_job) if step.get("name") == "Install coverage"
    )
    assert isinstance(install_coverage, str)
    assert '"coverage>=7.6"' in install_coverage


def test_checks_is_a_fail_closed_aggregate_of_every_python_gate() -> None:
    """The required Checks status whitelists success from every Python gate."""
    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    checks = [job for job in jobs.values() if _mapping(job).get("name") == "Checks"]
    assert len(checks) == 1
    checks_job = _mapping(checks[0])
    assert checks_job["if"] == "always()"
    assert set(cast(list[str], checks_job["needs"])) == {
        "quality",
        "pytest-ordinary",
        "pytest-serial",
        "pytest-stress",
        "package",
        "coverage",
    }
    check_steps = _steps(checks_job)
    assert len(check_steps) == 1
    check_run = check_steps[0]["run"]
    assert isinstance(check_run, str)
    assert "toJSON(needs)" in check_run
    assert '"success"' in check_run
    for job in jobs.values():
        mapped_job = _mapping(job)
        assert "continue-on-error" not in mapped_job
        assert all("continue-on-error" not in step for step in _steps(mapped_job))

    assert workflow["permissions"] == {"contents": "read"}
    for job_id in (
        "quality",
        "pytest-ordinary",
        "pytest-serial",
        "pytest-stress",
        "package",
        "coverage",
    ):
        assert "permissions" not in _mapping(jobs[job_id])
    assert _mapping(jobs["web"])["name"] == "Web (lint + typecheck + unit)"
    assert _mapping(jobs["web-snapshots"])["name"] == "Web (Playwright snapshots)"
    checks_needs = cast(list[str], checks_job["needs"])
    assert "web" not in checks_needs
    assert "web-snapshots" not in checks_needs

    quality_steps = _steps(_mapping(jobs["quality"]))
    quality_runs = [step.get("run") for step in quality_steps]
    assert "python -m ruff check ." in quality_runs
    assert "python -m ruff format --check ." in quality_runs
    assert any(
        isinstance(run, str) and run.startswith("python -m pyright ") for run in quality_runs
    )
    contract_drift = next(
        step
        for step in quality_steps
        if step.get("run") == "python scripts/check_contract_drift.py"
    )
    assert contract_drift["if"] == "always()"
    for job in jobs.values():
        for step in _steps(_mapping(job)):
            uses = step.get("uses")
            if uses is not None:
                assert isinstance(uses, str)
                assert uses.startswith("actions/") or uses == "codecov/codecov-action@v5"
