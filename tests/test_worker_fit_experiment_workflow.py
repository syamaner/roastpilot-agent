"""Behavioural contract for the temporary #1048 worker-fit experiment workflow.

These tests run locally only: the experiment's six trials check out the pinned
original payload ``H``, which contains neither this module nor the workflow.
They pin the closed workflow shape (admission, chain, production fidelity,
evidence steps) and exercise the inline ``WORKER_FIT_TOOL`` producer/checker in
process against ``tmp_path`` fixtures and an injected fake host.
"""

from __future__ import annotations

import inspect
import json
import re
import shlex
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import cast

import coverage
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github/workflows/worker-fit-experiment.yml"
CI_PATH = REPO_ROOT / ".github/workflows/ci.yml"

BRANCH = "feature/1048-ci-worker-fit-experiment"
SOURCE_SHA = "a9cd3997c5b866db3c60d74f126e16ad191d31ec"
SOURCE_TREE = "a850a89e5c124a1ee4cda9dfea3d26f25f017db2"
CHECKOUT_ACTION = "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"
JOBS = ["t1", "t2", "t3", "t4", "t5", "t6"]
WORKERS = [4, 2, 2, 4, 4, 2]
ADMISSION = (
    "github.repository == 'syamaner/roastpilot-agent' && github.event_name == 'push' && "
    "github.ref == 'refs/heads/feature/1048-ci-worker-fit-experiment' && "
    "github.event.created == true && github.run_attempt == '1'"
)
NOT_CANCELLED = "${{ !cancelled() }}"
JUNIT_FLAG = "--junitxml=$RUNNER_TEMP/worker-fit/junit-raw.xml"
TOOL_CALL = 'python -c "$WORKER_FIT_TOOL"'
GATING = [
    "source_sha",
    "source_tree",
    "image_os",
    "image_version",
    "python_version",
    "machine",
    "cpu_count",
    "affinity_count",
    "cgroup_cpu_max",
    "cgroup_memory_max",
    "mem_total_gib",
    "pip_freeze_sha256",
]
CGROUP_FIELDS = {"cgroup_cpu_max", "cgroup_memory_max"}
RECORD_KEYS = {*GATING, "schema", "workers", "kernel_release", "cpu_model", "snapshot"}
SNAPSHOT_KEYS = {
    "wall_time",
    "loadavg",
    "mem_available_kib",
    "cpu_usage_usec",
    "cpu_nr_throttled",
    "cpu_throttled_usec",
    "memory_current",
    "memory_peak",
}
SEALED = {
    ".coverage.ordinary",
    "environment-after.json",
    "environment-before.json",
    "junit.xml",
    "pip-freeze.txt",
    "pytest-exit.txt",
}
MIB = 1024 * 1024

ToolMain = Callable[..., int]


def _mapping(value: object) -> dict[str, object]:
    """Narrow an untyped YAML or JSON object to a string-keyed mapping."""
    assert isinstance(value, dict)
    mapping = cast(dict[object, object], value)
    assert all(isinstance(key, str) for key in mapping)
    return {cast(str, key): item for key, item in mapping.items()}


def _sequence(value: object) -> list[object]:
    """Narrow an untyped YAML object to a list."""
    assert isinstance(value, list)
    return cast(list[object], value)


def _load(path: Path) -> dict[str, object]:
    """Load a workflow, normalising PyYAML's boolean ``on`` key."""
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    raw = cast(dict[object, object], loaded)
    return _mapping({"on" if key is True else key: item for key, item in raw.items()})


def _workflow() -> dict[str, object]:
    """Return the experiment workflow."""
    return _load(WORKFLOW_PATH)


def _jobs() -> dict[str, dict[str, object]]:
    """Return the experiment jobs in declaration order."""
    return {name: _mapping(job) for name, job in _mapping(_workflow()["jobs"]).items()}


def _steps(job: Mapping[str, object]) -> list[dict[str, object]]:
    """Return one job's steps as mappings."""
    return [_mapping(step) for step in _sequence(job["steps"])]


def _step(job: Mapping[str, object], name: str) -> dict[str, object]:
    """Return the single step with ``name``."""
    matches = [step for step in _steps(job) if step.get("name") == name]
    assert len(matches) == 1, name
    return matches[0]


def _ci_ordinary_steps() -> list[dict[str, object]]:
    """Return the production ci.yml ``pytest-ordinary`` steps."""
    return _steps(_mapping(_mapping(_load(CI_PATH)["jobs"])["pytest-ordinary"]))


def _partner(index: int) -> tuple[int, str, str] | None:
    """Return (partner job number, baseline dir, candidate dir) for a pair job."""
    return {
        2: (1, "prior/pair", "out"),
        4: (3, "out", "prior/pair"),
        6: (5, "prior/pair", "out"),
    }.get(index)


def _expected_step_names(index: int) -> list[str]:
    """Return the closed ordered step-name list for job ``t{index}``."""
    names = [
        "Check out original source payload",
        "Verify source payload",
        "Set up Python",
        "Install system libraries (PortAudio for sounddevice)",
        "Install dependencies",
        "Record environment before tests",
        "Run ordinary tests with coverage",
        "Finalise evidence",
        "Upload evidence",
        "Check sealed evidence",
    ]
    if index > 1:
        names += ["Download t1 evidence", "Check environment against t1"]
    if _partner(index) is not None:
        names += ["Download pair partner evidence", "Check pair"]
    return names


# --- T1-T8: closed workflow shape -------------------------------------------------


def test_t1_top_level_trigger_permissions_and_concurrency_are_closed() -> None:
    """T1: only a push to the exact branch admits; read-only token; no cancellation."""
    workflow = _workflow()
    assert list(workflow) == ["name", "on", "permissions", "concurrency", "env", "jobs"]
    assert workflow["on"] == {"push": {"branches": [BRANCH]}}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "worker-fit-experiment-1048",
        "cancel-in-progress": False,
    }
    assert _mapping(workflow["concurrency"])["cancel-in-progress"] is False
    env = _mapping(workflow["env"])
    assert list(env) == ["WORKER_FIT_TOOL"]
    assert isinstance(env["WORKER_FIT_TOOL"], str)
    assert "${{" not in env["WORKER_FIT_TOOL"]


