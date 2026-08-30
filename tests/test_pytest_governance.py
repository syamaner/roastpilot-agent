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

#: D180 (#702, slice 3) job topology (§2.3).
_REQUIRED_CHECK_NAMES = {
    "checks": "Checks",
    "web": "Web (lint + typecheck + unit)",
    "web-snapshots": "Web (Playwright snapshots)",
}
_GATE_ARGV_CLASSES: dict[str, dict[str, tuple[str, ...]]] = {
    "checks": {
        "always": ("classify", "docs-fastpath", "codecov-upload"),
        "full-only": (
            "quality",
            "pytest-ordinary",
            "pytest-serial",
            "pytest-stress",
            "package",
            "coverage",
        ),
        "docs-only": (),
    },
    "web": {"always": ("classify",), "full-only": ("web-unit-worker",), "docs-only": ()},
    "web-snapshots": {
        "always": ("classify",),
        "full-only": ("web-snapshots-worker",),
        "docs-only": (),
    },
}
_FULL_ONLY_WORKER_JOBS = (
    "quality",
    "pytest-ordinary",
    "pytest-serial",
    "pytest-stress",
    "package",
    "coverage",
    "web-unit-worker",
    "web-snapshots-worker",
)
_FULL_ONLY_CONDITION = "needs.classify.outputs.mode != 'docs-only'"
_DOCS_ONLY_CONDITION = "needs.classify.outputs.mode == 'docs-only'"
_DOCS_FASTPATH_CONDITION = (
    "needs.classify.outputs.mode == 'docs-only' || needs.classify.outputs.mode == 'full'"
)
_PULL_REQUEST_CONDITION = "github.event_name == 'pull_request'"
_TIMEOUT_BEARING_JOBS = (
    "classify",
    "checks",
    "web",
    "web-snapshots",
    "docs-fastpath",
    "codecov-upload",
)
_CHECKOUT_PIN = "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"


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


def _module_has_docs_ci_marker(tree: ast.Module) -> bool:
    """Return whether a module broadly applies the docs-ci marker."""

    return any(
        isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in statement.targets
        )
        and any(ast.unparse(node) == "pytest.mark.docs_ci" for node in ast.walk(statement.value))
        for statement in tree.body
    )


