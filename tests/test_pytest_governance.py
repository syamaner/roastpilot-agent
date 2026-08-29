"""Fail-closed governance tests for the Python test-suite markers."""

from __future__ import annotations

import ast
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import cast

import pytest
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
        "classify",
        "quality",
        "pytest-ordinary",
        "pytest-serial",
        "pytest-stress",
        "package",
        "coverage",
        "docs-fastpath",
    }
    check_steps = _steps(checks_job)
    gate_step = next(step for step in check_steps if "run" in step)
    check_run = gate_step["run"]
    assert isinstance(check_run, str)
    assert _mapping(gate_step["env"]) == {
        "NEEDS_JSON": "${{ toJSON(needs) }}",
        "MODE": "${{ needs.classify.outputs.mode }}",
    }
    assert "${{" not in check_run
    assert check_run.strip().startswith("python3 scripts/ci_gate_result.py")

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
        "docs-fastpath",
    ):
        assert "permissions" not in _mapping(jobs[job_id])
    assert _mapping(jobs["web"])["name"] == "Web (lint + typecheck + unit)"
    assert _mapping(jobs["web-snapshots"])["name"] == "Web (Playwright snapshots)"
    checks_needs = cast(list[str], checks_job["needs"])
    assert "web" not in checks_needs
    assert "web-snapshots" not in checks_needs
    assert "web-unit-worker" not in checks_needs
    assert "web-snapshots-worker" not in checks_needs

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


# ---------------------------------------------------------------------------
# Gate/worker split structure (#702 slice 2). The classify job's mode is now
# consumed by every conditional job and every gate; these tests are the
# authoritative replacement for the slice-1 inertness test retired at
# tests/test_ci_change_classifier.py:549-577 (contract §3.1, "Slice-1
# inertness test retirement" — this contract is the ratified slice-2
# authority that supersedes it).
# ---------------------------------------------------------------------------

_REQUIRED_CHECK_NAMES = {
    "Checks",
    "Web (lint + typecheck + unit)",
    "Web (Playwright snapshots)",
}
_GATE_JOB_IDS = {"checks", "web", "web-snapshots"}
_FULL_ONLY_JOB_IDS = {
    "quality",
    "pytest-ordinary",
    "pytest-serial",
    "pytest-stress",
    "package",
    "coverage",
    "web-unit-worker",
    "web-snapshots-worker",
}
_DOCS_ONLY_JOB_IDS = {"docs-fastpath"}
_FULL_ONLY_CONDITION = "needs.classify.outputs.mode != 'docs-only'"
_DOCS_ONLY_CONDITION = "needs.classify.outputs.mode == 'docs-only'"


def _gate_run_step(job: dict[str, object]) -> dict[str, object]:
    """Return the sole `ci_gate_result.py`-invoking step of a gate job."""
    steps = _steps(job)
    gate_steps = [step for step in steps if "run" in step]
    assert len(gate_steps) == 1
    return gate_steps[0]


def _gate_argv_classes(run: str) -> tuple[set[str], set[str], set[str]]:
    """Parse a gate's `--always`/`--full-only`/`--docs-only` argv into three sets."""
    tokens = shlex.split(run)
    always: set[str] = set()
    full_only: set[str] = set()
    docs_only: set[str] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--always":
            always.add(tokens[index + 1])
            index += 2
        elif token == "--full-only":
            full_only.add(tokens[index + 1])
            index += 2
        elif token == "--docs-only":
            docs_only.add(tokens[index + 1])
            index += 2
        else:
            index += 1
    return always, full_only, docs_only