def test_t2_exactly_six_jobs_in_a_fixed_needs_chain_with_closed_keys() -> None:
    """T2: t1..t6 in order, each needing only its predecessor, 30-minute cap."""
    jobs = _jobs()
    assert list(jobs) == JOBS
    for index, (name, job) in enumerate(jobs.items(), start=1):
        expected_keys = {"name", "runs-on", "timeout-minutes", "if", "steps"}
        if index == 1:
            assert "needs" not in job
        else:
            expected_keys.add("needs")
            assert job["needs"] == [f"t{index - 1}"], name
        assert set(job) == expected_keys, name
        assert job["runs-on"] == "ubuntu-latest", name
        assert job["timeout-minutes"] == 30, name
        assert type(job["timeout-minutes"]) is int, name


def test_t3_every_job_repeats_the_exact_admission_guard_without_status_functions() -> None:
    """T3: identical five-clause admission literal; implicit success() keeps the chain."""
    for name, job in _jobs().items():
        guard = job["if"]
        assert isinstance(guard, str), name
        assert " ".join(guard.split()) == ADMISSION, name
        assert not re.search(r"\b(always|failure|cancelled|success)\s*\(", guard), name


def test_t4_raw_text_has_no_secret_token_codecov_or_extra_trigger_material() -> None:
    """T4: no secret/token reference, Codecov, continue-on-error or other event."""
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "secrets.",
        "github.token",
        "GITHUB_TOKEN",
        "continue-on-error",
        "workflow_call",
        "pull_request",
        "workflow_dispatch",
        "schedule",
        "|| true",
    ):
        assert forbidden not in text, forbidden
    assert "codecov" not in text.lower()
    expressions = {" ".join(body.split()) for body in re.findall(r"\$\{\{(.*?)\}\}", text)}
    assert expressions == {"!cancelled()", "runner.temp"}


def test_t5_checkout_pins_the_original_payload_and_verify_step_proves_it() -> None:
    """T5: checkout is exactly H without persisted credentials; verify asserts H/T/absence."""
    expected_verify = "\n".join(
        [
            "set -euo pipefail",
            'head_sha="$(git rev-parse HEAD)"',
            f'test "$head_sha" = "{SOURCE_SHA}"',
            "head_tree=\"$(git rev-parse 'HEAD^{tree}')\"",
            f'test "$head_tree" = "{SOURCE_TREE}"',
            'worktree_status="$(git status --porcelain --ignored)"',
            'test -z "$worktree_status"',
            "test ! -e tests/test_worker_fit_experiment_workflow.py",
            "test ! -e .github/workflows/worker-fit-experiment.yml",
            "",
        ]
    )
    for name, job in _jobs().items():
        steps = _steps(job)
        assert steps[0] == {
            "name": "Check out original source payload",
            "uses": CHECKOUT_ACTION,
            "with": {"ref": SOURCE_SHA, "persist-credentials": False},
        }, name
        assert _mapping(steps[0]["with"])["persist-credentials"] is False, name
        assert steps[1] == {"name": "Verify source payload", "run": expected_verify}, name


def test_t6_setup_steps_equal_the_production_ordinary_job() -> None:
    """T6: setup-python, PortAudio and install dicts equal ci.yml pytest-ordinary."""
    production = _ci_ordinary_steps()
    assert [step["name"] for step in production[1:4]] == [
        "Set up Python",
        "Install system libraries (PortAudio for sounddevice)",
        "Install dependencies",
    ]
    for name, job in _jobs().items():
        assert _steps(job)[2:5] == production[1:4], name


def test_t7_test_step_is_production_argv_with_only_workers_and_junit_changed() -> None:
    """T7: shlex argv equals production except ``-n``; the real exit status is returned."""
    production = _step(
        _mapping(_mapping(_load(CI_PATH)["jobs"])["pytest-ordinary"]),
        ("Run ordinary tests with coverage"),
    )
    production_argv = shlex.split(cast(str, production["run"]))
    worker_index = production_argv.index("-n") + 1
    assert production_argv[worker_index] == "4"
    observed_workers: list[int] = []
    for name, job in _jobs().items():
        step = _step(job, "Run ordinary tests with coverage")
        assert set(step) == {"name", "env", "run"}, name
        assert step["env"] == production["env"] == {"COVERAGE_FILE": ".coverage.ordinary"}, name
        script = cast(str, step["run"])
        assert "|| true" not in script, name
        lines = script.splitlines()
        assert len(lines) == 5, name
        argv = shlex.split(lines[1])
        assert len(argv) == len(production_argv) + 1, name
        assert argv[worker_index - 1] == "-n", name
        assert argv[worker_index] in {"2", "4"}, name
        workers = int(argv[worker_index])
        observed_workers.append(workers)
        expected = [*production_argv, JUNIT_FLAG]
        expected[worker_index] = str(workers)
        assert argv == expected, name
        assert lines == [
            "set +e",
            lines[1],
            "status=$?",
            'printf \'%s\\n\' "$status" > "$RUNNER_TEMP/worker-fit/out/pytest-exit.txt"',
            'exit "$status"',
        ], name
    assert observed_workers == WORKERS
    for index, job in enumerate(_jobs().values(), start=1):
        record = _step(job, "Record environment before tests")
        assert record == {
            "name": "Record environment before tests",
            "run": f"{TOOL_CALL} record-before {WORKERS[index - 1]}",
        }


