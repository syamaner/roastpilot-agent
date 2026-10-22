"""Bounded controller-side pytest phase attribution for the ordinary CI lane."""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Protocol, TypedDict, cast

import pytest

MAX_CASES = 40_000
MAX_NODEID_CHARS = 131072
MAX_NODEID_BYTES_TOTAL = 8 * 1024 * 1024
MAX_REPORT_BYTES = 64 * 1024 * 1024
TIMING_LIMITS = (
    (
        "Worker time.time start/stop offsets use the same-host controller session-start "
        "wall clock; clock adjustment can distort them and they are not monotonic."
    ),
    "duration_s is pytest's native per-phase duration.",
    "phase_seconds_sum totals concurrent worker time and is not wall time.",
    (
        "Gaps between consecutive worker phases include scheduling, IPC, import and "
        "collection overhead and are unattributed."
    ),
    "Worker start-up and per-worker collection are not measured.",
    (
        "wall_seconds is controller-monotonic from session start to finish, excluding "
        "interpreter start-up and plugin import."
    ),
    "One run is an observation, not a performance claim.",
)
_COUNTERS = (
    "duplicate_phase_report",
    "unknown_phase",
    "unknown_outcome",
    "missing_worker",
    "missing_wallclock",
    "nonfinite_timing",
    "oversize_nodeid",
    "case_limit",
    "unpaired_phases",
    "missing_cases",
    "unexpected_cases",
    "worker_down_with_error",
)


class _Phase(TypedDict):
    outcome: str
    duration_s: float | None
    start_offset_s: float | None
    stop_offset_s: float | None


class _Case(TypedDict):
    nodeid: str
    worker: str | None
    phases: dict[str, _Phase]


class _Worker(TypedDict):
    cases: int
    phase_seconds_sum: float | None
    first_start_offset_s: float | None
    last_stop_offset_s: float | None


def _worker_id(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"gw[0-9]{1,4}", value) else None


