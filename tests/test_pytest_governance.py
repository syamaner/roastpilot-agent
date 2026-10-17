"""Fail-closed governance tests for the Python test-suite markers."""

from __future__ import annotations

import ast
import copy
import hashlib
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import ci_ordinary_shards
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
    "package-arm64",
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
    "package-arm64",
)
_CHECKOUT_PIN = "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"

#: #954: the closed inventory omitted from pull_request/push CI and selected only by
#: scheduled and manually dispatched ``ci.yml`` runs. Every member must be ``serial``.
_EXHAUSTIVE_ONLY_INVENTORY = frozenset(
    {
        "tests/test_capture_agent_usage.py::test_rendered_qa_full_suite_gate_runs_under_the_actual_wrapper"
    }
)
#: #1010: the complete installer subset that remains in the serial lane.
_INSTALLER_RETAINED_SERIAL_INVENTORY = frozenset(
    {
        "tests/test_pi_installer.py::test_fake_log_grammar_fails_closed_for_forced_concurrency",
        "tests/test_pi_installer.py::test_framing_writer_closes_all_popen_pipes",
        "tests/test_pi_installer.py::test_framing_writer_wait_supports_a_high_pipe_descriptor",
        "tests/test_pi_installer.py::test_fake_log_framing_is_deterministic_under_two_writers",
        "tests/test_pi_installer.py::test_operator_home_temporary_root_boundary_differs_by_mode[/tmp]",
        "tests/test_pi_installer.py::test_operator_home_temporary_root_boundary_differs_by_mode[/var/tmp]",
        "tests/test_pi_installer.py::test_process_guard_default_sigalrm_termination_maps_to_generic_refusal_and_kills_alarm_mutant",
    }
)
#: #954: the exact serial-lane selection expression; any other value fails closed.
_SERIAL_SELECTION_EXPRESSION = (
    "${{ (github.event_name == 'schedule' || github.event_name == 'workflow_dispatch') "
    "&& 'serial and not stress' || 'serial and not stress and not exhaustive_only' }}"
)
_EXHAUSTIVE_SERIAL_SELECTION = "serial and not stress"
_FAST_SERIAL_SELECTION = "serial and not stress and not exhaustive_only"
#: Exact event -> serial selection; an unmapped event is a KeyError (fail closed).
_SERIAL_SELECTION_BY_EVENT = {
    "pull_request": _FAST_SERIAL_SELECTION,
    "push": _FAST_SERIAL_SELECTION,
    "schedule": _EXHAUSTIVE_SERIAL_SELECTION,
    "workflow_dispatch": _EXHAUSTIVE_SERIAL_SELECTION,
}
_CI_TRIGGERS = {
    "pull_request": None,
    "push": {"branches": ["main"]},
    "workflow_dispatch": None,
    "schedule": [{"cron": "23 2 * * *"}],
}
_COVERAGE_ARGUMENTS = (
    "--cov=roastpilot_agent --cov=scripts --cov=.agents/skills/capture-agent-usage/scripts "
    "--cov-report="
)
_COLLECTION_CACHE_MAX_ENTRIES = 32
_COLLECTION_CACHE: dict[tuple[Path, tuple[str, ...], str], tuple[int, frozenset[str], str]] = {}
#: #1039: the CI-only shard shim and its two options, stripped before collection probes.
_ORDINARY_SHARD_PLUGIN = "scripts.ci_ordinary_shard_plugin"
_ORDINARY_SHARD_OPTIONS = frozenset({"--ordinary-shard-index", "--ordinary-shard-receipt"})
#: #1039: the closed ordinary two-index matrix (YAML strings, never ints).
_ORDINARY_SHARD_MATRIX: tuple[dict[str, str], ...] = ({"index": "0"}, {"index": "1"})
_ORDINARY_RUN = (
    f"python -m pytest -n 4 --dist worksteal -p {_ORDINARY_SHARD_PLUGIN} "
    "--ordinary-shard-index ${{ matrix.index }} "
    "--ordinary-shard-receipt ordinary-shard-receipt.json "
    '-m "not serial and not stress" '
    f"--ignore=tests/test_packaging.py {_COVERAGE_ARGUMENTS}"
)
_SERIAL_RUN = (
    'python -m pytest -m "$SERIAL_SELECTION" '
    f"--ignore=tests/test_packaging.py {_COVERAGE_ARGUMENTS}"
)
#: #1039: every new producer, validation, upload, download and combine step, pinned.
_ORDINARY_FINALIZE_RUN = (
    "python scripts/ci_ordinary_shards.py finalize --index ${{ matrix.index }} "
    "--receipt ordinary-shard-receipt.json "
    "--coverage .coverage.ordinary-shard-${{ matrix.index }} "
    "--bundle ordinary-shard-bundle --identity-mode normal"
)
_ORDINARY_BUNDLE_UPLOAD: dict[str, object] = {
    "name": "ordinary-shard-${{ matrix.index }}",
    "path": "ordinary-shard-bundle",
    "if-no-files-found": "error",
    "retention-days": 7,
    "overwrite": True,
}
_ORDINARY_SHARD_STEPS: tuple[dict[str, object], ...] = (
    {
        "name": "Run ordinary tests with coverage",
        "env": {"COVERAGE_FILE": ".coverage.ordinary-shard-${{ matrix.index }}"},
        "run": _ORDINARY_RUN,
    },
    {"name": "Finalize ordinary shard bundle", "run": _ORDINARY_FINALIZE_RUN},
    {
        "name": "Upload ordinary shard bundle",
        "uses": "actions/upload-artifact@v4",
        "with": _ORDINARY_BUNDLE_UPLOAD,
    },
)
_ORDINARY_SHARD_COMBINE_RUN = (
    "python -m coverage combine --keep --data-file=.coverage.ordinary "
    "ordinary-shards/ordinary-shard-0/coverage.data "
    "ordinary-shards/ordinary-shard-1/coverage.data"
)
#: The pre-#1039 final combine, byte-identical.
_FINAL_COMBINE_RUN = (
    "python -m coverage combine --keep .coverage.ordinary .coverage.serial "
    ".coverage.stress-opaque-event-boundary .coverage.stress-opaque-total-accepts-exact "
    ".coverage.stress-opaque-total-rejects-one-over .coverage.package"
)
_COVERAGE_SHARD_STEPS: tuple[dict[str, object], ...] = (
    {
        "name": "Download coverage data",
        "uses": "actions/download-artifact@v4",
        "with": {"pattern": "coverage-*", "merge-multiple": True, "path": "."},
    },
    {
        "name": "Download ordinary shard 0 bundle",
        "uses": "actions/download-artifact@v4",
        "with": {"name": "ordinary-shard-0", "path": "ordinary-shards/ordinary-shard-0"},
    },
    {
        "name": "Download ordinary shard 1 bundle",
        "uses": "actions/download-artifact@v4",
        "with": {"name": "ordinary-shard-1", "path": "ordinary-shards/ordinary-shard-1"},
    },
    {
        "name": "Verify ordinary shard bundles",
        "run": (
            "python scripts/ci_ordinary_shards.py verify --bundle-root ordinary-shards "
            "--identity-mode normal"
        ),
    },
    {"name": "Combine ordinary shard coverage", "run": _ORDINARY_SHARD_COMBINE_RUN},
    {"name": "Combine coverage data", "run": _FINAL_COMBINE_RUN},
)


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