def test_t8_evidence_steps_conditions_and_partners_are_closed() -> None:
    """T8: only finalise/upload run after failure; uploads and partner downloads exact."""
    for index, (name, job) in enumerate(_jobs().items(), start=1):
        steps = _steps(job)
        assert [step["name"] for step in steps] == _expected_step_names(index), name
        for step in steps:
            assert set(step) <= {"name", "uses", "with", "run", "env", "if"}, name
            if step["name"] in {"Finalise evidence", "Upload evidence"}:
                assert step["if"] == NOT_CANCELLED, name
            else:
                assert "if" not in step, (name, step["name"])
            if "env" in step:
                assert step["name"] == "Run ordinary tests with coverage", name
        assert _step(job, "Finalise evidence")["run"] == "\n".join(
            [
                "set -uo pipefail",
                "status=0",
                f"{TOOL_CALL} record-after || status=1",
                'cp .coverage.ordinary "$RUNNER_TEMP/worker-fit/out/.coverage.ordinary"'
                " || status=1",
                f"{TOOL_CALL} sanitise || status=1",
                f"{TOOL_CALL} seal || status=1",
                'exit "$status"',
                "",
            ]
        )
        assert _step(job, "Upload evidence") == {
            "name": "Upload evidence",
            "if": NOT_CANCELLED,
            "uses": "actions/upload-artifact@v4",
            "with": {
                "name": f"worker-fit-{name}",
                "path": "${{ runner.temp }}/worker-fit/out",
                "include-hidden-files": True,
                "if-no-files-found": "error",
                "retention-days": 14,
            },
        }, name
        assert _step(job, "Check sealed evidence")["run"] == f"{TOOL_CALL} check-evidence"
        if index > 1:
            assert _step(job, "Download t1 evidence") == {
                "name": "Download t1 evidence",
                "uses": "actions/download-artifact@v4",
                "with": {"name": "worker-fit-t1", "path": "${{ runner.temp }}/worker-fit/prior/t1"},
            }, name
            assert _step(job, "Check environment against t1")["run"] == (
                f'{TOOL_CALL} check-env "$RUNNER_TEMP/worker-fit/prior/t1" '
                '"$RUNNER_TEMP/worker-fit/out"'
            ), name
        partner = _partner(index)
        if partner is not None:
            partner_job, baseline, candidate = partner
            assert _step(job, "Download pair partner evidence") == {
                "name": "Download pair partner evidence",
                "uses": "actions/download-artifact@v4",
                "with": {
                    "name": f"worker-fit-t{partner_job}",
                    "path": "${{ runner.temp }}/worker-fit/prior/pair",
                },
            }, name
            assert _step(job, "Check pair")["run"] == (
                f'{TOOL_CALL} check-pair "$RUNNER_TEMP/worker-fit/{baseline}" '
                f'"$RUNNER_TEMP/worker-fit/{candidate}"'
            ), name
            assert WORKERS[index - 1] == (4 if baseline == "out" else 2), name
            assert WORKERS[partner_job - 1] == (2 if baseline == "out" else 4), name


# --- Inline tool harness ----------------------------------------------------------

CGROUP = "/sys/fs/cgroup/system.slice/runner-provisioner.service"
FAKE_FILES = {
    "/proc/self/cgroup": "0::/system.slice/runner-provisioner.service\n",
    f"{CGROUP}/cpu.max": "max 100000\n",
    f"{CGROUP}/memory.max": "max\n",
    f"{CGROUP}/cpu.stat": "usage_usec 10\nuser_usec 6\nnr_throttled 0\nthrottled_usec 0\n",
    f"{CGROUP}/memory.current": "4096\n",
    "/proc/meminfo": "MemTotal:       16777216 kB\nMemAvailable:    8000000 kB\n",
    "/proc/loadavg": "0.10 0.20 0.30 1/100 42\n",
    "/proc/cpuinfo": "processor\t: 0\nmodel name\t: Fake CPU 3000\n",
}
FREEZE = "coverage==7.16.2\npytest==9.1.1\n"
HOST_SECRET_VALUES = ("runner-host-0042", "GitHub Actions 7", "10.1.2.3")

PASS_A = '<testcase classname="tests.test_a" name="test_one" time="0.1" />'
DUP = '<testcase classname="tests.test_a" name="test_dup" time="0.1" />'
SKIP_C = (
    '<testcase classname="tests.test_c" name="test_skip" time="0.0">'
    '<skipped type="pytest.skip" message="needs hardware">tests/test_c.py:3: needs hardware'
    "</skipped></testcase>"
)
OUT_D = (
    '<testcase classname="tests.test_d" name="test_out" time="0.2" hostname="runner-host-0042">'
    "<system-out>hello</system-out></testcase>"
)
FAIL_A = (
    '<testcase classname="tests.test_a" name="test_one" time="0.1">'
    '<failure message="boom">trace</failure></testcase>'
)
SKIP_A = (
    '<testcase classname="tests.test_a" name="test_one" time="0.0">'
    '<skipped type="pytest.skip" message="later">x</skipped></testcase>'
)
NEW_D = '<testcase classname="tests.test_d" name="test_new" time="0.1" />'
BASE_CASES = [PASS_A, DUP, DUP, SKIP_C, OUT_D]
BASE_ARCS: dict[str, list[tuple[int, int]]] = {
    "src/roastpilot_agent/alpha.py": [(-1, 1), (1, 2), (2, -1)],
    "scripts/beta.py": [(-1, 1), (1, -1)],
    ".agents/skills/capture-agent-usage/scripts/gamma.py": [(-1, 1), (1, -1)],
}


def _junit(cases: list[str]) -> str:
    """Return a pytest-shaped JUnit document carrying host attributes to strip."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites name="pytest tests" hostname="runner-host-0042">'
        '<testsuite name="pytest" errors="0" failures="0" skipped="1" tests="5" '
        'time="1.234" timestamp="2026-10-21T06:00:00.000000+00:00" '
        'hostname="runner-host-0042">' + "".join(cases) + "</testsuite></testsuites>"
    )


def _exec_tool(name: str) -> dict[str, object]:
    """Execute the workflow's inline tool source under ``__name__ == name``.

    ``dont_inherit=True`` stops this module's ``from __future__ import
    annotations`` leaking into the tool, so it compiles exactly as under
    ``python -c`` on the runner (annotations evaluated, not postponed strings).
    """
    source = _mapping(_workflow()["env"])["WORKER_FIT_TOOL"]
    assert isinstance(source, str)
    namespace: dict[str, object] = {"__name__": name}
    exec(compile(source, "worker_fit_tool", "exec", dont_inherit=True), namespace)
    return namespace


@pytest.fixture(scope="module")
def tool() -> ToolMain:
    """Execute the workflow's inline tool source and return its ``main``."""
    source = _mapping(_workflow()["env"])["WORKER_FIT_TOOL"]
    assert isinstance(source, str)
    namespace: dict[str, object] = {"__name__": "worker_fit_tool"}
    exec(compile(source, "worker_fit_tool", "exec"), namespace)
    return cast(ToolMain, namespace["main"])


