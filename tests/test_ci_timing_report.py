"""Finite unit and native-process proofs for ordinary CI attribution."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[1]

if TYPE_CHECKING:
    from scripts import ci_timing_report as timing
else:
    _spec = importlib.util.spec_from_file_location(
        "_ci_timing_report_under_test", ROOT / "scripts/ci_timing_report.py"
    )
    assert _spec is not None and _spec.loader is not None
    timing = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(timing)


def report(
    nodeid: str = "tests/test_a.py::test_a", when: str = "call", **kwargs: Any
) -> pytest.TestReport:
    """Construct native reports with only explicit fixture values."""
    values: dict[str, Any] = dict(
        nodeid=nodeid,
        location=("test_a.py", 0, "test_a"),
        keywords={},
        outcome="passed",
        longrepr=None,
        when=when,
        duration=0.1,
        start=11.0,
        stop=11.1,
        worker_id="gw0",
    )
    values.update(kwargs)
    return pytest.TestReport(**values)


def recorder(tmp_path: Path) -> timing.TimingRecorder:
    """Create isolated controller state with deterministic phase offsets."""
    value = timing.TimingRecorder(tmp_path, Path("out/report.json"))
    value.wall_start = 10.0
    value.pytest_testnodeready(SimpleNamespace(gateway=SimpleNamespace(id="gw0")))
    return value


def finish(value: timing.TimingRecorder, code: int = 0) -> dict[str, Any]:
    """Exercise the actual write and return strict decoded JSON."""
    value.pytest_sessionfinish(cast(pytest.Session, SimpleNamespace(exitstatus=code)))
    return cast(dict[str, Any], json.loads((value.root / value.path).read_text()))


def child(
    tmp_path: Path, args: list[str], source: str = "def test_pass(): pass\n", conftest: str = ""
) -> subprocess.CompletedProcess[str]:
    """Run a native pytest child with literal temporary configuration."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    serial\n    stress\n")
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests/test_fixture.py").write_text(source)
    (tmp_path / "conftest.py").write_text(conftest)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTEST_ADDOPTS", "PYTEST_PLUGINS"} and not key.startswith("PYTEST_XDIST_")
    }
    env["PYTHONPATH"] = str(ROOT)
    env["TIMING_SENTINEL"] = "PRIVATE_ENV_SENTINEL"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(tmp_path / "pytest.ini"),
            "--rootdir",
            str(tmp_path),
            "-p",
            "no:cacheprovider",
            *args,
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )


PLUGIN = ["-p", "scripts.ci_timing_report"]
ACTIVE = ["-n", "4", "--dist", "worksteal", *PLUGIN, "--ci-timing-report=out/report.json"]
SOURCE = (
    """import os, sys, pytest
@pytest.fixture
def broken_teardown():
    yield
    assert False, "PRIVATE_TEARDOWN_SENTINEL"
def test_pass(): pass
def test_fail(): assert False
@pytest.mark.skip(reason="setup skip")
def test_setup_skip(): pass
def test_call_skip(): pytest.skip("call skip")
@pytest.mark.xfail(reason="xfail")
def test_xfail(): assert False
def test_teardown(broken_teardown): pass
@pytest.mark.parametrize("value", [1, 2])
def test_pair(value): assert value
 def_unused = None
""".replace(" def_unused = None", "")
    + """
def test_private():
    print("PRIVATE_STDOUT_SENTINEL")
    print("PRIVATE_STDERR_SENTINEL", file=sys.stderr)
    assert os.environ["TIMING_SENTINEL"] != "PRIVATE_ENV_SENTINEL", "PRIVATE_EXCEPTION_SENTINEL"
def test_extra1(): pass
def test_extra2(): pass
def test_extra3(): pass
"""
)