def _pytest_lanes(workflow: dict[str, object], event: str) -> list[tuple[str, list[str], str]]:
    """Expand each governed pytest lane for one trigger event.

    ``$SERIAL_SELECTION`` resolves only when the step's env value equals the pinned
    expression exactly; the event then maps to its branch, and an unmapped event
    fails closed.
    """
    selection = _SERIAL_SELECTION_BY_EVENT[event]
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
        elif job_id == "pytest-ordinary":
            strategy = _mapping(job["strategy"])
            assert _mapping(strategy["matrix"]) == {"include": list(_ORDINARY_SHARD_MATRIX)}
            matrix_entries = [dict(entry) for entry in _ORDINARY_SHARD_MATRIX]
        for step in _steps(job):
            run = step.get("run")
            if not isinstance(run, str) or not run.startswith("python -m pytest"):
                continue
            env = _mapping(step["env"])
            coverage_file = env["COVERAGE_FILE"]
            assert isinstance(coverage_file, str)
            if "SERIAL_SELECTION" in env or "$SERIAL_SELECTION" in run:
                assert job_id == "pytest-serial"
                assert env["SERIAL_SELECTION"] == _SERIAL_SELECTION_EXPRESSION
                assert run.count('"$SERIAL_SELECTION"') == 1
                run = run.replace('"$SERIAL_SELECTION"', shlex.quote(selection))
            for matrix_entry in matrix_entries:
                command = run
                resolved_coverage_file = coverage_file
                for key, value in matrix_entry.items():
                    assert isinstance(value, str)
                    command = command.replace(f"${{{{ matrix.{key} }}}}", value)
                    resolved_coverage_file = resolved_coverage_file.replace(
                        f"${{{{ matrix.{key} }}}}", value
                    )
                assert "$" not in command
                arguments = shlex.split(command)
                assert arguments[:3] == ["python", "-m", "pytest"]
                lanes.append((job_id, arguments[3:], resolved_coverage_file))
    return lanes


def _collection_arguments(arguments: list[str]) -> list[str]:
    """Remove execution and coverage options before a lane collection probe.

    The closed #1039 strip list also removes exactly the
    ``-p scripts.ci_ordinary_shard_plugin`` pair and the two shard options, so
    both ordinary shards share one cached unsharded collection; the shard filter
    is then applied by the test-local oracle (:func:`_lane_shard_nodeids`).
    """
    retained: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if (
            argument in {"-n", "--dist"}
            or argument in _ORDINARY_SHARD_OPTIONS
            or (argument == "-p" and arguments[index + 1 : index + 2] == [_ORDINARY_SHARD_PLUGIN])
        ):
            index += 2
        elif argument.startswith("--cov=") or argument.startswith("--cov-report="):
            index += 1
        else:
            retained.append(argument)
            index += 1
    return retained


def _oracle_shard_index(nodeid: str) -> int:
    """Return the v1 shard index with a test-local oracle (never the CI helper).

    A big-endian integer is even or odd exactly as its final byte is, so the
    oracle reads the parity of the digest's last byte.
    """
    return hashlib.sha256(nodeid.encode("utf-8", "strict")).digest()[-1] & 1