def _reader(
    files: Mapping[str, str], errors: Mapping[str, OSError] | None = None
) -> Callable[[str], str]:
    """Return a fake host file reader; unknown paths raise FileNotFoundError (ENOENT)."""

    def read(path: str) -> str:
        if errors is not None and path in errors:
            raise errors[path]
        if path not in files:
            raise FileNotFoundError(path)
        return files[path]

    return read


def _run(argv: list[str]) -> str:
    """Return fake output for the tool's only commands (git rev-parse, pip freeze)."""
    if argv == ["git", "rev-parse", "HEAD"]:
        return SOURCE_SHA + "\n"
    if argv == ["git", "rev-parse", "HEAD^{tree}"]:
        return SOURCE_TREE + "\n"
    assert argv[1:] == ["-m", "pip", "freeze", "--all", "--exclude-editable"], argv
    return FREEZE


def _host(
    runner_temp: Path,
    files: Mapping[str, str] = FAKE_FILES,
    errors: Mapping[str, OSError] | None = None,
) -> dict[str, object]:
    """Return keyword arguments that bind ``main`` to a fake host."""
    return {
        "environ": {
            "RUNNER_TEMP": str(runner_temp),
            "ImageOS": "ubuntu24",
            "ImageVersion": "20261019.1.0",
            "HOSTNAME": HOST_SECRET_VALUES[0],
            "RUNNER_NAME": HOST_SECRET_VALUES[1],
            "RUNNER_IP": HOST_SECRET_VALUES[2],
        },
        "read": _reader(files, errors),
        "run": _run,
        "affinity": lambda: 4,
    }


def _write_coverage(
    path: Path,
    arcs: Mapping[str, list[tuple[int, int]]] | None = None,
    lines: Mapping[str, list[int]] | None = None,
) -> None:
    """Write a native coverage data file with arcs (or lines only)."""
    data = coverage.CoverageData(basename=str(path))
    if arcs is not None:
        data.add_arcs({name: set(pairs) for name, pairs in arcs.items()})
    if lines is not None:
        data.add_lines({name: set(numbers) for name, numbers in lines.items()})
    data.write()


def _build(
    tool: ToolMain,
    runner_temp: Path,
    workers: int,
    *,
    cases: list[str] | None = None,
    arcs: Mapping[str, list[tuple[int, int]]] | None = None,
    files: Mapping[str, str] = FAKE_FILES,
    exit_text: str = "0\n",
    seal: bool = True,
) -> Path:
    """Produce one job's evidence directory through the tool's own modes."""
    host = _host(runner_temp, files)
    assert tool(["record-before", str(workers)], **host) == 0
    out = runner_temp / "worker-fit" / "out"
    junit = _junit(BASE_CASES if cases is None else cases)
    (runner_temp / "worker-fit" / "junit-raw.xml").write_text(junit, encoding="utf-8")
    (out / "pytest-exit.txt").write_text(exit_text, encoding="utf-8")
    _write_coverage(out / ".coverage.ordinary", BASE_ARCS if arcs is None else arcs)
    assert tool(["record-after"], **host) == 0
    assert tool(["sanitise"], **host) == 0
    if seal:
        assert tool(["seal"], **host) == 0
    return out


def _reseal(tool: ToolMain, out: Path) -> None:
    """Re-seal an evidence directory after a deliberate fixture edit."""
    (out / "SHA256SUMS").unlink()
    assert tool(["seal"], **_host(out.parent.parent)) == 0


def _edit_records(out: Path, edit: Callable[[dict[str, object]], None]) -> None:
    """Apply ``edit`` to both environment records in ``out``."""
    for name in ("environment-before.json", "environment-after.json"):
        record = _mapping(json.loads((out / name).read_text(encoding="utf-8")))
        edit(record)
        (out / name).write_text(json.dumps(record), encoding="utf-8")


def _failure(capsys: pytest.CaptureFixture[str]) -> str:
    """Return the tool's captured stderr."""
    return capsys.readouterr().err


# --- T9: sanitise -----------------------------------------------------------------


def test_t9_sanitise_strips_every_hostname_and_keeps_cases(tool: ToolMain, tmp_path: Path) -> None:
    """T9: no hostname attribute survives; the raw file stays outside out/."""
    out = _build(tool, tmp_path, 4, seal=False)
    text = (out / "junit.xml").read_text(encoding="utf-8")
    assert "hostname" not in text
    assert "runner-host-0042" not in text
    assert text.count("<testcase ") == len(BASE_CASES)
    assert "needs hardware" in text
    assert not (out / "junit-raw.xml").exists()