@pytest.mark.docs_ci
def test_required_check_names_appear_exactly_once_on_trivial_gate_jobs() -> None:
    """Each required check name is a tiny gate: checkout + the gate script only."""
    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    seen_names: dict[str, str] = {}
    for job_id, job in jobs.items():
        mapped = _mapping(job)
        name = mapped.get("name")
        if not isinstance(name, str) or name not in _REQUIRED_CHECK_NAMES:
            continue
        assert name not in seen_names, f"{name!r} appears on both {seen_names[name]} and {job_id}"
        seen_names[name] = job_id
        assert job_id in _GATE_JOB_IDS
        steps = _steps(mapped)
        assert len(steps) == 2
        checkout, gate_step = steps
        assert checkout["uses"] == "actions/checkout@v6.0.2"
        checkout_with = cast(dict[str, object], checkout.get("with", {}))
        assert checkout_with.get("persist-credentials") is False
        run = gate_step["run"]
        assert isinstance(run, str)
        assert "install" not in run.lower()
        assert "${{" not in run
        assert "permissions" not in mapped
        assert mapped.get("timeout-minutes") == 10
    assert set(seen_names) == _REQUIRED_CHECK_NAMES
    assert set(seen_names.values()) == _GATE_JOB_IDS


@pytest.mark.docs_ci
def test_every_gate_needs_equals_its_declared_argv_class_union() -> None:
    """A gate's `needs` list is exactly the union of its declared argv classes."""
    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    for job_id in _GATE_JOB_IDS:
        job = _mapping(jobs[job_id])
        run = cast(str, _gate_run_step(job)["run"])
        always, full_only, docs_only = _gate_argv_classes(run)
        declared = always | full_only | docs_only
        assert len(always) + len(full_only) + len(docs_only) == len(declared), (
            f"{job_id}: a job id is declared in more than one class"
        )
        needs = job.get("needs", [])
        needs_set = {needs} if isinstance(needs, str) else set(cast(list[str], needs))
        assert needs_set == declared, f"{job_id}: needs {needs_set} != declared {declared}"
        assert "classify" in always


@pytest.mark.docs_ci
def test_checks_gate_declares_the_full_docs_only_worker_partition() -> None:
    """The `Checks` gate's declared classes match the full-only/docs-only job sets."""
    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    run = cast(str, _gate_run_step(_mapping(jobs["checks"]))["run"])
    always, full_only, docs_only = _gate_argv_classes(run)
    assert always == {"classify"}
    assert full_only == {
        "quality",
        "pytest-ordinary",
        "pytest-serial",
        "pytest-stress",
        "package",
        "coverage",
    }
    assert docs_only == {"docs-fastpath"}


@pytest.mark.docs_ci
def test_full_only_jobs_carry_the_exact_condition_and_need_classify() -> None:
    """Every full-only job's `if` is the exact string, never a status function."""
    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    for job_id in _FULL_ONLY_JOB_IDS:
        job = _mapping(jobs[job_id])
        assert job.get("if") == _FULL_ONLY_CONDITION, job_id
        needs = job.get("needs", [])
        needs_set = {needs} if isinstance(needs, str) else set(cast(list[str], needs))
        assert "classify" in needs_set, job_id


@pytest.mark.docs_ci
def test_docs_fastpath_carries_the_exact_docs_only_condition() -> None:
    """`docs-fastpath`'s `if` is the exact equality form, and it needs classify."""
    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    job = _mapping(jobs["docs-fastpath"])
    assert job.get("if") == _DOCS_ONLY_CONDITION
    needs = job.get("needs", [])
    needs_set = {needs} if isinstance(needs, str) else set(cast(list[str], needs))
    assert needs_set == {"classify"}


@pytest.mark.docs_ci
def test_no_worker_if_condition_uses_a_status_function() -> None:
    """No full-only/docs-only worker `if` contains `always()`, `failure()`, or `!cancelled()`."""
    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    for job_id in _FULL_ONLY_JOB_IDS | _DOCS_ONLY_JOB_IDS:
        condition = _mapping(jobs[job_id]).get("if")
        assert isinstance(condition, str)
        for forbidden in ("always()", "failure()", "!cancelled()"):
            assert forbidden not in condition, f"{job_id}: {condition!r} contains {forbidden!r}"


@pytest.mark.docs_ci
def test_no_trigger_level_path_filtering_or_continue_on_error_in_either_workflow() -> None:
    """Class 2 sweep: no `paths`/`paths-ignore`/`continue-on-error` in either workflow."""
    for workflow_path in (
        REPO_ROOT / ".github" / "workflows" / "ci.yml",
        REPO_ROOT / ".github" / "workflows" / "codeql.yml",
    ):
        text = workflow_path.read_text(encoding="utf-8")
        assert "paths:" not in text
        assert "paths-ignore:" not in text
        assert "continue-on-error" not in text