def _lane_shard_nodeids(arguments: list[str], nodeids: set[str]) -> set[str]:
    """Apply an ordinary lane's shard filter to its unsharded cached collection."""
    if "--ordinary-shard-index" not in arguments:
        return nodeids
    index = arguments[arguments.index("--ordinary-shard-index") + 1]
    assert index in {"0", "1"}
    return {nodeid for nodeid in nodeids if _oracle_shard_index(nodeid) == int(index)}


def _collection_source_digest(repository_root: Path) -> str:
    """Digest known Python, pytest-config, and CI YAML collection inputs.

    The closed collection roots come from pytest's configured ``testpaths`` and
    ``pythonpath``: Python files under ``tests``, ``src``, ``scripts``, and
    ``.agents``; root Python modules; root pytest configuration; and GitHub workflow
    YAML are included. Other known roots may contain documentation or tooling Python,
    but are neither pytest collection roots nor configured import paths. Generated and
    vendor directories are never traversed. A new unrecognised top-level Python root
    raises, rather than permitting an incomplete digest to reuse stale collection.
    """

    digest = hashlib.sha256()
    configuration_files = {"pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini"}
    python_roots = (".agents", "scripts", "src", "tests")
    yaml_roots = (".github",)
    excluded_directories = {
        ".git",
        ".venv",
        "node_modules",
        ".pytest_cache",
        "build",
        "dist",
        "__pycache__",
    }
    known_directory_roots = (
        excluded_directories
        | set(python_roots)
        | set(yaml_roots)
        | {
            ".claude",
            ".codex",
            ".design-sync",
            "docs",
            "packaging",
            "web",
        }
    )
    for child in repository_root.iterdir():
        if (
            child.is_dir()
            and child.name not in known_directory_roots
            and any(path.suffix == ".py" for path in child.iterdir() if path.is_file())
        ):
            raise AssertionError(f"unrecognised collection Python root: {child}")

    paths = {
        *repository_root.glob("*.py"),
        *(repository_root / name for name in configuration_files),
    }
    for root_name in python_roots:
        root = repository_root / root_name
        if root.is_dir():
            paths.update(root.rglob("*.py"))
    for root_name in yaml_roots:
        root = repository_root / root_name
        if root.is_dir():
            paths.update(root.rglob("*.yml"))
    for path in sorted(paths):
        relative_path = path.relative_to(repository_root)
        if excluded_directories.intersection(relative_path.parts) or not path.is_file():
            continue
        try:
            contents = path.read_bytes()
        except OSError as error:
            raise AssertionError(f"collection input became unreadable: {path}") from error
        digest.update(relative_path.as_posix().encode())
        digest.update(b"\0")
        digest.update(contents)
        digest.update(b"\0")
    return digest.hexdigest()


def _collect_nodeids(arguments: list[str]) -> tuple[int, set[str], str]:
    """Collect one lane and return its status, node IDs, and diagnostic output.

    The bounded process-local cache shares equivalent collection probes while all
    repository-controlled Python and pytest configuration inputs are byte-identical.
    A source digest invalidates a stale entry when governance mutation tests edit
    or add a collected file or imported collection dependency.
    Non-zero collection results are cached too, so every consumer still fails
    closed when it asserts the returned status.
    """

    repository_root = REPO_ROOT.resolve()
    collection_arguments = tuple(_collection_arguments(arguments))
    cache_key = (repository_root, collection_arguments, _collection_source_digest(repository_root))
    cached = _COLLECTION_CACHE.get(cache_key)
    if cached is not None:
        return cached[0], set(cached[1]), cached[2]
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
            *collection_arguments,
        ],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    nodeids = {
        line.strip()
        for line in result.stdout.splitlines()
        if re.fullmatch(r"tests/.*::.*", line.strip())
    }
    cached_result = (result.returncode, frozenset(nodeids), result.stdout + result.stderr)
    _COLLECTION_CACHE[cache_key] = cached_result
    if len(_COLLECTION_CACHE) > _COLLECTION_CACHE_MAX_ENTRIES:
        _COLLECTION_CACHE.pop(next(iter(_COLLECTION_CACHE)))
    return cached_result[0], set(cached_result[1]), cached_result[2]


def _clear_collection_cache() -> Iterator[None]:
    """Isolate process-local collection cache state for a mutation test."""

    _COLLECTION_CACHE.clear()
    try:
        yield
    finally:
        _COLLECTION_CACHE.clear()


cleared_collection_cache = pytest.fixture(_clear_collection_cache)