def test_t9_sanitise_rejects_oversize_and_malformed_raw_junit(
    tool: ToolMain, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T9: the 32 MiB cap and XML parsing fail closed without writing junit.xml."""
    host = _host(tmp_path)
    assert tool(["record-before", "4"], **host) == 0
    raw = tmp_path / "worker-fit" / "junit-raw.xml"
    with raw.open("wb") as handle:
        handle.truncate(32 * MIB + 1)
    assert tool(["sanitise"], **host) == 1
    assert "raw JUnit file exceeds the size cap" in _failure(capsys)
    raw.write_text("<testsuites><testsuite>", encoding="utf-8")
    assert tool(["sanitise"], **host) == 1
    assert "ParseError" in _failure(capsys)
    raw.unlink()
    assert tool(["sanitise"], **host) == 1
    assert "FileNotFoundError" in _failure(capsys)
    assert not (tmp_path / "worker-fit" / "out" / "junit.xml").exists()


def test_t9_sanitise_preserves_every_other_attribute_text_order_and_case(
    tool: ToolMain, tmp_path: Path
) -> None:
    """T9: only ``hostname`` is removed; attributes, text, order and cases survive."""
    out = _build(tool, tmp_path, 4, seal=False)
    raw_path = tmp_path / "worker-fit" / "junit-raw.xml"
    raw = list(ET.parse(raw_path).getroot().iter())
    clean = list(ET.parse(out / "junit.xml").getroot().iter())
    assert [element.tag for element in clean] == [element.tag for element in raw]
    assert sum("hostname" in element.attrib for element in raw) == 3
    for before, after in zip(raw, clean, strict=True):
        expected = {key: value for key, value in before.attrib.items() if key != "hostname"}
        assert after.attrib == expected, before.tag
        assert after.text == before.text, before.tag
        assert after.tail == before.tail, before.tag
    suite = clean[1]
    assert suite.tag == "testsuite"
    assert suite.get("time") == "1.234"
    assert suite.get("timestamp") == "2026-10-21T06:00:00.000000+00:00"
    assert [case.get("time") for case in clean if case.tag == "testcase"] == [
        "0.1",
        "0.1",
        "0.1",
        "0.0",
        "0.2",
    ]
    cases = cast(Callable[[Path], object], _exec_tool("worker_fit_tool")["_cases"])
    assert cases(out / "junit.xml") == cases(raw_path)


def test_t9_sanitise_refuses_an_existing_target(
    tool: ToolMain, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T9: sanitise never overwrites an existing ``out/junit.xml``."""
    out = _build(tool, tmp_path, 4, seal=False)
    before = (out / "junit.xml").read_bytes()
    capsys.readouterr()
    assert tool(["sanitise"], **_host(tmp_path)) == 1
    assert "sanitised JUnit file already exists" in _failure(capsys)
    assert (out / "junit.xml").read_bytes() == before


# --- T10: seal and check-evidence -------------------------------------------------


def test_t10_complete_evidence_seals_and_checks(tool: ToolMain, tmp_path: Path) -> None:
    """T10: the closed set seals into SHA256SUMS and check-evidence passes."""
    out = _build(tool, tmp_path, 4)
    sums = (out / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert sorted(line.split("  ")[1] for line in sums) == sorted(SEALED)
    assert tool(["check-evidence"], **_host(tmp_path)) == 0


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("missing", "missing evidence file junit.xml"),
        ("extra", "unexpected evidence file stray.txt"),
        ("oversize", "evidence exceeds the total size cap"),
    ],
)
def test_t10_seal_rejects_incomplete_extra_or_oversize_sets(
    tool: ToolMain,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutation: str,
    reason: str,
) -> None:
    """T10: seal refuses anything but the closed file set within 64 MiB."""
    out = _build(tool, tmp_path, 4, seal=False)
    if mutation == "missing":
        (out / "junit.xml").unlink()
    elif mutation == "extra":
        (out / "stray.txt").write_text("x", encoding="utf-8")
    else:
        with (out / "pip-freeze.txt").open("ab") as handle:
            handle.truncate(64 * MIB + 1)
    capsys.readouterr()
    assert tool(["seal"], **_host(tmp_path)) == 1
    assert reason in _failure(capsys)
    assert not (out / "SHA256SUMS").exists()


def _tamper_hash(out: Path) -> None:
    (out / "pip-freeze.txt").write_text("tampered\n", encoding="utf-8")


def _tamper_extra(out: Path) -> None:
    (out / "stray.txt").write_text("x", encoding="utf-8")


def _tamper_missing(out: Path) -> None:
    (out / "environment-after.json").unlink()


def _tamper_oversize(out: Path) -> None:
    with (out / "junit.xml").open("ab") as handle:
        handle.truncate(64 * MIB + 1)


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        (_tamper_hash, "hash mismatch for pip-freeze.txt"),
        (_tamper_extra, "unexpected evidence file stray.txt"),
        (_tamper_missing, "missing evidence file environment-after.json"),
        (_tamper_oversize, "evidence exceeds the total size cap"),
    ],
    ids=["hash", "extra", "missing", "oversize"],
)
def test_t10_check_evidence_rejects_tampered_sealed_sets(
    tool: ToolMain,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    tamper: Callable[[Path], None],
    reason: str,
) -> None:
    """T10: any post-seal change to the closed set fails check-evidence."""
    out = _build(tool, tmp_path, 4)
    tamper(out)
    capsys.readouterr()
    assert tool(["check-evidence"], **_host(tmp_path)) == 1
    assert reason in _failure(capsys)


@pytest.mark.parametrize("exit_text", ["1\n", "0", "", "5\n"])
def test_t10_check_evidence_requires_a_zero_pytest_exit(
    tool: ToolMain, tmp_path: Path, capsys: pytest.CaptureFixture[str], exit_text: str
) -> None:
    """T10: a sealed nonzero or malformed pytest exit record fails the job."""
    _build(tool, tmp_path, 4, exit_text=exit_text)
    capsys.readouterr()
    assert tool(["check-evidence"], **_host(tmp_path)) == 1
    assert "pytest exit status is not 0" in _failure(capsys)