@pytest.mark.slow
def test_native_failed_run(tmp_path: Path) -> None:
    """T1/V1-V5: native four-worker reports preserve failures and sanitise output."""
    result = child(tmp_path, ACTIVE, SOURCE)
    assert result.returncode == 1, result.stdout + result.stderr
    path = tmp_path / "out/report.json"
    data = json.loads(path.read_text())
    assert list(path.parent.iterdir()) == [path]
    assert set(data) == {
        "schema",
        "complete",
        "incomplete_reasons",
        "counters",
        "collection_available",
        "missing_case_sample",
        "exit_status",
        "session",
        "limits",
        "workers",
        "workers_up",
        "cases",
    }
    assert data["schema"] == "roastpilot-ci-timing/1"
    assert data["exit_status"] == 1
    assert data["complete"] is True
    assert len(data["cases"]) == 12
    assert len(data["workers_up"]) == 4
    assert data["limits"] == list(timing.TIMING_LIMITS)
    assert set(data["session"]) == {"wall_seconds", "phase_seconds_sum"}
    for case in data["cases"]:
        assert set(case) == {"nodeid", "worker", "phases"}
        assert case["worker"] in data["workers_up"]
        phases = case["phases"]
        assert set(phases) == (
            {"setup", "teardown"}
            if "test_setup_skip" in case["nodeid"]
            else {"setup", "call", "teardown"}
        )
        for phase in phases.values():
            assert set(phase) == {"outcome", "duration_s", "start_offset_s", "stop_offset_s"}
            assert 0 <= phase["start_offset_s"] <= phase["stop_offset_s"]
    by_name = {case["nodeid"].split("::")[-1]: case["phases"] for case in data["cases"]}
    assert by_name["test_fail"]["call"]["outcome"] == "failed"
    assert by_name["test_teardown"]["teardown"]["outcome"] == "failed"
    assert by_name["test_xfail"]["call"]["outcome"] == "skipped"
    assert by_name["test_call_skip"]["call"]["outcome"] == "skipped"
    captured = result.stdout + result.stderr
    for sentinel in (
        "PRIVATE_STDOUT_SENTINEL",
        "PRIVATE_STDERR_SENTINEL",
        "PRIVATE_ENV_SENTINEL",
        "PRIVATE_EXCEPTION_SENTINEL",
    ):
        assert sentinel in captured
    raw = path.read_text()
    for forbidden in ("PRIVATE_", str(tmp_path), os.uname().nodename):
        assert forbidden not in raw
    assert (
        result.stdout.count("ci-timing: report out/report.json cases=12 complete=true reasons=none")
        == 1
    )