def test_collection_memo_reuses_equivalent_argv_and_invalidates_changed_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleared_collection_cache: None
) -> None:
    """Collection reuse is bounded, fail-closed, and cannot survive source edits."""

    first_root = tmp_path / "first"
    first_test = first_root / "tests/test_collected.py"
    first_test.parent.mkdir(parents=True)
    first_test.write_text("def test_original() -> None:\n    pass\n", encoding="utf-8")
    imported_source = first_root / "src/package.py"
    imported_source.parent.mkdir()
    imported_source.write_text("VALUE = 'original'\n", encoding="utf-8")
    (first_root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    workflow = first_root / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: test\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            1 if "broken" in argv else 0,
            stdout="tests/test_collected.py::test_original\n",
            stderr="collection failed" if "broken" in argv else "",
        )

    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", first_root)
    monkeypatch.setattr(subprocess, "run", fake_run)
    first = _collect_nodeids(["-m", "docs"])
    assert first[0] == 0
    first[1].clear()
    repeated = _collect_nodeids(["-m", "docs"])
    assert repeated[1] == {"tests/test_collected.py::test_original"}
    assert _COLLECTION_CACHE
    assert len(calls) == 1
    assert _collect_nodeids(["-m", "serial"])[0] == 0
    assert len(calls) == 2

    first_test.write_text("def test_edited() -> None:\n    pass\n", encoding="utf-8")
    assert _collect_nodeids(["-m", "docs"])[0] == 0
    assert len(calls) == 3

    imported_source.write_text("VALUE = 'edited'\n", encoding="utf-8")
    assert _collect_nodeids(["-m", "docs"])[0] == 0
    assert len(calls) == 4

    (first_root / "pyproject.toml").write_text("[tool.pytest.ini_options]\naddopts = '-q'\n")
    assert _collect_nodeids(["-m", "docs"])[0] == 0
    assert len(calls) == 5
    workflow.write_text("name: changed\n", encoding="utf-8")
    assert _collect_nodeids(["-m", "docs"])[0] == 0
    assert len(calls) == 6

    unexpected_root = first_root / "new_collection_root"
    unexpected_root.mkdir()
    (unexpected_root / "plugin.py").write_text("VALUE = 'new'\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="unrecognised collection Python root"):
        _collect_nodeids(["-m", "docs"])
    (unexpected_root / "plugin.py").unlink()
    unexpected_root.rmdir()

    failed = _collect_nodeids(["-m", "broken"])
    assert failed[0] == 1
    assert _collect_nodeids(["-m", "broken"])[0] == 1
    assert len(calls) == 7

    second_root = tmp_path / "second"
    for relative_path in (
        Path("tests/test_collected.py"),
        Path("src/package.py"),
        Path("pyproject.toml"),
        Path(".github/workflows/ci.yml"),
    ):
        second_path = second_root / relative_path
        second_path.parent.mkdir(parents=True, exist_ok=True)
        second_path.write_bytes((first_root / relative_path).read_bytes())
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", second_root)
    assert _collect_nodeids(["-m", "docs"])[0] == 0
    assert len(calls) == 8


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


def test_installer_serial_inventory_is_frozen() -> None:
    """Only the seven classified host-resource installer nodes remain serial (#1010)."""
    returncode, serial, output = _collect_nodeids(["-m", "serial"])
    assert returncode == 0, output
    installer_serial = {
        nodeid for nodeid in serial if nodeid.startswith("tests/test_pi_installer.py::")
    }
    assert installer_serial == _INSTALLER_RETAINED_SERIAL_INVENTORY


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


@pytest.mark.parametrize(
    ("events", "exhaustive"),
    [(("pull_request", "push"), False), (("schedule", "workflow_dispatch"), True)],
    ids=("pull-request-and-push", "schedule-and-workflow-dispatch"),
)
def test_ci_lane_selections_partition_the_full_collection(
    events: tuple[str, str], exhaustive: bool
) -> None:
    """Every collected test belongs to exactly one lane, or to the closed inventory.

    Pull-request and push lanes plus the ``exhaustive_only`` inventory equal the full
    collection; scheduled and dispatched lanes alone equal it (#954).
    """
    workflow = _workflow()
    first, second = events
    assert _pytest_lanes(workflow, first) == _pytest_lanes(workflow, second)
    by_job: dict[str, set[str]] = {}
    for job_id, arguments, _ in _pytest_lanes(workflow, first):
        returncode, nodeids, output = _collect_nodeids(arguments)
        assert returncode == 0, output
        by_job.setdefault(job_id, set()).update(_lane_shard_nodeids(arguments, nodeids))

    full_returncode, full, full_output = _collect_nodeids([])
    stress_returncode, stress, stress_output = _collect_nodeids(["-m", "stress"])
    inventory_returncode, inventory, inventory_output = _collect_nodeids(["-m", "exhaustive_only"])
    serial_returncode, serial, serial_output = _collect_nodeids(["-m", "serial"])
    assert full_returncode == 0, full_output
    assert stress_returncode == 0, stress_output
    assert inventory_returncode == 0, inventory_output
    assert serial_returncode == 0, serial_output
    assert inventory == _EXHAUSTIVE_ONLY_INVENTORY
    assert inventory <= serial
    assert set(by_job) == {"pytest-ordinary", "pytest-serial", "pytest-stress", "package"}
    assert all(by_job.values())

    lane_sets = list(by_job.values())
    for index, current in enumerate(lane_sets):
        for other in lane_sets[index + 1 :]:
            assert current.isdisjoint(other)
    lanes = set[str]().union(*lane_sets)
    if exhaustive:
        assert lanes == full
        assert inventory <= by_job["pytest-serial"]
    else:
        assert lanes.isdisjoint(inventory)
        assert lanes | inventory == full
    assert by_job["pytest-stress"] == stress

    package_nodeids = {nodeid for nodeid in full if nodeid.startswith("tests/test_packaging.py::")}
    assert by_job["package"] == package_nodeids
    assert package_nodeids.isdisjoint(by_job["pytest-ordinary"])
    assert package_nodeids.isdisjoint(by_job["pytest-serial"])
    assert full and len(full) > 4_000


def test_ci_triggers_and_serial_selection_are_pinned_exactly() -> None:
    """The four triggers, the daily schedule, and the serial expression are exact (#954).

    The exhaustive branch is chosen only by the exact ``schedule`` and
    ``workflow_dispatch`` event names; every other event takes the fast selection.
    """
    workflow = _workflow()
    assert workflow["on"] == _CI_TRIGGERS
    on_block = _mapping(workflow["on"])
    assert set(on_block) == {"pull_request", "push", "workflow_dispatch", "schedule"}
    assert on_block["schedule"] == [{"cron": "23 2 * * *"}]
    assert on_block["workflow_dispatch"] is None

    jobs = _mapping(workflow["jobs"])
    selection_steps = [
        (job_id, step)
        for job_id, job in jobs.items()
        for step in _steps(_mapping(job))
        if "SERIAL_SELECTION" in _mapping(step.get("env", {}))
    ]
    assert [job_id for job_id, _ in selection_steps] == ["pytest-serial"]
    _, serial_step = selection_steps[0]
    serial_env = _mapping(serial_step["env"])
    assert serial_env == {
        "COVERAGE_FILE": ".coverage.serial",
        "SERIAL_SELECTION": _SERIAL_SELECTION_EXPRESSION,
    }
    assert serial_step["run"] == _SERIAL_RUN
    workflow_text = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
    assert workflow_text.count("exhaustive_only") == 1
    assert workflow_text.count("SERIAL_SELECTION") == 2

    for event, expected in _SERIAL_SELECTION_BY_EVENT.items():
        serial_arguments = next(
            arguments
            for job_id, arguments, _ in _pytest_lanes(workflow, event)
            if job_id == "pytest-serial"
        )
        assert serial_arguments[serial_arguments.index("-m") + 1] == expected
    with pytest.raises(KeyError):
        _pytest_lanes(workflow, "pull_request_target")


def test_agents_md_describes_the_event_selected_serial_lane() -> None:
    """AGENTS.md names the event-selected lane and inventory; the full-gate rule stays."""
    agents_md = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "The serial lane is event-selected (#954)" in agents_md
    assert "`and not exhaustive_only`" in agents_md
    assert "closed `exhaustive_only` inventory" in agents_md
    assert "`tests/test_pytest_governance.py`" in agents_md
    assert "daily `schedule` (02:23 UTC)" in agents_md
    assert "manual `workflow_dispatch`" in agents_md
    assert (
        "tests are never run under `-n`. The full gate (`python -m pytest`, all markers,\n"
        "single process) remains mandatory before handback and before opening a PR.\n"
    ) in agents_md


def test_ci_lanes_are_bounded_parallel_and_write_unique_coverage_files() -> None:
    """CI lanes have fixed xdist bounds and preserve all coverage data files."""
    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    ordinary_runs = [
        step.get("run") for step in _steps(_mapping(jobs["pytest-ordinary"])) if "run" in step
    ]
    serial_runs = [
        step.get("run") for step in _steps(_mapping(jobs["pytest-serial"])) if "run" in step
    ]
    assert _ORDINARY_RUN in ordinary_runs
    assert _SERIAL_RUN in serial_runs
    for event in _SERIAL_SELECTION_BY_EVENT:
        event_lanes = _pytest_lanes(workflow, event)
        ordinary_arguments = next(
            arguments for job, arguments, _ in event_lanes if job == "pytest-ordinary"
        )
        assert ordinary_arguments.count("-n") == 1
        assert ordinary_arguments[ordinary_arguments.index("-n") + 1] == "4"
        assert ordinary_arguments[ordinary_arguments.index("--dist") + 1] == "worksteal"
        for job_id, arguments, _ in event_lanes:
            if job_id != "pytest-ordinary":
                assert "-n" not in arguments
                assert not any(argument.startswith("-n") for argument in arguments)
                assert "--numprocesses" not in arguments
    lanes = _pytest_lanes(workflow, "pull_request")

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
            if job_id == "pytest-ordinary":
                # #1039: the shard coverage file travels inside the fixed bundle.
                assert upload == _ORDINARY_BUNDLE_UPLOAD
                continue
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
    ordinary_shard_files = {
        coverage_file for job, _, coverage_file in lanes if job == "pytest-ordinary"
    }
    assert ordinary_shard_files == {".coverage.ordinary-shard-0", ".coverage.ordinary-shard-1"}
    shard_combine = next(
        step["run"]
        for step in _steps(coverage_job)
        if step.get("name") == "Combine ordinary shard coverage"
    )
    assert shard_combine == _ORDINARY_SHARD_COMBINE_RUN
    assert combined_files == (set(coverage_files) - ordinary_shard_files) | {".coverage.ordinary"}

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


def _check_ordinary_shard_wiring(workflow: dict[str, object], workflow_text: str) -> None:
    """Assert the exact #1039 ordinary-shard producer and coverage-job wiring."""
    jobs = _mapping(workflow["jobs"])
    ordinary = _mapping(jobs["pytest-ordinary"])
    assert set(ordinary) == {"name", "runs-on", "needs", "if", "strategy", "steps"}
    assert ordinary["runs-on"] == "ubuntu-latest"
    assert ordinary["needs"] == ["classify"]
    assert ordinary["if"] == _FULL_ONLY_CONDITION
    assert ordinary["strategy"] == {
        "fail-fast": False,
        "max-parallel": 2,
        "matrix": {"include": list(_ORDINARY_SHARD_MATRIX)},
    }
    ordinary_steps = _steps(ordinary)
    assert [step.get("name") for step in ordinary_steps[:4]] == [
        "Check out repository",
        "Set up Python",
        "Install system libraries (PortAudio for sounddevice)",
        "Install dependencies",
    ]
    assert ordinary_steps[4:] == list(_ORDINARY_SHARD_STEPS)

    coverage_job = _mapping(jobs["coverage"])
    assert coverage_job["needs"] == [
        "classify",
        "pytest-ordinary",
        "pytest-serial",
        "pytest-stress",
        "package",
    ]
    coverage_steps = _steps(coverage_job)
    assert [step.get("name") for step in coverage_steps[:3]] == [
        "Check out repository",
        "Set up Python",
        "Install coverage",
    ]
    assert coverage_steps[3:9] == list(_COVERAGE_SHARD_STEPS)
    assert [step.get("name") for step in coverage_steps[9:]] == [
        "Generate combined coverage XML",
        "Report combined coverage",
        "Normalize coverage filenames for Codecov",
        "Upload normalized full coverage artifact",
    ]

    ordinary_lanes = [
        (arguments, coverage_file)
        for job_id, arguments, coverage_file in _pytest_lanes(workflow, "pull_request")
        if job_id == "pytest-ordinary"
    ]
    assert [
        (arguments[arguments.index("--ordinary-shard-index") + 1], coverage_file)
        for arguments, coverage_file in ordinary_lanes
    ] == [("0", ".coverage.ordinary-shard-0"), ("1", ".coverage.ordinary-shard-1")]

    assert "coverage-ordinary" not in workflow_text
    assert "benchmark" not in workflow_text
    assert workflow_text.count("--identity-mode") == 2
    assert workflow_text.count("--identity-mode normal") == 2


def test_ordinary_shard_wiring_is_pinned_exactly() -> None:
    """#1039: two fixed shards, a verified bundle chain, and no local-gate change."""
    workflow_text = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
    _check_ordinary_shard_wiring(_workflow(), workflow_text)

    assert not (REPO_ROOT / "conftest.py").exists()
    addopts = cast(str, _pytest_options()["addopts"])
    assert "-p" not in addopts.split()
    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "ci_ordinary_shard" not in pyproject_text
    assert "ordinary-shard" not in pyproject_text


def _raw_steps(workflow: dict[str, object], job_id: str) -> list[dict[str, object]]:
    """Return a job's live (uncopied) step list for an in-memory mutant."""
    jobs = cast(dict[str, object], workflow["jobs"])
    return cast(list[dict[str, object]], cast(dict[str, object], jobs[job_id])["steps"])


def _raw_step(workflow: dict[str, object], job_id: str, name: str) -> dict[str, object]:
    """Return one named live step for an in-memory mutant."""
    return next(step for step in _raw_steps(workflow, job_id) if step.get("name") == name)


def _raw_with(workflow: dict[str, object], job_id: str, name: str) -> dict[str, object]:
    """Return one named step's live ``with`` mapping."""
    return cast(dict[str, object], _raw_step(workflow, job_id, name)["with"])


def _raw_strategy(workflow: dict[str, object]) -> dict[str, object]:
    """Return the ordinary job's live strategy mapping."""
    jobs = cast(dict[str, object], workflow["jobs"])
    return cast(dict[str, object], cast(dict[str, object], jobs["pytest-ordinary"])["strategy"])


def _raw_include(workflow: dict[str, object]) -> list[dict[str, object]]:
    """Return the ordinary job's live matrix include list."""
    matrix = cast(dict[str, object], _raw_strategy(workflow)["matrix"])
    return cast(list[dict[str, object]], matrix["include"])


def _replace_run(workflow: dict[str, object], job_id: str, name: str, old: str, new: str) -> None:
    """Rewrite part of one named step's run string, asserting the edit applies."""
    step = _raw_step(workflow, job_id, name)
    run = cast(str, step["run"])
    assert old in run
    step["run"] = run.replace(old, new)


def _move_step(workflow: dict[str, object], job_id: str, name: str, offset: int) -> None:
    """Move one named step by ``offset`` positions."""
    steps = _raw_steps(workflow, job_id)
    position = next(index for index, step in enumerate(steps) if step.get("name") == name)
    steps.insert(position + offset, steps.pop(position))


_RUN_STEP = "Run ordinary tests with coverage"
_UPLOAD_STEP = "Upload ordinary shard bundle"
_DOWNLOAD_ZERO = "Download ordinary shard 0 bundle"
_WIRING_MUTANTS: dict[str, Callable[[dict[str, object]], object]] = {
    # G28: matrix, xdist request, plugin, job scope.
    "matrix-third-index": lambda w: _raw_include(w).append({"index": "2"}),
    "matrix-integer-index": lambda w: _raw_include(w)[0].update({"index": 0}),
    "matrix-fail-fast": lambda w: _raw_strategy(w).update({"fail-fast": True}),
    "matrix-unbounded-parallel": lambda w: _raw_strategy(w).pop("max-parallel"),
    "pytest-without-shim": lambda w: _replace_run(
        w, "pytest-ordinary", _RUN_STEP, f"-p {_ORDINARY_SHARD_PLUGIN} ", ""
    ),
    "pytest-three-workers": lambda w: _replace_run(w, "pytest-ordinary", _RUN_STEP, "-n 4", "-n 3"),
    "job-permissions": lambda w: cast(
        dict[str, object], cast(dict[str, object], w["jobs"])["pytest-ordinary"]
    ).update({"permissions": {"contents": "write"}}),
    "job-runner-changed": lambda w: cast(
        dict[str, object], cast(dict[str, object], w["jobs"])["pytest-ordinary"]
    ).update({"runs-on": "ubuntu-24.04"}),
    # G29: upload mapping.
    "upload-no-overwrite": lambda w: _raw_with(w, "pytest-ordinary", _UPLOAD_STEP).update(
        {"overwrite": False}
    ),
    "upload-coverage-prefixed-name": lambda w: _raw_with(w, "pytest-ordinary", _UPLOAD_STEP).update(
        {"name": "coverage-shard-${{ matrix.index }}"}
    ),
    "upload-retention": lambda w: _raw_with(w, "pytest-ordinary", _UPLOAD_STEP).update(
        {"retention-days": 30}
    ),
    "upload-hidden-files": lambda w: _raw_with(w, "pytest-ordinary", _UPLOAD_STEP).update(
        {"include-hidden-files": True}
    ),
    # G30: downloads carry no run-id, pattern, merge-multiple or token.
    "download-run-id": lambda w: _raw_with(w, "coverage", _DOWNLOAD_ZERO).update(
        {"run-id": "${{ github.run_id }}"}
    ),
    "download-pattern": lambda w: _raw_with(w, "coverage", _DOWNLOAD_ZERO).update(
        {"pattern": "ordinary-shard-*"}
    ),
    "download-merge-multiple": lambda w: _raw_with(w, "coverage", _DOWNLOAD_ZERO).update(
        {"merge-multiple": True}
    ),
    "download-github-token": lambda w: _raw_with(w, "coverage", _DOWNLOAD_ZERO).update(
        {"github-token": "${{ github.token }}"}
    ),
    "coverage-download-widened": lambda w: _raw_with(
        w, "coverage", "Download coverage data"
    ).update({"pattern": "*"}),
    # G31: step order and presence.
    "verify-after-shard-combine": lambda w: _move_step(
        w, "coverage", "Verify ordinary shard bundles", 1
    ),
    "shard-download-before-coverage-download": lambda w: _move_step(
        w, "coverage", _DOWNLOAD_ZERO, -1
    ),
    "verify-removed": lambda w: _raw_steps(w, "coverage").remove(
        _raw_step(w, "coverage", "Verify ordinary shard bundles")
    ),
    "finalize-removed": lambda w: _raw_steps(w, "pytest-ordinary").remove(
        _raw_step(w, "pytest-ordinary", "Finalize ordinary shard bundle")
    ),
    # G32: the final combine stays byte-identical.
    "final-combine-edited": lambda w: _replace_run(
        w, "coverage", "Combine coverage data", " .coverage.package", ""
    ),
    # G33: identity mode pinned to normal; no benchmark or old upload literal.
    "finalize-benchmark-mode": lambda w: _replace_run(
        w,
        "pytest-ordinary",
        "Finalize ordinary shard bundle",
        "--identity-mode normal",
        "--identity-mode benchmark",
    ),
    "text-benchmark-literal": lambda _w: "\n# benchmark\n",
    "text-coverage-ordinary-literal": lambda _w: "\n# name: coverage-ordinary\n",
    "text-extra-identity-mode": lambda _w: "\n# --identity-mode normal\n",
}


@pytest.mark.parametrize("mutant", sorted(_WIRING_MUTANTS))
def test_ordinary_shard_wiring_checker_rejects_each_mutant(mutant: str) -> None:
    """Each in-memory workflow mutant actually fails the wiring checker (G28-G33)."""
    workflow = copy.deepcopy(_workflow())
    workflow_text = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
    _check_ordinary_shard_wiring(copy.deepcopy(workflow), workflow_text)
    appended = _WIRING_MUTANTS[mutant](workflow)
    if isinstance(appended, str):
        workflow_text += appended
    with pytest.raises(AssertionError):
        _check_ordinary_shard_wiring(workflow, workflow_text)


def test_collection_arguments_strip_only_the_closed_shard_options() -> None:
    """Only the exact shim pair and the two shard options are stripped (G34)."""
    arguments = [
        "-n",
        "4",
        "--dist",
        "worksteal",
        "-p",
        _ORDINARY_SHARD_PLUGIN,
        "--ordinary-shard-index",
        "1",
        "--ordinary-shard-receipt",
        "receipt.json",
        "-p",
        "no:randomly",
        "-p",
        "scripts.other_plugin",
        "--ordinary-shard-count",
        "2",
        "-m",
        "not serial",
        "--cov=scripts",
        "--cov-report=",
    ]
    assert _collection_arguments(arguments) == [
        "-p",
        "no:randomly",
        "-p",
        "scripts.other_plugin",
        "--ordinary-shard-count",
        "2",
        "-m",
        "not serial",
    ]
    assert _collection_arguments(["-p"]) == ["-p"]


def test_ordinary_shards_partition_the_cached_ordinary_universe() -> None:
    """The oracle splits the cached ordinary universe into two disjoint, complete shards.

    The CI helper agrees with the test-local oracle on every collected ID, and the
    universe fits the frozen v1 caps.
    """
    ordinary = [
        arguments
        for job_id, arguments, _ in _pytest_lanes(_workflow(), "pull_request")
        if job_id == "pytest-ordinary"
    ]
    assert len({tuple(_collection_arguments(arguments)) for arguments in ordinary}) == 1
    returncode, universe, output = _collect_nodeids(ordinary[0])
    assert returncode == 0, output
    zero, one = (_lane_shard_nodeids(arguments, universe) for arguments in ordinary)
    assert zero and one
    assert zero.isdisjoint(one)
    assert zero | one == universe
    assert all(ci_ordinary_shards.shard_index(nodeid) == 0 for nodeid in zero)
    assert all(ci_ordinary_shards.shard_index(nodeid) == 1 for nodeid in one)
    sizes = [len(nodeid.encode("utf-8", "strict")) for nodeid in universe]
    assert len(universe) <= ci_ordinary_shards.COUNT_CAP
    assert max(sizes) <= ci_ordinary_shards.NODEID_CAP
    # kept + three phase records per ID, with generous per-entry JSON overhead.
    assert 4 * sum(sizes) + 256 * len(universe) < ci_ordinary_shards.MANIFEST_CAP


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
def test_package_arm64_isolated_pi_smoke_is_structurally_fail_closed() -> None:
    """E11-S1 keeps the native ARM64 appliance smoke bounded and isolated."""
    workflow = _workflow()
    jobs = _mapping(workflow["jobs"])
    package = _mapping(jobs["package"])
    arm64 = _mapping(jobs["package-arm64"])

    assert package["needs"] == ["classify", "package-arm64"]
    package_runs = [cast(str, step["run"]) for step in _steps(package) if "run" in step]
    assert any("/tmp/wheel-pi-smoke-venv" in run for run in package_runs)
    assert any('"${wheel_path}[pi]"' in run for run in package_runs)
    assert any('version("coffee-roaster-mcp") == "0.2.4"' in run for run in package_runs)
    assert any('denied = {"torch", "torchaudio", "transformers"}' in run for run in package_runs)
    assert any('print("\\n".join(names))' in run for run in package_runs)
    assert any('distribution.metadata.get("Name")' in run for run in package_runs)
    assert any("valid = all(isinstance(name, str)" in run for run in package_runs)
    assert any("if valid else []" in run for run in package_runs)

    assert arm64["name"] == "Package ARM64 (wheel[pi] clean-venv smoke)"
    assert arm64["runs-on"] == "ubuntu-24.04-arm"
    assert arm64["needs"] == ["classify"]
    assert arm64["if"] == _FULL_ONLY_CONDITION
    assert arm64["timeout-minutes"] == 20
    assert "permissions" not in arm64

    steps = _steps(arm64)
    action_uses = [step["uses"] for step in steps if "uses" in step]
    assert action_uses == [_CHECKOUT_PIN, "actions/setup-python@v6.2.0", "actions/setup-node@v4"]
    runs = [cast(str, step["run"]) for step in steps if "run" in step]
    assert all("--group dev" not in run or "[pi]" not in run for run in runs)
    assert all("continue-on-error" not in run for run in runs)
    assert all("|| true" not in run for run in runs)
    assert all("always()" not in run for run in runs)
    assert all("retry" not in run.lower() for run in runs)
    assert all("source-build" not in run for run in runs)
    assert any("import platform; actual = platform.machine()" in run for run in runs)
    assert any('actual == "aarch64"' in run for run in runs)
    assert any('version("coffee-roaster-mcp") == "0.2.4"' in run for run in runs)
    assert any('denied = {"torch", "torchaudio", "transformers"}' in run for run in runs)
    assert any('print("\\n".join(names))' in run for run in runs)
    assert any('distribution.metadata.get("Name")' in run for run in runs)
    assert any("valid = all(isinstance(name, str)" in run for run in runs)
    assert any("if valid else []" in run for run in runs)
    assert any("--port 18235" in run and "--replay" in run for run in runs)

    all_runs = [
        cast(str, step["run"])
        for job in jobs.values()
        for step in _steps(_mapping(job))
        if "run" in step
    ]
    assert all("--group dev" not in run or "[pi]" not in run for run in all_runs)


@pytest.mark.docs
def test_e11_packaging_status_is_current_and_honest() -> None:
    """E11 records package delivery without claiming Pi hardware validation."""
    epic = (REPO_ROOT / "docs/epics/E11-packaging.md").read_text(encoding="utf-8")
    registry = (REPO_ROOT / "docs/state/registry.md").read_text(encoding="utf-8")
    assert "E11-S1 | Wheel with bundled SPA + the `[pi]` extra | done" in epic
    assert "E11-S2 | Native installer, systemd unit, bundled model, deploy doc | done" in epic
    assert (
        "E11-S3 | Pi 5 single-primary-mic complete-appliance cold characterisation "
        "(overflow validation) | in progress" in epic
    )
    assert "Hosted ARM64 evidence is package compatibility" in epic
    assert "not Raspberry Pi hardware validation" in registry
    assert "does not state that `coffee-roaster-mcp#157` or #194 is\nclosed" in registry
    assert "published torch-free\n> `coffee-roaster-mcp==0.2.0` release" in epic
    assert "E11-S1's D27 dependency/publication\n  gate is ✅ CLEARED" in registry
    assert "Still gated on D27 (independent)" not in epic
    assert "do not pin/ship\n> the `[pi]` extra until that lands" not in epic
    assert "gated only on D27 (torch-free chain)" not in registry


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
    assert "docs-fastpath" not in {
        job_id for job_id, _, _ in _pytest_lanes(workflow, "pull_request")
    }

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