def test_t10_check_evidence_rejects_in_job_environment_drift(
    tool: ToolMain, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T10/AC6: the after record must match the before record on gating fields."""
    out = _build(tool, tmp_path, 4)
    after = _mapping(json.loads((out / "environment-after.json").read_text(encoding="utf-8")))
    after["pip_freeze_sha256"] = "0" * 64
    (out / "environment-after.json").write_text(json.dumps(after), encoding="utf-8")
    _reseal(tool, out)
    capsys.readouterr()
    assert tool(["check-evidence"], **_host(tmp_path)) == 1
    assert "before/after: gating field differs: pip_freeze_sha256" in _failure(capsys)


def _record_not_mapping(out: Path) -> None:
    for name in ("environment-before.json", "environment-after.json"):
        (out / name).write_text("[]", encoding="utf-8")


def _record_schema_two(out: Path) -> None:
    _edit_records(out, lambda record: record.update(schema=2))


def _record_workers_three(out: Path) -> None:
    _edit_records(out, lambda record: record.update(workers=3))


def _record_snapshot_key_missing(out: Path) -> None:
    def edit(record: dict[str, object]) -> None:
        snapshot = _mapping(record["snapshot"])
        del snapshot["memory_peak"]
        record["snapshot"] = snapshot

    _edit_records(out, edit)


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        (_record_not_mapping, "environment record keys differ from the schema"),
        (_record_schema_two, "environment record schema is not 1"),
        (_record_workers_three, "environment record workers is not 2 or 4"),
        (_record_snapshot_key_missing, "environment snapshot keys differ from the schema"),
    ],
    ids=["not-mapping", "schema-2", "workers-3", "snapshot-keys"],
)
def test_t10_check_evidence_rejects_invalid_environment_records(
    tool: ToolMain,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    edit: Callable[[Path], None],
    reason: str,
) -> None:
    """T10/AC6: a sealed record outside the closed schema fails check-evidence."""
    out = _build(tool, tmp_path, 4)
    edit(out)
    _reseal(tool, out)
    capsys.readouterr()
    assert tool(["check-evidence"], **_host(tmp_path)) == 1
    assert reason in _failure(capsys)


@pytest.mark.parametrize("damage", ["truncated", "malformed"])
def test_t10_check_evidence_rejects_a_truncated_or_malformed_sums_file(
    tool: ToolMain, tmp_path: Path, capsys: pytest.CaptureFixture[str], damage: str
) -> None:
    """T10: SHA256SUMS must list exactly the closed set as ``digest  name`` lines."""
    out = _build(tool, tmp_path, 4)
    lines = (out / "SHA256SUMS").read_text(encoding="utf-8").splitlines(keepends=True)
    if damage == "truncated":
        lines = lines[:-1]
    else:
        lines[0] = lines[0].replace("  ", " ", 1)
    (out / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")
    capsys.readouterr()
    assert tool(["check-evidence"], **_host(tmp_path)) == 1
    assert "SHA256SUMS does not list the closed file set" in _failure(capsys)


@pytest.mark.parametrize("mode", ["seal", "check-evidence"])
def test_t10_seal_and_check_evidence_reject_a_non_regular_entry(
    tool: ToolMain, tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    """T10: a symlinked evidence entry is refused even when its target content matches."""
    out = _build(tool, tmp_path / "rt", 4, seal=mode == "check-evidence")
    target = tmp_path / "outside-pip-freeze.txt"
    target.write_bytes((out / "pip-freeze.txt").read_bytes())
    (out / "pip-freeze.txt").unlink()
    (out / "pip-freeze.txt").symlink_to(target)
    capsys.readouterr()
    assert tool([mode], **_host(tmp_path / "rt")) == 1
    assert "evidence entry is not a regular file pip-freeze.txt" in _failure(capsys)


# --- T11: check-env ---------------------------------------------------------------


def _changed(value: object) -> object:
    """Return a different valid value of the same kind."""
    if isinstance(value, int):
        return value + 1
    assert isinstance(value, str)
    return value + "-changed"


def _env_cases() -> Iterator[tuple[str, str]]:
    """Yield (field, mutation) pairs for every gating field."""
    for field in GATING:
        yield field, "changed"
        yield field, "missing"
        if field not in CGROUP_FIELDS:
            yield field, "absent"


@pytest.mark.parametrize(("field", "mutation"), list(_env_cases()))
def test_t11_check_env_rejects_each_changed_missing_or_absent_gating_field(
    tool: ToolMain,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    field: str,
    mutation: str,
) -> None:
    """T11: every gating field is required, validated and equal to t1's."""
    prior = _build(tool, tmp_path / "t1", 4)
    out = _build(tool, tmp_path / "t2", 2)
    host = _host(tmp_path / "t2")
    assert tool(["check-env", str(prior), str(out)], **host) == 0

    def edit(record: dict[str, object]) -> None:
        if mutation == "changed":
            record[field] = _changed(record[field])
        elif mutation == "missing":
            del record[field]
        else:
            record[field] = "absent"

    _edit_records(out, edit)
    _reseal(tool, out)
    capsys.readouterr()
    assert tool(["check-env", str(prior), str(out)], **host) == 1
    expected = {
        "changed": f"environment differs from t1: gating field differs: {field}",
        "missing": "environment record keys differ from the schema",
        "absent": f"gating field invalid or absent: {field}",
    }[mutation]
    assert expected in _failure(capsys)


def test_t11_check_env_allows_cgroup_absent_only_when_both_sides_agree(
    tool: ToolMain, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T11: cgroup limits may be ``absent`` provided t1 and the job agree."""
    no_cgroup = {k: v for k, v in FAKE_FILES.items() if k != "/proc/self/cgroup"}
    prior = _build(tool, tmp_path / "t1", 4, files=no_cgroup)
    out = _build(tool, tmp_path / "t2", 2, files=no_cgroup)
    record = _mapping(json.loads((out / "environment-before.json").read_text(encoding="utf-8")))
    assert record["cgroup_cpu_max"] == record["cgroup_memory_max"] == "absent"
    assert tool(["check-env", str(prior), str(out)], **_host(tmp_path / "t2")) == 0
    with_cgroup = _build(tool, tmp_path / "t3", 2)
    capsys.readouterr()
    assert tool(["check-env", str(prior), str(with_cgroup)], **_host(tmp_path / "t3")) == 1
    assert "gating field differs: cgroup_cpu_max" in _failure(capsys)


def test_t11_check_env_ignores_cpu_model_snapshots_and_workers(
    tool: ToolMain, tmp_path: Path
) -> None:
    """T11: cpu_model, resource snapshots and the worker arm are observations only."""
    prior = _build(tool, tmp_path / "t1", 4)
    out = _build(tool, tmp_path / "t2", 2)

    def edit(record: dict[str, object]) -> None:
        record["cpu_model"] = "Other CPU"
        record["kernel_release"] = "9.9.9"
        snapshot = _mapping(record["snapshot"])
        snapshot["loadavg"] = ["9.0", "9.0", "9.0"]
        snapshot["cpu_nr_throttled"] = "77"
        record["snapshot"] = snapshot

    _edit_records(out, edit)
    _reseal(tool, out)
    assert tool(["check-env", str(prior), str(out)], **_host(tmp_path / "t2")) == 0


# --- T12: check-pair --------------------------------------------------------------


def _pair(
    tool: ToolMain,
    tmp_path: Path,
    cand_cases: list[str] | None = None,
    cand_arcs: Mapping[str, list[tuple[int, int]]] | None = None,
) -> tuple[Path, Path]:
    """Build a 4-worker baseline and a 2-worker candidate evidence directory."""
    base = _build(tool, tmp_path / "base", 4)
    cand = _build(tool, tmp_path / "cand", 2, cases=cand_cases, arcs=cand_arcs)
    return base, cand


def test_t12_check_pair_passes_on_equal_and_superset_candidates(
    tool: ToolMain, tmp_path: Path
) -> None:
    """T12: equal cases and a superset candidate coverage qualify the pair checks."""
    base, cand = _pair(tool, tmp_path)
    assert tool(["check-pair", str(base), str(cand)], **_host(tmp_path)) == 0
    superset = {
        **BASE_ARCS,
        "src/roastpilot_agent/alpha.py": [(-1, 1), (1, 2), (2, 3), (3, -1), (2, -1)],
        "scripts/extra.py": [(-1, 1), (1, -1)],
    }
    base, cand = _pair(tool, tmp_path / "superset", cand_arcs=superset)
    assert tool(["check-pair", str(base), str(cand)], **_host(tmp_path)) == 0


def _without(name: str) -> dict[str, list[tuple[int, int]]]:
    return {key: value for key, value in BASE_ARCS.items() if key != name}


def _replaced(name: str, arcs: list[tuple[int, int]]) -> dict[str, list[tuple[int, int]]]:
    return {**BASE_ARCS, name: arcs}


ALPHA = "src/roastpilot_agent/alpha.py"


@pytest.mark.parametrize(
    ("cand_cases", "cand_arcs", "reason"),
    [
        pytest.param([DUP, DUP, SKIP_C, OUT_D], None, "case identity multiset", id="missing"),
        pytest.param([*BASE_CASES, NEW_D], None, "case identity multiset", id="extra"),
        pytest.param(
            [PASS_A, DUP, SKIP_C, OUT_D], None, "case identity multiset", id="dropped-duplicate"
        ),
        pytest.param([FAIL_A, DUP, DUP, SKIP_C, OUT_D], None, "per-case outcome", id="outcome"),
        pytest.param([SKIP_A, DUP, DUP, SKIP_C, OUT_D], None, "per-case outcome", id="new-skip"),
        pytest.param(
            [
                PASS_A,
                DUP,
                DUP,
                SKIP_C.replace('message="needs hardware"', 'message="other"'),
                OUT_D,
            ],
            None,
            "skip inventory differs",
            id="skip-message",
        ),
        pytest.param(
            [PASS_A.replace(" />", "><rerun /></testcase>"), DUP, DUP, SKIP_C, OUT_D],
            None,
            "unknown testcase child rerun",
            id="unknown-child",
        ),
        pytest.param(
            None, _replaced(ALPHA, [(-1, 1), (1, -1)]), "baseline line missing", id="line"
        ),
        pytest.param(
            None,
            _replaced(ALPHA, [(-1, 1), (1, 3), (3, 2), (2, -1)]),
            "baseline arc missing",
            id="arc",
        ),
        pytest.param(None, _without("scripts/beta.py"), "baseline file missing", id="file"),
        pytest.param(
            None,
            {**BASE_ARCS, "tests/test_other.py": [(-1, 1), (1, -1)]},
            "outside the coverage roots",
            id="out-of-root",
        ),
        pytest.param(
            None,
            {**BASE_ARCS, "scripts/../tests/escape.py": [(-1, 1), (1, -1)]},
            "outside the coverage roots",
            id="dot-dot",
        ),
    ],
)
def test_t12_check_pair_rejects_each_case_or_coverage_regression(
    tool: ToolMain,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    cand_cases: list[str] | None,
    cand_arcs: Mapping[str, list[tuple[int, int]]] | None,
    reason: str,
) -> None:
    """T12: identities, outcomes, skips and three-root line/arc containment are strict."""
    base, cand = _pair(tool, tmp_path, cand_cases=cand_cases, cand_arcs=cand_arcs)
    capsys.readouterr()
    assert tool(["check-pair", str(base), str(cand)], **_host(tmp_path)) == 1
    assert reason in _failure(capsys)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "no-arcs"])
def test_t12_check_pair_rejects_missing_corrupt_or_arcless_coverage(
    tool: ToolMain, tmp_path: Path, capsys: pytest.CaptureFixture[str], damage: str
) -> None:
    """T12: unreadable, absent or line-only coverage data never qualifies."""
    base, cand = _pair(tool, tmp_path)
    data = cand / ".coverage.ordinary"
    data.unlink()
    if damage == "corrupt":
        data.write_bytes(b"this is not a coverage database\n" * 8)
        _reseal(tool, cand)
        reason = "coverage data unreadable"
    elif damage == "no-arcs":
        _write_coverage(data, lines={ALPHA: [1, 2]})
        _reseal(tool, cand)
        reason = "coverage data has no arcs"
    else:
        reason = "missing evidence file .coverage.ordinary"
    capsys.readouterr()
    assert tool(["check-pair", str(base), str(cand)], **_host(tmp_path)) == 1
    assert reason in _failure(capsys)


def test_t12_check_pair_rejects_swapped_arms_and_bad_junit_root(
    tool: ToolMain, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T12: baseline must be the 4-worker arm and the JUnit grammar is closed."""
    base, cand = _pair(tool, tmp_path)
    capsys.readouterr()
    assert tool(["check-pair", str(cand), str(base)], **_host(tmp_path)) == 1
    assert "pair is not a 4-worker baseline" in _failure(capsys)
    (cand / "junit.xml").write_text("<testsuite><testcase classname='a' name='b'/></testsuite>")
    _reseal(tool, cand)
    capsys.readouterr()
    assert tool(["check-pair", str(base), str(cand)], **_host(tmp_path)) == 1
    assert "JUnit root is not testsuites" in _failure(capsys)


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        pytest.param(
            "<testsuites><testsuite><properties />" + PASS_A + "</testsuite></testsuites>",
            "unexpected JUnit element properties",
            id="suite-child",
        ),
        pytest.param(
            '<testsuites><testsuite><testcase name="test_one" /></testsuite></testsuites>',
            "testcase lacks classname or name",
            id="no-classname",
        ),
        pytest.param(
            '<testsuites><testsuite><testcase classname="tests.test_a" /></testsuite></testsuites>',
            "testcase lacks classname or name",
            id="no-name",
        ),
        pytest.param(
            "<testsuites><testsuite></testsuite></testsuites>",
            "JUnit file has no testcase",
            id="no-cases",
        ),
    ],
)
def test_t12_check_pair_rejects_junit_grammar_violations(
    tool: ToolMain,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    document: str,
    reason: str,
) -> None:
    """T12: the closed JUnit grammar refuses foreign suite children, unnamed or no cases."""
    base, cand = _pair(tool, tmp_path)
    (cand / "junit.xml").write_text(document, encoding="utf-8")
    _reseal(tool, cand)
    capsys.readouterr()
    assert tool(["check-pair", str(base), str(cand)], **_host(tmp_path)) == 1
    assert reason in _failure(capsys)