def _finite(value: object) -> float | None:
    return value if isinstance(value, float) and math.isfinite(value) else None


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the explicit report destination option in every process."""
    parser.addoption("--ci-timing-report", default=None, help="Ordinary CI timing JSON path")


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    """Enable the recorder only on an explicitly opted-in xdist controller."""
    value: object = config.getoption("ci_timing_report")
    if value is None or hasattr(config, "workerinput"):
        return
    processes: object = config.getoption("numprocesses", default=0)
    if not processes or config.getoption("dist", default="no") == "no" or config.option.collectonly:
        raise pytest.UsageError("ci-timing requires an active xdist test run")
    if not isinstance(value, str):
        raise pytest.UsageError("invalid ci-timing path")
    path = Path(value)
    root = config.rootpath.resolve()
    try:
        if (
            not value
            or path.is_absolute()
            or ".." in path.parts
            or path.suffix != ".json"
            or not (root / path).resolve().is_relative_to(root)
            or (root / path).exists()
        ):
            raise pytest.UsageError("invalid or existing ci-timing path")
    except (OSError, ValueError) as exc:
        raise pytest.UsageError("invalid ci-timing path") from exc
    config.pluginmanager.register(TimingRecorder(root, path), "ci-timing-recorder")


class TerminalReporter(Protocol):
    """Consumer-owned terminal output capability."""

    def write_line(self, line: str) -> None:
        """Write one diagnostic line."""
        ...


class TimingRecorder:
    """Bounded per-config phase state owned only by the xdist controller."""

    def __init__(self, root: Path, path: Path) -> None:
        """Initialise empty attribution state for a validated destination."""
        self.root = root
        self.path = path
        self.cases: dict[str, _Case] = {}
        self.expected: set[str] | None = None
        self.workers_up: set[str] = set()
        self.recorded_bytes = 0
        self.counters: dict[str, int] = dict.fromkeys(_COUNTERS, 0)
        self.wall_start = 0.0
        self.mono_start = 0.0
        self.status = "not written (os_error)"

    def pytest_sessionstart(self) -> None:
        """Read the two session clocks once on the controller."""
        self.wall_start = time.time()
        self.mono_start = time.monotonic()

    def _nodeid(self, value: object) -> str | None:
        if isinstance(value, str) and 0 < len(value) <= MAX_NODEID_CHARS:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                self.counters["oversize_nodeid"] += 1
                return None
            return value
        self.counters["oversize_nodeid"] += 1
        return None

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, ids: list[str]) -> None:
        """Keep only the first worker's bounded expected inventory."""
        if self.expected is not None:
            return
        self.expected = set()
        expected_bytes = 0
        for value in ids:
            if len(self.expected) >= MAX_CASES:
                self.counters["case_limit"] += 1
                break
            nodeid = self._nodeid(value)
            if nodeid is not None and nodeid not in self.expected:
                cost = len(nodeid.encode("utf-8"))
                if expected_bytes + cost > MAX_NODEID_BYTES_TOTAL:
                    self.counters["case_limit"] += 1
                    break
                self.expected.add(nodeid)
                expected_bytes += cost

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodeready(self, node: object) -> None:
        """Retain bounded, grammar-checked xdist worker identities."""
        worker = _worker_id(getattr(getattr(node, "gateway", None), "id", None))
        if worker is None:
            self.counters["missing_worker"] += 1
        else:
            self.workers_up.add(worker)

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, error: object) -> None:
        """Count worker failures without reading or retaining their text."""
        if error is not None:
            self.counters["worker_down_with_error"] += 1

    def _number(self, value: object, reason: str) -> float | None:
        finite = _finite(value)
        if finite is None:
            self.counters[reason] += 1
        return finite

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Extract only admitted native report fields; keep the first phase."""
        nodeid = self._nodeid(getattr(report, "nodeid", None))
        if nodeid is None:
            return
        when: object = getattr(report, "when", None)
        if not isinstance(when, str) or when not in {"setup", "call", "teardown"}:
            self.counters["unknown_phase"] += 1
            return
        outcome: object = getattr(report, "outcome", None)
        if not isinstance(outcome, str) or outcome not in {"passed", "failed", "skipped"}:
            self.counters["unknown_outcome"] += 1
            return
        if nodeid not in self.cases:
            cost = len(nodeid.encode("utf-8"))
            if len(self.cases) >= MAX_CASES or self.recorded_bytes + cost > MAX_NODEID_BYTES_TOTAL:
                self.counters["case_limit"] += 1
                return
            self.cases[nodeid] = {"nodeid": nodeid, "worker": None, "phases": {}}
            self.recorded_bytes += cost
        case = self.cases[nodeid]
        if when in case["phases"]:
            self.counters["duplicate_phase_report"] += 1
            return
        worker = _worker_id(getattr(report, "worker_id", None))
        if worker is None or (case["phases"] and worker != case["worker"]):
            self.counters["missing_worker"] += 1
        case["worker"] = worker
        duration = self._number(getattr(report, "duration", None), "nonfinite_timing")
        if duration is not None and duration < 0:
            duration = None
            self.counters["nonfinite_timing"] += 1
        start, stop = (
            _finite(getattr(report, "start", None)),
            _finite(getattr(report, "stop", None)),
        )
        if start is None or stop is None or start > stop:
            start = stop = None
            self.counters["missing_wallclock"] += 1
        else:
            start = self._number(start - self.wall_start, "missing_wallclock")
            stop = self._number(stop - self.wall_start, "missing_wallclock")
        case["phases"][when] = {
            "outcome": outcome,
            "duration_s": duration,
            "start_offset_s": start,
            "stop_offset_s": stop,
        }

    def _payload(self, exitstatus: int, elapsed: float) -> dict[str, object]:
        workers: dict[str, _Worker] = {}
        total = 0.0
        for case in self.cases.values():
            phases = case["phases"]
            setup = phases.get("setup")
            expected = (
                {"setup", "teardown", "call"}
                if setup is not None and setup["outcome"] == "passed"
                else {"setup", "teardown"}
            )
            if set(phases) != expected:
                self.counters["unpaired_phases"] += 1
            worker = case["worker"]
            summary: _Worker | None = None
            if worker is not None:
                summary = workers.setdefault(
                    worker,
                    {
                        "cases": 0,
                        "phase_seconds_sum": 0.0,
                        "first_start_offset_s": None,
                        "last_stop_offset_s": None,
                    },
                )
                summary["cases"] += 1
            for phase in phases.values():
                duration = phase["duration_s"]
                if duration is not None:
                    total += duration
                    if summary is not None and summary["phase_seconds_sum"] is not None:
                        summary["phase_seconds_sum"] += duration
                if summary is not None:
                    start, stop = phase["start_offset_s"], phase["stop_offset_s"]
                    first, last = summary["first_start_offset_s"], summary["last_stop_offset_s"]
                    if start is not None:
                        summary["first_start_offset_s"] = (
                            start if first is None else min(first, start)
                        )
                    if stop is not None:
                        summary["last_stop_offset_s"] = stop if last is None else max(last, stop)
        for summary in workers.values():
            summary["phase_seconds_sum"] = self._number(
                summary["phase_seconds_sum"], "nonfinite_timing"
            )
        phase_sum = self._number(total, "nonfinite_timing")
        wall = self._number(elapsed, "nonfinite_timing")
        missing = (self.expected or set()) - self.cases.keys()
        self.counters["missing_cases"] = len(missing)
        self.counters["unexpected_cases"] = len(self.cases.keys() - (self.expected or set()))
        if not self.workers_up or any(
            case["worker"] is not None and case["worker"] not in self.workers_up
            for case in self.cases.values()
        ):
            self.counters["missing_worker"] += 1
        reasons = [key for key, count in self.counters.items() if count]
        if self.expected is None:
            reasons.append("collection_unavailable")
        return {
            "schema": "roastpilot-ci-timing/1",
            "complete": not reasons,
            "incomplete_reasons": sorted(reasons),
            "counters": self.counters,
            "collection_available": self.expected is not None,
            "missing_case_sample": sorted(missing)[:50],
            "exit_status": exitstatus,
            "session": {"wall_seconds": wall, "phase_seconds_sum": phase_sum},
            "limits": TIMING_LIMITS,
            "workers": workers,
            "workers_up": sorted(self.workers_up),
            "cases": [self.cases[key] for key in sorted(self.cases)],
        }

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        """Write exclusively without changing native pytest outcomes."""
        elapsed = time.monotonic() - self.mono_start
        try:
            payload = self._payload(int(session.exitstatus), elapsed)
            encoded = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
            )
            if len(encoded.encode("utf-8")) > MAX_REPORT_BYTES:
                self.status = "not written (size_limit)"
                return
            target = self.root / self.path
            if not target.resolve().is_relative_to(self.root):
                raise OSError("outside root")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                stream = target.open("x", encoding="utf-8")
            except FileExistsError:
                self.status = "not written (exists)"
                return
            with stream:
                stream.write(encoded)
            reasons = ",".join(cast(list[str], payload["incomplete_reasons"]))
            self.status = (
                f"{self.path.as_posix()} cases={len(self.cases)} "
                f"complete={str(payload['complete']).lower()} reasons={reasons or 'none'}"
            )
        except OSError:
            self.status = "not written (os_error)"
        except ValueError:
            self.status = "not written (encode_error)"

    def pytest_terminal_summary(self, terminalreporter: TerminalReporter) -> None:
        """Print exactly one sanitised controller status line after writing."""
        terminalreporter.write_line(f"ci-timing: report {self.status}")