@pytest.mark.docs_ci
def test_every_ci_job_declares_a_timeout() -> None:
    """Every job in `ci.yml` carries `timeout-minutes` (a timeout is a job failure)."""
    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    for job_id, job in jobs.items():
        assert "timeout-minutes" in _mapping(job), job_id


@pytest.mark.docs_ci
def test_docs_fastpath_pytest_invocation_is_not_a_governed_lane() -> None:
    """The docs-fastpath job's own pytest step is outside the four governed lanes."""
    workflow = _workflow()
    lane_job_ids = {job_id for job_id, _, _ in _pytest_lanes(workflow)}
    assert "docs-fastpath" not in lane_job_ids
    jobs = _mapping(workflow["jobs"])
    docs_fastpath_steps = _steps(_mapping(jobs["docs-fastpath"]))
    pytest_runs = [
        step["run"]
        for step in docs_fastpath_steps
        if isinstance(step.get("run"), str) and "pytest" in cast(str, step["run"])
    ]
    assert len(pytest_runs) == 1
    assert '-m "(docs or docs_ci) and not stress"' in cast(str, pytest_runs[0])


@pytest.mark.docs_ci
def test_docs_fastpath_selection_is_nonempty_strict_subset_matching_governance() -> None:
    """The exact `(docs or docs_ci)` collection matches the governance-derived inventory."""
    docs_returncode, docs_selected, docs_output = _collect_nodeids(
        ["-m", "(docs or docs_ci) and not stress"]
    )
    assert docs_returncode == 0, docs_output
    full_returncode, full, full_output = _collect_nodeids([])
    assert full_returncode == 0, full_output
    assert docs_selected
    assert docs_selected < full

    governed_modules = {
        "test_agent_model_pins.py",
        "test_agent_worktree_controls.py",
        "test_capture_agent_usage.py",
        "test_config.py",
        "test_worktree_gate_recipe.py",
        "test_ci_change_classifier.py",
        "test_ci_gate_result.py",
        "test_ci_docs_fastpath_verify.py",
        "test_pytest_governance.py",
    }
    expected: set[str] = set()
    for path in sorted((REPO_ROOT / "tests").glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        module_marked = False
        function_marked: set[str] = set()
        for statement in tree.body:
            if isinstance(statement, ast.Assign):
                target_names = {
                    target.id for target in statement.targets if isinstance(target, ast.Name)
                }
                if "pytestmark" in target_names:
                    value = statement.value
                    expressions = (
                        value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
                    )
                    if any(
                        ast.unparse(expression) in {"pytest.mark.docs", "pytest.mark.docs_ci"}
                        for expression in expressions
                    ):
                        module_marked = True
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in statement.decorator_list:
                if ast.unparse(decorator) in {"pytest.mark.docs", "pytest.mark.docs_ci"}:
                    function_marked.add(statement.name)
        if module_marked:
            marked = {
                statement.name
                for statement in tree.body
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
                and statement.name.startswith("test_")
            }
        else:
            marked = function_marked
        if not marked:
            continue
        assert path.name in governed_modules, f"unexpected docs/docs_ci marker in {path.name}"
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
                str(path),
            ],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        for nodeid in result.stdout.splitlines():
            nodeid = nodeid.strip()
            if not re.fullmatch(r"tests/.*::.*", nodeid):
                continue
            function_name = nodeid.split("::")[-1].split("[")[0]
            if function_name in marked:
                expected.add(nodeid)
    assert docs_selected == expected


@pytest.mark.docs_ci
def test_docs_fastpath_step_order_includes_independent_reverification() -> None:
    """M20: the re-verification step exists and runs before the pytest selection."""
    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    steps = _steps(_mapping(jobs["docs-fastpath"]))
    runs = [cast(str, step["run"]) for step in steps if isinstance(step.get("run"), str)]
    verify_index = next(
        index for index, run in enumerate(runs) if "ci_docs_fastpath_verify.py" in run
    )
    pytest_index = next(index for index, run in enumerate(runs) if " pytest" in run)
    assert verify_index < pytest_index, (
        "the independent re-verification must run before the docs pytest selection"
    )