# --- T13: grammar -----------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["bogus"],
        ["record-before"],
        ["record-before", "3"],
        ["record-before", "four"],
        ["record-before", "4", "extra"],
        ["record-after", "x"],
        ["sanitise", "x"],
        ["seal", "x"],
        ["check-evidence", "x"],
        ["check-env", "a"],
        ["check-env", "a", "b", "c"],
        ["check-pair", "a"],
        ["check-pair", "a", "b", "c"],
    ],
)
def test_t13_unknown_mode_or_wrong_arity_exits_two(
    tool: ToolMain, tmp_path: Path, argv: list[str]
) -> None:
    """T13: the mode grammar is closed and rejected before any host access."""
    assert tool(argv, **_host(tmp_path)) == 2
    assert not (tmp_path / "worker-fit").exists()


# --- T14: record-before -----------------------------------------------------------


def test_t14_record_before_writes_only_whitelisted_fields(tool: ToolMain, tmp_path: Path) -> None:
    """T14: exact keys, ENOENT maps to ``absent``, no host identifiers recorded."""
    assert tool(["record-before", "2"], **_host(tmp_path)) == 0
    out = tmp_path / "worker-fit" / "out"
    for name in SEALED:
        assert (out / name).exists() is (name in {"environment-before.json", "pip-freeze.txt"})
    text = (out / "environment-before.json").read_text(encoding="utf-8")
    record = _mapping(json.loads(text))
    assert set(record) == RECORD_KEYS
    snapshot = _mapping(record["snapshot"])
    assert set(snapshot) == SNAPSHOT_KEYS
    assert record["schema"] == 1
    assert record["workers"] == 2
    assert record["source_sha"] == SOURCE_SHA
    assert record["source_tree"] == SOURCE_TREE
    assert record["image_os"] == "ubuntu24"
    assert record["image_version"] == "20261019.1.0"
    assert record["cgroup_cpu_max"] == "max 100000"
    assert record["cgroup_memory_max"] == "max"
    assert record["mem_total_gib"] == 16
    assert record["affinity_count"] == 4
    assert record["cpu_model"] == "Fake CPU 3000"
    assert snapshot["memory_peak"] == "absent"
    assert snapshot["memory_current"] == "4096"
    assert snapshot["cpu_usage_usec"] == "10"
    assert snapshot["mem_available_kib"] == "8000000"
    assert snapshot["loadavg"] == ["0.10", "0.20", "0.30"]
    assert (out / "pip-freeze.txt").read_text(encoding="utf-8") == FREEZE
    for secret in (*HOST_SECRET_VALUES, CGROUP, "runner-provisioner"):
        assert secret not in text