def _marked_test_prefixes(marker: str) -> set[str]:
    """Return exact test-node prefixes bearing one function-level marker."""

    prefixes: set[str] = set()
    for path in _governed_test_module_paths(REPO_ROOT / "tests"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not _module_has_docs_ci_marker(tree)
        relative = path.relative_to(REPO_ROOT).as_posix()
        for statement in tree.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if statement.name.startswith("test_") and any(
                    ast.unparse(decorator) == f"pytest.mark.{marker}"
                    for decorator in statement.decorator_list
                ):
                    prefixes.add(f"{relative}::{statement.name}")
            elif isinstance(statement, ast.ClassDef):
                class_marked = any(
                    ast.unparse(decorator) == f"pytest.mark.{marker}"
                    for decorator in statement.decorator_list
                )
                for method in statement.body:
                    if (
                        isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and method.name.startswith("test_")
                        and (
                            class_marked
                            or any(
                                ast.unparse(decorator) == f"pytest.mark.{marker}"
                                for decorator in method.decorator_list
                            )
                        )
                    ):
                        prefixes.add(f"{relative}::{statement.name}::{method.name}")
    return prefixes


def _governed_test_module_paths(tests_root: Path) -> list[Path]:
    """Return both configured recursive pytest module filename forms."""

    return sorted(
        {path for pattern in ("test_*.py", "*_test.py") for path in tests_root.rglob(pattern)}
    )


def test_marker_prefix_inventory_covers_nested_suffix_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nested ``*_test.py`` markers cannot evade docs-ci or stress partition inventory."""

    tests_root = tmp_path / "tests" / "nested"
    tests_root.mkdir(parents=True)
    (tests_root / "suffix_test.py").write_text(
        "import pytest\n\n@pytest.mark.docs_ci\n@pytest.mark.stress\n"
        "def test_nested() -> None:\n    pass\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)
    expected = {"tests/nested/suffix_test.py::test_nested"}
    assert _marked_test_prefixes("docs_ci") == expected
    assert _marked_test_prefixes("stress") == expected


@pytest.mark.docs_ci
def test_docs_fastpath_marker_selection_is_closed_nonempty_and_strict() -> None:
    """The docs and docs-ci node inventories exactly govern the focused lane."""

    docs_code, docs, docs_output = _collect_nodeids(["-m", "docs and not stress"])
    docs_ci_code, docs_ci, docs_ci_output = _collect_nodeids(["-m", "docs_ci and not stress"])
    selected_code, selected, selected_output = _collect_nodeids(
        ["-m", "(docs or docs_ci) and not stress"]
    )
    full_code, full, full_output = _collect_nodeids([])
    assert docs_code == 0, docs_output
    assert docs_ci_code == 0, docs_ci_output
    assert selected_code == 0, selected_output
    assert full_code == 0, full_output
    assert selected == docs | docs_ci
    assert selected and selected < full
    docs_ci_prefixes = _marked_test_prefixes("docs_ci")
    assert docs_ci_prefixes
    assert all(
        any(nodeid == prefix or nodeid.startswith(f"{prefix}[") for prefix in docs_ci_prefixes)
        for nodeid in docs_ci
    )
    selected_prefixes = {
        prefix
        for prefix in docs_ci_prefixes
        if any(nodeid == prefix or nodeid.startswith(f"{prefix}[") for nodeid in docs_ci)
    }
    assert selected_prefixes == docs_ci_prefixes


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


def _argv_declared_classes(run: str) -> dict[str, tuple[str, ...]]:
    """Parse one gate invocation's ``--always``/``--full-only``/``--docs-only`` argv."""
    tokens = shlex.split(run)
    classes: dict[str, list[str]] = {"always": [], "full-only": [], "docs-only": []}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        flag = token.removeprefix("--")
        if flag in classes:
            classes[flag].append(tokens[index + 1])
            index += 2
        else:
            index += 1
    return {key: tuple(value) for key, value in classes.items()}


def test_checks_is_a_fail_closed_aggregate_of_every_python_gate() -> None:
    """The required Checks status whitelists success from every Python gate.

    D180 (#702, slice 3): ``checks`` now runs the base-trusted
    ``ci_gate_result.py`` helper (one checkout + one gate invocation) rather
    than an inline heredoc, and ``needs`` includes ``classify`` and
    ``docs-fastpath`` alongside the six full-path jobs.
    """
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
        "codecov-upload",
    }
    check_steps = _steps(checks_job)
    assert len(check_steps) == 2
    checkout_step, gate_step = check_steps
    assert checkout_step["uses"] == _CHECKOUT_PIN
    checkout_with = _mapping(checkout_step["with"])
    assert checkout_with["path"] == ".ci-gate-base"
    assert checkout_with["persist-credentials"] is False

    gate_run = gate_step["run"]
    assert isinstance(gate_run, str)
    assert gate_run.startswith("python3 .ci-gate-base/scripts/ci_gate_result.py")
    assert "${{" not in gate_run
    gate_env = _mapping(gate_step["env"])
    assert gate_env["MODE"] == "${{ needs.classify.outputs.mode }}"
    assert gate_env["NEEDS_JSON"] == "${{ toJSON(needs) }}"

    for job in jobs.values():
        mapped_job = _mapping(job)
        assert "continue-on-error" not in mapped_job
        assert all("continue-on-error" not in step for step in _steps(mapped_job))

    assert workflow["permissions"] == {"contents": "read"}
    for job_id in (
        "classify",
        "quality",
        "pytest-ordinary",
        "pytest-serial",
        "pytest-stress",
        "package",
        "coverage",
        "docs-fastpath",
        "codecov-upload",
        "checks",
        "web-unit-worker",
        "web",
        "web-snapshots",
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


@pytest.mark.docs_ci
def test_required_gates_run_exactly_one_base_checkout_and_one_gate_invocation() -> None:
    """Each of the three required checks belongs to a minimal gate job (D180 §3.6)."""
    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    for job_id, required_name in _REQUIRED_CHECK_NAMES.items():
        job = _mapping(jobs[job_id])
        assert job["name"] == required_name
        steps = _steps(job)
        assert len(steps) == 2, f"{job_id}: a gate job must have exactly one checkout + one run"
        checkout_step, gate_step = steps
        assert checkout_step["uses"] == _CHECKOUT_PIN
        checkout_with = _mapping(checkout_step["with"])
        assert checkout_with["path"] == ".ci-gate-base"
        gate_run = gate_step["run"]
        assert isinstance(gate_run, str)
        assert gate_run.startswith("python3 .ci-gate-base/scripts/ci_gate_result.py")
        assert "${{" not in gate_run
        assert job["if"] == "always()"


@pytest.mark.docs_ci
def test_ci_checkout_actions_are_immutably_pinned() -> None:
    """Every CI checkout action uses the approved immutable v6.0.2 commit."""

    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    checkout_uses = [
        step["uses"]
        for job in jobs.values()
        for step in _steps(_mapping(job))
        if isinstance(step.get("uses"), str) and str(step["uses"]).startswith("actions/checkout@")
    ]
    assert checkout_uses
    assert all(use == _CHECKOUT_PIN for use in checkout_uses)
    assert "actions/checkout@v6.0.2" not in (REPO_ROOT / ".github/workflows/ci.yml").read_text()


@pytest.mark.docs_ci
def test_gate_needs_equals_the_union_of_its_declared_argv_classes() -> None:
    """A ``needs`` edit cannot silently drop a check without also editing argv (D180 §3.6)."""
    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    for job_id, classes in _GATE_ARGV_CLASSES.items():
        job = _mapping(jobs[job_id])
        _, gate_step = _steps(job)
        gate_run = gate_step["run"]
        assert isinstance(gate_run, str)
        declared = _argv_declared_classes(gate_run)
        assert declared == classes, f"{job_id}: argv classes drifted from the expected mapping"
        needs = set(cast(list[str], job["needs"]))
        declared_union = {name for names in classes.values() for name in names}
        assert needs == declared_union, f"{job_id}: needs must equal the union of argv classes"


@pytest.mark.docs_ci
def test_full_only_workers_carry_the_exact_literal_condition_and_no_status_function() -> None:
    """Every full-only worker's ``if`` is the exact literal string (D180 §3.6)."""
    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    for job_id in _FULL_ONLY_WORKER_JOBS:
        job = _mapping(jobs[job_id])
        assert job.get("if") == _FULL_ONLY_CONDITION, f"{job_id}: wrong or missing condition"
        needs = job.get("needs", [])
        needs_values = {needs} if isinstance(needs, str) else set(cast(list[str], needs))
        assert "classify" in needs_values, f"{job_id}: must declare needs: classify"
        for forbidden in ("always()", "failure()", "!cancelled()"):
            assert forbidden not in cast(str, job["if"])

    docs_fastpath = _mapping(jobs["docs-fastpath"])
    assert docs_fastpath.get("if") == _DOCS_FASTPATH_CONDITION
    assert "classify" in set(cast(list[str], docs_fastpath["needs"]))

    for job_id in _REQUIRED_CHECK_NAMES:
        assert _mapping(jobs[job_id])["if"] == "always()"


@pytest.mark.docs_ci
def test_no_trigger_level_path_filtering_or_continue_on_error() -> None:
    """A required workflow with a `paths` filter would leave its check pending forever."""
    for workflow_path in (
        REPO_ROOT / ".github" / "workflows" / "ci.yml",
        REPO_ROOT / ".github" / "workflows" / "codeql.yml",
    ):
        raw_loaded = cast(dict[object, object], yaml.safe_load(workflow_path.read_text()))
        loaded = {
            "on" if key is True else cast(str, key): value for key, value in raw_loaded.items()
        }
        on_block = loaded.get("on")
        assert isinstance(on_block, dict)
        for trigger in cast(dict[str, object], on_block).values():
            if isinstance(trigger, dict):
                assert "paths" not in trigger
                assert "paths-ignore" not in trigger
        jobs = cast(dict[str, object], loaded["jobs"])
        for job in jobs.values():
            mapped_job = _mapping(job)
            assert "continue-on-error" not in mapped_job
            for step in _steps(mapped_job):
                assert "continue-on-error" not in step


@pytest.mark.docs_ci
def test_codeql_analyze_runs_unless_classify_succeeds_with_exact_docs_only() -> None:
    """CodeQL may skip only the successful, exact docs-only classifier verdict."""

    loaded = yaml.safe_load((REPO_ROOT / ".github/workflows/codeql.yml").read_text())
    assert isinstance(loaded, dict)
    raw_workflow = cast(dict[object, object], loaded)
    workflow = _mapping(
        {"on" if key is True else cast(str, key): value for key, value in raw_workflow.items()}
    )
    jobs = _mapping(workflow["jobs"])
    analyze = _mapping(jobs["analyze"])
    expected = (
        "always() && !(needs.classify.result == 'success' "
        "&& needs.classify.outputs.mode == 'docs-only')"
    )
    assert analyze["needs"] == ["classify"]
    assert analyze["if"] == expected

    for mutant in (
        "needs.classify.outputs.mode != 'docs-only'",
        "always() && needs.classify.result == 'success'",
    ):
        mutated = dict(analyze)
        mutated["if"] = mutant
        assert mutated["if"] != expected


@pytest.mark.docs_ci
def test_new_or_newly_tiny_jobs_carry_a_timeout() -> None:
    """The four new/newly-tiny jobs are bounded; heavy pre-existing jobs are out of scope."""
    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    for job_id in _TIMEOUT_BEARING_JOBS:
        assert isinstance(_mapping(jobs[job_id]).get("timeout-minutes"), int)


@pytest.mark.docs_ci
def test_docs_fastpath_job_structure_and_dependency_group() -> None:
    """The docs-only fast path runs the exact ordered steps D180 §2.5 specifies."""
    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    job = _mapping(jobs["docs-fastpath"])
    assert job["name"] == "Docs fast path (docs tests + coverage artifact)"
    steps = _steps(job)
    step_names = [step.get("name") for step in steps]
    assert step_names == [
        "Check out repository",
        "Set up Python",
        "Whitespace and diff validation",
        "Install dependencies (docs-ci group only — no MCP/audio/ML/build tooling)",
        "Run docs and fast-path tooling tests with coverage",
        "Normalize coverage filenames for Codecov",
        "Upload normalized docs coverage artifact",
    ]
    install_step = steps[3]
    whitespace_step = steps[2]
    assert whitespace_step["if"] == _PULL_REQUEST_CONDITION
    whitespace_env = _mapping(whitespace_step["env"])
    assert whitespace_env == {"BASE_SHA": "${{ github.event.pull_request.base.sha }}"}
    assert install_step["run"] == (
        "python -m pip install --upgrade pip\npython -m pip install --group docs-ci\n"
    )
    pytest_step = steps[4]
    pytest_run = cast(str, pytest_step["run"])
    assert pytest_run.startswith("env -u OPENROUTER_API_KEY PYTHONPATH=src python -m pytest ")
    assert "working-directory" not in job
    assert "working-directory" not in pytest_step
    assert "env" not in pytest_step
    assert '-m "(docs or docs_ci) and not stress"' in pytest_run
    assert "--cov=scripts" in pytest_run
    assert "--cov=.agents/skills/capture-agent-usage/scripts" in pytest_run
    assert "--cov-report=xml:coverage.xml" in pytest_run
    assert "OPENROUTER_API_KEY" in pytest_run
    artifact_step = steps[6]
    assert artifact_step["uses"] == "actions/upload-artifact@v4"
    assert _mapping(artifact_step["with"]) == {
        "name": "codecov-coverage-docs",
        "path": "codecov-input",
        "if-no-files-found": "error",
        "include-hidden-files": True,
    }

    codecov_upload = _mapping(jobs["codecov-upload"])
    assert codecov_upload["needs"] == ["classify", "coverage", "docs-fastpath"]
    assert codecov_upload["if"] == "always()"
    codecov_steps = _steps(codecov_upload)
    assert all("run" not in step and "uses" in step for step in codecov_steps)
    assert all(not str(step["uses"]).startswith("actions/checkout@") for step in codecov_steps)
    assert {step["uses"] for step in codecov_steps} == {
        "actions/download-artifact@v4",
        "codecov/codecov-action@v5",
    }
    for step in codecov_steps:
        with_block = _mapping(step["with"])
        if step["uses"] == "actions/download-artifact@v4":
            assert with_block["name"] in {"codecov-coverage-full", "codecov-coverage-docs"}
            assert with_block["path"] == "codecov-input"
        else:
            assert with_block == {
                "token": "${{ secrets.CODECOV_TOKEN }}",
                "files": "./coverage.xml",
                "root_dir": ".",
                "working-directory": "./codecov-input",
                "disable_search": True,
                "disable_file_fixes": True,
            }

    # docs-fastpath's pytest invocation is deliberately not a governed lane:
    # it never appears in `_pytest_lanes`'s job-id allowlist.
    assert "docs-fastpath" not in {job_id for job_id, _, _ in _pytest_lanes(workflow)}

    with (REPO_ROOT / "pyproject.toml").open("rb") as config_file:
        project = cast(dict[str, object], tomllib.load(config_file))
    dependency_groups = _mapping(project["dependency-groups"])
    docs_ci_group = dependency_groups["docs-ci"]
    dev_group = dependency_groups["dev"]
    project_dependencies = project["project"]
    assert isinstance(docs_ci_group, list)
    assert isinstance(dev_group, list)
    assert isinstance(project_dependencies, dict)
    dependencies = _mapping(cast(dict[str, object], project_dependencies))["dependencies"]
    assert isinstance(dependencies, list)
    docs_ci_entries = cast(list[str], docs_ci_group)
    admissible = set(cast(list[str], dev_group)) | set(cast(list[str], dependencies))
    assert set(docs_ci_entries) <= admissible
    normalized = {
        re.split(r"[<>=!~;\[]", entry, maxsplit=1)[0].lower() for entry in docs_ci_entries
    }
    assert not (
        {
            "sounddevice",
            "coffee-roaster-mcp",
            "transformers",
            "onnxruntime",
            "torch",
            "build",
            "hatchling",
            "hatch",
        }
        & normalized
    )
    assert not any("playwright" in name for name in normalized)
    assert "src" in cast(list[str], _pytest_options()["pythonpath"])

    markers = cast(list[str], _pytest_options()["markers"])
    registered = {marker.partition(":")[0] for marker in markers}
    assert "docs_ci" in registered