@pytest.mark.slow
def test_native_pass_duplicate_and_crash(tmp_path: Path) -> None:
    """T2/T6/T7: native pass, duplicate first-wins, and crash evidence."""
    passed = child(tmp_path / "pass", ACTIVE)
    assert passed.returncode == 0, passed.stdout + passed.stderr
    data = json.loads((tmp_path / "pass/out/report.json").read_text())
    assert data["complete"] and data["exit_status"] == 0
    duplicate = """import pytest
config_ref = None
seen = False
def pytest_configure(config):
    global config_ref
    config_ref = config
@pytest.hookimpl(trylast=True)
def pytest_runtest_logreport(report):
    global seen
    if not hasattr(config_ref, "workerinput") and report.when == "call" and not seen:
        seen = True
        report.duration = 999.0
        config_ref.hook.pytest_runtest_logreport(report=report)
"""
    result = child(tmp_path / "duplicate", ACTIVE, conftest=duplicate)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads((tmp_path / "duplicate/out/report.json").read_text())
    assert data["counters"]["duplicate_phase_report"] == 1
    assert not data["complete"]
    assert data["cases"][0]["phases"]["call"]["duration_s"] != 999.0
    result = child(
        tmp_path / "crash",
        [*ACTIVE, "--max-worker-restart=0"],
        "import os\ndef test_crash(): os._exit(1)\n",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    raw = (tmp_path / "crash/out/report.json").read_text()
    data = json.loads(raw)
    assert not data["complete"]
    assert data["counters"]["worker_down_with_error"] > 0
    assert "crashed" not in raw


@pytest.mark.slow
@pytest.mark.parametrize("args", [[], PLUGIN, [*PLUGIN, "-p", "no:xdist"]])
def test_disabled(tmp_path: Path, args: list[str]) -> None:
    """T3: disabled plugin including xdist-unavailable registration is inert."""
    result = child(tmp_path, args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ci-timing:" not in result.stdout + result.stderr
    assert not (tmp_path / "out").exists()


@pytest.mark.slow
@pytest.mark.parametrize("extra", [[], ["-n", "0"], ["-n", "2", "--collect-only"]])
def test_preconditions(tmp_path: Path, extra: list[str]) -> None:
    """T4: collection and inactive distribution fail before running tests."""
    result = child(tmp_path, [*PLUGIN, "--ci-timing-report=out/report.json", *extra])
    assert result.returncode == 4
    assert "ci-timing requires an active xdist test run" in result.stderr
    assert not (tmp_path / "out").exists()


@pytest.mark.slow
@pytest.mark.parametrize("path", ["", "../out.json", "out.txt", "absolute", "exists.json"])
def test_paths(tmp_path: Path, path: str) -> None:
    """T5: invalid destinations never overwrite or escape the root."""
    tmp_path.mkdir(exist_ok=True)
    existing = tmp_path / "exists.json"
    existing.write_text("preserve")
    path = str(tmp_path / "absolute.json") if path == "absolute" else path
    result = child(tmp_path, ["-n", "2", *PLUGIN, f"--ci-timing-report={path}"])
    assert result.returncode == 4
    assert "invalid or existing ci-timing path" in result.stderr
    assert not (tmp_path.parent / "out.json").exists()
    assert existing.read_text() == "preserve"
    assert not (tmp_path / "absolute.json").exists()


@pytest.mark.parametrize(
    "field,value,counter",
    [
        ("when", "crash", "unknown_phase"),
        ("when", 1, "unknown_phase"),
        ("outcome", "unknown", "unknown_outcome"),
        ("outcome", 1, "unknown_outcome"),
        ("worker_id", "host", "missing_worker"),
        ("worker_id", None, "missing_worker"),
        ("duration", -1.0, "nonfinite_timing"),
        ("duration", float("nan"), "nonfinite_timing"),
        ("duration", 1, "nonfinite_timing"),
        ("start", None, "missing_wallclock"),
        ("stop", 1.0, "missing_wallclock"),
        ("stop", float("inf"), "missing_wallclock"),
    ],
)
def test_invalid_fields(tmp_path: Path, field: str, value: object, counter: str) -> None:
    """T8: closed native field grammars produce declared incomplete evidence."""
    value_rec = recorder(tmp_path)
    native = report()
    setattr(native, field, value)
    value_rec.pytest_runtest_logreport(native)
    data = finish(value_rec)
    assert data["counters"][counter] == 1
    assert not data["complete"]
    if field in {"duration", "start", "stop"}:
        phase = data["cases"][0]["phases"]["call"]
        assert phase["duration_s" if field == "duration" else "start_offset_s"] is None


def test_caps_and_inventory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T9/T11: inventory, IDs and encoded bytes all have explicit caps."""
    monkeypatch.setattr(timing, "MAX_CASES", 1)
    monkeypatch.setattr(timing, "MAX_NODEID_CHARS", 3)
    value = recorder(tmp_path)
    value.pytest_xdist_node_collection_finished(["long", "a", "b"])
    value.pytest_xdist_node_collection_finished(["ignored"])
    for name in ("", "long", "a", "b"):
        value.pytest_runtest_logreport(report(name))
    value.pytest_testnodeready(SimpleNamespace(gateway=SimpleNamespace(id="gw9999")))
    value.pytest_testnodeready(SimpleNamespace(gateway=SimpleNamespace(id="bad")))
    value.pytest_testnodedown(None)
    value.pytest_testnodedown("private")
    data = finish(value)
    assert len(data["cases"]) == 1
    assert data["counters"]["case_limit"] == 2
    assert data["counters"]["oversize_nodeid"] == 3
    assert data["workers_up"] == ["gw0", "gw9999"]
    assert data["counters"]["missing_worker"] == 1
    assert data["counters"]["worker_down_with_error"] == 1
    monkeypatch.setattr(timing, "MAX_REPORT_BYTES", 1)
    value = timing.TimingRecorder(tmp_path, Path("size.json"))
    value.pytest_sessionfinish(cast(pytest.Session, SimpleNamespace(exitstatus=1)))
    assert value.status == "not written (size_limit)"
    assert not (tmp_path / "size.json").exists()


@pytest.mark.parametrize(
    "phases,complete",
    [
        ([("setup", "passed"), ("call", "passed"), ("teardown", "passed")], True),
        ([("setup", "skipped"), ("teardown", "passed")], True),
        ([("setup", "failed"), ("teardown", "passed")], True),
        ([("setup", "passed"), ("teardown", "passed")], False),
        ([("setup", "skipped"), ("call", "passed"), ("teardown", "passed")], False),
        ([("call", "passed")], False),
    ],
)
def test_pairing(tmp_path: Path, phases: list[tuple[str, str]], complete: bool) -> None:
    """T11: call is present if and only if setup passed."""
    value = recorder(tmp_path)
    value.pytest_xdist_node_collection_finished(["a"])
    for when, outcome in phases:
        value.pytest_runtest_logreport(report("a", when, outcome=outcome))
    assert finish(value)["complete"] is complete


def test_missing_and_unavailable(tmp_path: Path) -> None:
    """T11: missing/unexpected cases and absent collection never look complete."""
    value = recorder(tmp_path)
    value.pytest_xdist_node_collection_finished([str(i) for i in range(70)])
    value.pytest_runtest_logreport(report())
    data = finish(value)
    assert data["counters"]["missing_cases"] == 70
    assert data["counters"]["unexpected_cases"] == 1
    assert len(data["missing_case_sample"]) == 50
    value = timing.TimingRecorder(tmp_path, Path("unavailable.json"))
    assert "collection_unavailable" in finish(value)["incomplete_reasons"]


def test_write_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T10: exclusive creation and write errors never rewrite native exit status."""
    value = recorder(tmp_path)
    target = tmp_path / value.path
    target.parent.mkdir()
    target.write_text("preserve")
    session = cast(pytest.Session, SimpleNamespace(exitstatus=1))
    value.pytest_sessionfinish(session)
    assert target.read_text() == "preserve"
    assert value.status == "not written (exists)"
    assert session.exitstatus == 1
    lines: list[str] = []
    value.pytest_terminal_summary(
        cast(timing.TerminalReporter, SimpleNamespace(write_line=lines.append))
    )
    assert lines == ["ci-timing: report not written (exists)"]
    value = timing.TimingRecorder(tmp_path, Path("parent_file/child.json"))
    (tmp_path / "parent_file").write_text("file")
    value.pytest_sessionfinish(session)
    assert value.status == "not written (os_error)"

    def encode_error(*args: object, **kwargs: object) -> str:
        raise ValueError("private")

    monkeypatch.setattr(timing, "json", SimpleNamespace(dumps=encode_error))
    assert timing.json is not json
    value.pytest_sessionfinish(session)
    assert value.status == "not written (encode_error)"
    assert session.exitstatus == 1


def test_strict_projection_and_clocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T12: native invalid numbers and aggregate overflow stay strict JSON."""
    value = recorder(tmp_path)
    monkeypatch.setattr(timing, "time", SimpleNamespace(time=lambda: 10.0, monotonic=lambda: 20.0))
    assert timing.time is not time
    value.pytest_sessionstart()
    for name, duration in (("a", 1e308), ("b", 1e308), ("c", float("inf"))):
        value.pytest_runtest_logreport(report(name, duration=duration))
    value.pytest_runtest_logreport(report("a", "setup", start=-1e308, stop=1e308))
    value.wall_start = -1e308
    value.pytest_runtest_logreport(report("b", "setup", start=1e308, stop=1e308))
    data = finish(value)
    assert data["session"]["phase_seconds_sum"] is None
    assert data["workers"]["gw0"]["phase_seconds_sum"] is None
    assert data["cases"][2]["phases"]["call"]["duration_s"] is None
    assert data["counters"]["nonfinite_timing"] > 0
    assert data["counters"]["missing_wallclock"] > 0
    assert "Infinity" not in (tmp_path / value.path).read_text()
    # Strict encoding is an independent backstop for an invalid projection.
    value = timing.TimingRecorder(tmp_path, Path("bad.json"))

    def invalid_payload(*args: object) -> dict[str, object]:
        return {"bad": float("nan")}

    monkeypatch.setattr(value, "_payload", invalid_payload)
    value.pytest_sessionfinish(cast(pytest.Session, SimpleNamespace(exitstatus=0)))
    assert value.status == "not written (encode_error)"


@pytest.mark.slow
def test_native_dist_no_normalisation(tmp_path: Path) -> None:
    """R2: native -n changes raw dist=no into active load distribution."""
    result = child(
        tmp_path, ["-n", "2", "--dist", "no", *PLUGIN, "--ci-timing-report=out/report.json"]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((tmp_path / "out/report.json").read_text())["complete"]


def test_configure_and_worker_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise invalid native plumbing and controller-only registration branches."""

    class Config:
        rootpath = tmp_path
        option = SimpleNamespace(collectonly=False)

        class Manager:
            def register(self, *args: object) -> None:
                pass

        pluginmanager = Manager()
        values: dict[str, object] = {
            "ci_timing_report": "out.json",
            "numprocesses": 2,
            "dist": "load",
        }

        def getoption(self, name: str, default: object = None) -> object:
            return self.values.get(name, default)

    config = Config()
    native = cast(pytest.Config, config)
    config.values["ci_timing_report"] = None
    timing.pytest_configure(native)
    config.values["ci_timing_report"] = "out.json"
    monkeypatch.setattr(config, "workerinput", {}, raising=False)
    timing.pytest_configure(native)
    monkeypatch.delattr(config, "workerinput")
    config.values["dist"] = "no"
    with pytest.raises(pytest.UsageError):
        timing.pytest_configure(native)
    config.values["dist"] = "load"
    config.values["ci_timing_report"] = 1
    with pytest.raises(pytest.UsageError):
        timing.pytest_configure(native)
    config.values["ci_timing_report"] = "out.json"
    timing.pytest_configure(native)
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    config.values["ci_timing_report"] = "escape/out.json"
    with pytest.raises(pytest.UsageError):
        timing.pytest_configure(native)
    config.values["ci_timing_report"] = "bad\x00.json"
    with pytest.raises(pytest.UsageError):
        timing.pytest_configure(native)


def test_worker_change_and_summary_branches(tmp_path: Path) -> None:
    """Malformed attribution cannot silently create complete worker totals."""
    value = recorder(tmp_path)
    value.pytest_runtest_logreport(report("a", "setup", worker_id="gw1", duration=None))
    value.pytest_runtest_logreport(report("a", "call", worker_id="gw0", start=10.5, stop=10.6))
    value.pytest_runtest_logreport(report("a", "teardown", worker_id="gw0", start=12.0, stop=12.1))
    value.pytest_runtest_logreport(report("b", worker_id=None, duration=None))
    data = finish(value)
    assert data["counters"]["missing_worker"] == 2
    assert data["workers"]["gw0"]["first_start_offset_s"] == 0.5
    assert data["workers"]["gw0"]["last_stop_offset_s"] == pytest.approx(2.1)
    assert not data["complete"]


def test_write_containment_rechecked(tmp_path: Path) -> None:
    """A parent symlink changed after validation cannot escape the root."""
    value = recorder(tmp_path)
    (tmp_path / "out").symlink_to(tmp_path.parent, target_is_directory=True)
    value.pytest_sessionfinish(cast(pytest.Session, SimpleNamespace(exitstatus=1)))
    assert value.status == "not written (os_error)"


def test_duplicate_and_option_registration(tmp_path: Path) -> None:
    """Cover controller first-wins and actual public pytest option registration."""
    parser = pytest.Parser(_ispytest=True)
    timing.pytest_addoption(parser)
    assert parser.parse(["--ci-timing-report=out.json"]).ci_timing_report == "out.json"
    value = recorder(tmp_path)
    value.pytest_runtest_logreport(report())
    value.pytest_runtest_logreport(report(duration=999.0))
    data = finish(value)
    assert data["counters"]["duplicate_phase_report"] == 1
    assert data["cases"][0]["phases"]["call"]["duration_s"] == 0.1


def test_explicit_os_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A filesystem failure remains a closed diagnostic with the native code intact."""

    def fail_open(*args: object, **kwargs: object) -> None:
        raise OSError("private error")

    value = recorder(tmp_path)
    session = cast(pytest.Session, SimpleNamespace(exitstatus=2))
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "open", fail_open)
        value.pytest_sessionfinish(session)
    assert value.status == "not written (os_error)"
    assert session.exitstatus == 2


def test_identity_byte_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R3: independent UTF-8 caps accept the exact bound and charge distinct IDs once."""
    monkeypatch.setattr(timing, "MAX_NODEID_BYTES_TOTAL", 3)
    value = recorder(tmp_path)
    value.pytest_xdist_node_collection_finished(["é", "é", "a", "b"])
    assert value.expected == {"é", "a"}
    for when in ("setup", "call", "teardown"):
        value.pytest_runtest_logreport(report("é", when))
    value.pytest_runtest_logreport(report("a"))
    value.pytest_runtest_logreport(report("b"))
    assert set(value.cases) == {"é", "a"}
    assert value.recorded_bytes == 3
    data = finish(value)
    assert data["counters"]["case_limit"] == 2
    assert not data["complete"]
    assert data["missing_case_sample"] == []
    assert all(case["nodeid"] != "b" for case in data["cases"])
    # Each inventory has its own whole budget, independent of the other set's contents.
    other = timing.TimingRecorder(tmp_path, Path("other.json"))
    other.pytest_xdist_node_collection_finished(["abc"])
    other.pytest_runtest_logreport(report("xyz"))
    assert other.expected == {"abc"} and set(other.cases) == {"xyz"}
    assert other.counters["case_limit"] == 0


def test_current_private_module_identity() -> None:
    """Unit coverage executes the current source even when the CI plugin is preloaded."""
    assert timing.__name__ == "_ci_timing_report_under_test"
    assert Path(timing.__file__).resolve() == ROOT / "scripts/ci_timing_report.py"
    loaded = sys.modules.get("scripts.ci_timing_report")
    assert loaded is not timing


@pytest.mark.parametrize("ready,worker", [(False, "gw0"), (True, "gw1")])
def test_ready_inventory_required(tmp_path: Path, ready: bool, worker: str) -> None:
    """Grammar-valid attribution must have corresponding native ready evidence."""
    value = recorder(tmp_path)
    if not ready:
        value.workers_up.clear()
    value.pytest_xdist_node_collection_finished(["a"])
    for phase in ("setup", "call", "teardown"):
        value.pytest_runtest_logreport(report("a", phase, worker_id=worker))
    data = finish(value)
    assert not data["complete"]
    assert data["counters"]["missing_worker"] == 1
    assert "missing_worker" in data["incomplete_reasons"]


def test_unencodable_identity(tmp_path: Path) -> None:
    """A lone surrogate is rejected before either inventory retains or encodes it."""
    value = recorder(tmp_path)
    bad = "bad\ud800"
    value.pytest_xdist_node_collection_finished([bad])
    value.pytest_runtest_logreport(report(bad))
    data = finish(value, 1)
    assert value.expected == set() and value.cases == {}
    assert data["counters"]["oversize_nodeid"] == 2
    assert not data["complete"] and data["exit_status"] == 1
    assert data["missing_case_sample"] == []


@pytest.mark.slow
def test_native_unencodable_reports_preserve_outcome(tmp_path: Path) -> None:
    """Native controller injection exercises both malformed-ID paths without escaping."""
    conftest = """import pytest
@pytest.hookimpl(trylast=True)
def pytest_sessionstart(session):
    if not hasattr(session.config, "workerinput"):
        recorder = session.config.pluginmanager.get_plugin("ci-timing-recorder")
        recorder.pytest_xdist_node_collection_finished(["bad\\ud800"])
        report = pytest.TestReport(nodeid="bad\\ud800", location=("test.py", 0, "test"),
            keywords={}, outcome="passed", longrepr=None, when="call", duration=0.1,
            start=1.0, stop=1.1, worker_id="gw0")
        recorder.pytest_runtest_logreport(report=report)
"""
    result = child(tmp_path, ACTIVE, "def test_fail(): assert False\n", conftest)
    assert result.returncode == 1, result.stdout + result.stderr
    data = json.loads((tmp_path / "out/report.json").read_text())
    assert data["exit_status"] == 1 and not data["complete"]
    assert data["counters"]["oversize_nodeid"] == 2
    assert all("bad" not in case["nodeid"] for case in data["cases"])
    assert data["missing_case_sample"] == []
    assert "INTERNALERROR" not in result.stdout + result.stderr