def test_t14_record_before_maps_enoent_to_absent_but_fails_on_other_os_errors(
    tool: ToolMain, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T14: only ENOENT is ``absent``; permission errors and odd cgroup paths fail."""
    assert tool(["record-before", "4"], **_host(tmp_path / "enoent", files={})) == 0
    record = _mapping(
        json.loads(
            (tmp_path / "enoent/worker-fit/out/environment-before.json").read_text(encoding="utf-8")
        )
    )
    for field in ("cgroup_cpu_max", "cgroup_memory_max", "mem_total_gib", "cpu_model"):
        assert record[field] == "absent", field
    denied = {"/proc/meminfo": PermissionError(13, "denied")}
    assert tool(["record-before", "4"], **_host(tmp_path / "perm", errors=denied)) == 1
    assert "PermissionError" in _failure(capsys)
    escaping = {**FAKE_FILES, "/proc/self/cgroup": "0::/../escape\n"}
    assert tool(["record-before", "4"], **_host(tmp_path / "escape", files=escaping)) == 1
    assert "unexpected cgroup path grammar" in _failure(capsys)
    assert tool(["record-before", "4"], **_host(tmp_path / "enoent", files={})) == 1
    assert "FileExistsError" in _failure(capsys)


# --- Inline tool public API and entry point -----------------------------------------


def test_t13_inline_tool_public_api_is_annotated_and_documented() -> None:
    """The YAML-held tool's public API carries real annotations and Args/Returns docs.

    Pyright never sees this Python (it is a YAML string), so the annotations are
    asserted on the executed objects. The optional injection keywords (the T14
    seam) must keep ``None`` defaults.
    """
    namespace = _exec_tool("worker_fit_tool")
    main = cast(Callable[..., object], namespace["main"])
    members = cast(dict[str, object], vars(cast(type, namespace["Host"])))
    init = cast(Callable[..., object], members["__init__"])
    value = cast(Callable[..., object], members["value"])
    root = cast(Callable[..., object], members["root"])
    seams: dict[str, object] = {
        "environ": Mapping[str, str] | None,
        "read": Callable[[str], str] | None,
        "run": Callable[[list[str]], str] | None,
        "affinity": Callable[[], int] | None,
    }
    assert inspect.get_annotations(main) == {"argv": list[str], **seams, "return": int}
    assert inspect.get_annotations(init) == {**seams, "return": None}
    assert inspect.get_annotations(value) == {"path": str, "return": str}
    assert inspect.get_annotations(root) == {"return": Path}
    main_params = inspect.signature(main).parameters
    assert list(main_params) == ["argv", *seams]
    assert all(main_params[name].default is None for name in seams)
    init_params = inspect.signature(init).parameters
    assert list(init_params) == ["self", *seams]
    assert all(init_params[name].default is None for name in seams)
    for function, sections in (
        (main, ("Args:", "Returns:")),
        (init, ("Args:",)),
        (value, ("Args:", "Returns:")),
        (root, ("Returns:",)),
    ):
        doc = inspect.getdoc(function)
        assert doc is not None
        assert all(section in doc for section in sections), function


def test_t13_main_entry_guard_exits_two_for_a_bogus_or_missing_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real ``__main__`` path (``python -c TOOL ...``) exits 2 on a grammar error."""
    original = list(sys.argv)
    for argv in (["-c", "bogus"], ["-c"]):
        status: object = "did not raise SystemExit"
        with monkeypatch.context() as patch:
            patch.setattr(sys, "argv", argv)
            try:
                _exec_tool("__main__")
            except SystemExit as exc:
                status = exc.code
        assert status == 2, argv
        assert sys.argv == original
        assert "usage: worker-fit-tool MODE [ARGS]" in _failure(capsys)