@pytest.mark.docs_ci
def test_docs_fastpath_uploads_coverage_with_disable_search() -> None:
    """M23: the docs-fastpath job's own real Codecov upload step exists."""
    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    steps = _steps(_mapping(jobs["docs-fastpath"]))
    uploads = [step for step in steps if step.get("uses") == "codecov/codecov-action@v5"]
    assert len(uploads) == 1
    upload_with = _mapping(uploads[0]["with"])
    assert upload_with["disable_search"] is True
    assert upload_with["files"] == "./coverage.xml"
    assert upload_with["token"] == "${{ secrets.CODECOV_TOKEN }}"


@pytest.mark.docs_ci
def test_web_snapshots_worker_retains_permissions_and_container_pin() -> None:
    """The moved Playwright worker keeps its narrowed permissions and image pin."""
    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    worker = _mapping(jobs["web-snapshots-worker"])
    assert worker["permissions"] == {"contents": "read", "packages": "read"}
    container = _mapping(worker["container"])
    assert container["image"] == "ghcr.io/${{ github.repository }}/playwright:v1.55.1-noble"


@pytest.mark.docs_ci
def test_gate_jobs_declare_no_permissions_block() -> None:
    """Gate jobs inherit the workflow-level `contents: read`; they declare none of their own."""
    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    for job_id in _GATE_JOB_IDS:
        assert "permissions" not in _mapping(jobs[job_id])


# ---------------------------------------------------------------------------
# CodeQL structure (#702 slice 2, B3).
# ---------------------------------------------------------------------------


def _codeql_workflow() -> dict[str, object]:
    """Load the CodeQL workflow as a structural mapping."""
    loaded = yaml.safe_load((REPO_ROOT / ".github/workflows/codeql.yml").read_text())
    assert isinstance(loaded, dict)
    raw_workflow = cast(dict[object, object], loaded)
    normalized = {
        "on" if key is True else cast(str, key): value for key, value in raw_workflow.items()
    }
    return _mapping(normalized)


@pytest.mark.docs_ci
def test_codeql_analyze_needs_classify_with_the_exact_docs_only_condition() -> None:
    """`analyze` is skipped only on a docs-only PR; every other trigger still analyzes."""
    workflow = _codeql_workflow()
    jobs = _mapping(workflow["jobs"])
    analyze = _mapping(jobs["analyze"])
    assert cast(list[str], analyze["needs"]) == ["classify"]
    assert analyze["if"] == _FULL_ONLY_CONDITION


@pytest.mark.docs_ci
def test_codeql_classify_pins_checkout_by_sha_and_narrows_permissions() -> None:
    """The new `classify` job matches this file's SHA-pin convention and narrows permissions."""
    workflow = _codeql_workflow()
    jobs = _mapping(workflow["jobs"])
    classify = _mapping(jobs["classify"])
    assert classify["permissions"] == {"contents": "read"}
    steps = _steps(classify)
    checkout = steps[0]
    uses = cast(str, checkout["uses"])
    assert uses.startswith("actions/checkout@")
    assert "@v" not in uses.split("#")[0], "checkout must be SHA-pinned, not tag-pinned"
    assert re.fullmatch(r"actions/checkout@[0-9a-f]{40}", uses)


@pytest.mark.docs_ci
def test_codeql_matrix_and_triggers_are_unwidened_and_unnarrowed() -> None:
    """The three-language matrix and all four triggers survive the split untouched."""
    workflow = _codeql_workflow()
    jobs = _mapping(workflow["jobs"])
    analyze = _mapping(jobs["analyze"])
    strategy = _mapping(analyze["strategy"])
    assert strategy["fail-fast"] is False
    matrix = _mapping(strategy["matrix"])
    assert matrix["language"] == ["actions", "javascript-typescript", "python"]

    triggers = _mapping(workflow["on"])
    assert _mapping(triggers["push"])["branches"] == ["main"]
    assert _mapping(triggers["pull_request"])["branches"] == ["main"]
    assert "schedule" in triggers
    assert "workflow_dispatch" in triggers
