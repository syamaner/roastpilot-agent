"""Hardware-free behavioural coverage for cold host-bound readers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from roastpilot_agent.cold_characterisation.host import (
    HOST_MIN_FREE_BYTES_BEFORE,
    HOST_MIN_FREE_BYTES_DURING,
    HOST_MIN_MEM_AVAILABLE_BYTES,
    ColdHostBoundError,
    ColdHostBoundFailure,
    HostBoundSample,
    HostBoundsConfig,
    LinuxHostBoundsReader,
)


class RecordingRunner:
    """Injectable command runner that records only list argv."""

    def __init__(self, result: subprocess.CompletedProcess[bytes] | BaseException) -> None:
        """Store the result or error that a command invocation should produce."""
        self.calls: list[list[str]] = []
        self.result = result

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        """Record *argv* and return the configured completed process."""
        self.calls.append(argv)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _completed(
    *,
    returncode: int = 0,
    stdout: bytes = b"throttled=0x0\n",
    stderr: bytes = b"",
) -> subprocess.CompletedProcess[bytes]:
    """Return a controlled byte-oriented command result."""
    return subprocess.CompletedProcess(["vcgencmd", "get_throttled"], returncode, stdout, stderr)


def _reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    thermal: str = "79900\n",
    meminfo: str | None = None,
    command_path: Path | None = None,
    runner: RecordingRunner | None = None,
    free_bytes: int = HOST_MIN_FREE_BYTES_BEFORE,
    thermal_path: Path | None = None,
    meminfo_path: Path | None = None,
    use_default_runner: bool = False,
) -> LinuxHostBoundsReader:
    """Build a Linux reader with files and seams wholly inside ``tmp_path``."""
    configured_thermal_path = thermal_path or tmp_path / "thermal"
    if thermal_path is None:
        configured_thermal_path.write_text(thermal, encoding="ascii")
    configured_meminfo_path = meminfo_path or tmp_path / "meminfo"
    if meminfo_path is None:
        configured_meminfo_path.write_text(
            f"MemAvailable: {HOST_MIN_MEM_AVAILABLE_BYTES // 1024} kB\n"
            if meminfo is None
            else meminfo,
            encoding="ascii",
        )
    binary_path = command_path or tmp_path / "vcgencmd"
    if command_path is None:
        binary_path.touch()

    def fake_statvfs(_path: object) -> Any:
        return SimpleNamespace(f_bavail=free_bytes, f_bfree=free_bytes + 17, f_frsize=1)

    monkeypatch.setattr(os, "statvfs", fake_statvfs)
    config = HostBoundsConfig(
        vcgencmd_path=binary_path,
        thermal_zone_temp_path=configured_thermal_path,
        meminfo_path=configured_meminfo_path,
    )
    return LinuxHostBoundsReader(
        config,
        command_runner=None if use_default_runner else runner or RecordingRunner(_completed()),
        platform_name="linux",
    )


def _assert_failure(expected: ColdHostBoundFailure, action: Any) -> None:
    """Assert that an observation fails with one closed failure member."""
    with pytest.raises(ColdHostBoundError) as raised:
        action()
    assert raised.value.failure is expected


def test_thermal_parses_safe_millidegrees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A safe millidegree source is returned in Celsius."""
    assert _reader(tmp_path, monkeypatch).read_thermal_c() == 79.9


@pytest.mark.parametrize("thermal", ["80000", "80100"])
def test_thermal_rejects_the_limit_and_above(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, thermal: str
) -> None:
    """Exactly 80 C and a hotter observation both fail closed."""
    _assert_failure(
        ColdHostBoundFailure.THERMAL_EXCEEDED,
        lambda: _reader(tmp_path, monkeypatch, thermal=thermal).read_thermal_c(),
    )


@pytest.mark.parametrize(
    "thermal",
    ["", "abc", "45.5", "79900\n1", "x79900", " 79900", "79900 ", "12345678"],
)
def test_thermal_rejects_malformed_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, thermal: str
) -> None:
    """Only the closed millidegree grammar is accepted."""
    _assert_failure(
        ColdHostBoundFailure.THERMAL_MALFORMED,
        lambda: _reader(tmp_path, monkeypatch, thermal=thermal).read_thermal_c(),
    )


def test_thermal_missing_source_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing thermal source never becomes a clean default."""
    reader = _reader(tmp_path, monkeypatch, thermal_path=tmp_path / "missing")
    _assert_failure(ColdHostBoundFailure.THERMAL_UNREADABLE, reader.read_thermal_c)


def test_thermal_non_ascii_source_is_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decoding failure is malformed rather than an acceptable temperature."""
    thermal_path = tmp_path / "thermal"
    reader = _reader(tmp_path, monkeypatch, thermal_path=thermal_path)

    def malformed_read(_path: Path, **_kwargs: object) -> str:
        raise UnicodeDecodeError("ascii", b"\xff", 0, 1, "invalid")

    monkeypatch.setattr(Path, "read_text", malformed_read)
    _assert_failure(ColdHostBoundFailure.THERMAL_MALFORMED, reader.read_thermal_c)


def test_throttle_reads_canonical_lowercase_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clear throttle result parses to an integer used for canonical samples."""
    runner = RecordingRunner(_completed(stdout=b"throttled=0x0100\n"))
    reader = _reader(tmp_path, monkeypatch, runner=runner)
    assert reader.read_throttled_word() == 0x100
    assert runner.calls == [[str((tmp_path / "vcgencmd").resolve()), "get_throttled"]]
    assert reader.sample(tmp_path).throttled_word_hex == "0x100"


@pytest.mark.parametrize("bit", [0, 1, 2, 3, 16, 17, 18, 19])
def test_throttle_rejects_each_current_and_sticky_guard_bit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bit: int
) -> None:
    """All ratified current and sticky throttle bits are independently gated."""
    word = 1 << bit
    reader = _reader(
        tmp_path,
        monkeypatch,
        runner=RecordingRunner(_completed(stdout=f"throttled=0x{word:x}".encode("ascii"))),
    )
    _assert_failure(ColdHostBoundFailure.THROTTLE_BITS_SET, reader.read_throttled_word)


@pytest.mark.parametrize(
    "stdout",
    [
        b"",
        b"throttled=0x0\nsecond",
        b"throttled=abc",
        b"throttled=0x",
        b"throttled=0x123456789",
        b"x" * 5120,
        b"\xff",
    ],
)
def test_throttle_rejects_malformed_or_oversized_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: bytes
) -> None:
    """Only one bounded ASCII throttle line is admitted."""
    reader = _reader(tmp_path, monkeypatch, runner=RecordingRunner(_completed(stdout=stdout)))
    _assert_failure(ColdHostBoundFailure.THROTTLE_OUTPUT_MALFORMED, reader.read_throttled_word)


def test_throttle_default_runner_uses_a_closed_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production runner uses fixed argv, environment, stdio, and timeout."""
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        return _completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    reader = _reader(tmp_path, monkeypatch, use_default_runner=True)
    assert reader.read_throttled_word() == 0
    argv, kwargs = calls[0]
    assert isinstance(argv, list)
    assert argv == [str((tmp_path / "vcgencmd").resolve()), "get_throttled"]
    assert kwargs["env"] == {"LC_ALL": "C", "PATH": "/usr/bin:/bin"}
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == 5.0
    assert kwargs["cwd"] is None
    assert kwargs.get("shell", False) is False


@pytest.mark.parametrize(
    "result, failure",
    [
        (_completed(returncode=1), ColdHostBoundFailure.THROTTLE_INVOCATION_FAILED),
        (subprocess.TimeoutExpired(["x"], 5), ColdHostBoundFailure.THROTTLE_TIMEOUT),
    ],
)
def test_throttle_exit_and_timeout_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: subprocess.CompletedProcess[bytes] | BaseException,
    failure: ColdHostBoundFailure,
) -> None:
    """Non-zero completion and timeout never receive a substitute throttle word."""
    reader = _reader(tmp_path, monkeypatch, runner=RecordingRunner(result))
    _assert_failure(failure, reader.read_throttled_word)


def test_throttle_runner_os_error_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runner-side execution failure does not produce a substitute word."""
    reader = _reader(tmp_path, monkeypatch, runner=RecordingRunner(OSError("unavailable")))
    _assert_failure(ColdHostBoundFailure.THROTTLE_INVOCATION_FAILED, reader.read_throttled_word)


def test_throttle_rejects_invalid_binary_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Relative, missing, and non-regular binary paths cannot run."""
    for path in (Path("relative-vcgencmd"), tmp_path / "missing", tmp_path):
        reader = _reader(tmp_path, monkeypatch, command_path=path)
        _assert_failure(ColdHostBoundFailure.THROTTLE_BINARY_MISSING, reader.read_throttled_word)


def test_throttle_stderr_never_reaches_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uncontrolled stderr remains absent from the fixed failure message."""
    secret = b"OPENROUTER_API_KEY=not-for-output"
    reader = _reader(
        tmp_path, monkeypatch, runner=RecordingRunner(_completed(returncode=1, stderr=secret))
    )
    with pytest.raises(ColdHostBoundError) as raised:
        reader.read_throttled_word()
    assert secret.decode("ascii") not in str(raised.value)


def test_memory_accepts_the_exact_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exactly 512 MiB MemAvailable is sufficient."""
    assert _reader(tmp_path, monkeypatch).read_mem_available_bytes() == HOST_MIN_MEM_AVAILABLE_BYTES


@pytest.mark.parametrize(
    "meminfo",
    ["", "MemAvailable: 1 kB\nMemAvailable: 2 kB\n", "MemAvailable: x kB", "MemAvailable: 1"],
)
def test_memory_rejects_missing_or_ambiguous_grammar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, meminfo: str
) -> None:
    """Exactly one decimal-kilobyte MemAvailable line is required."""
    _assert_failure(
        ColdHostBoundFailure.MEMINFO_MALFORMED,
        lambda: _reader(tmp_path, monkeypatch, meminfo=meminfo).read_mem_available_bytes(),
    )


def test_memory_below_floor_and_missing_file_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Low or unreadable memory never looks acceptable."""
    _assert_failure(
        ColdHostBoundFailure.MEMINFO_BELOW_BOUND,
        lambda: _reader(
            tmp_path,
            monkeypatch,
            meminfo=f"MemAvailable: {(HOST_MIN_MEM_AVAILABLE_BYTES // 1024) - 1} kB",
        ).read_mem_available_bytes(),
    )
    reader = _reader(tmp_path, monkeypatch, meminfo_path=tmp_path / "missing")
    _assert_failure(ColdHostBoundFailure.MEMINFO_UNREADABLE, reader.read_mem_available_bytes)


def test_memory_non_ascii_source_is_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decoding failure is malformed rather than an acceptable memory value."""
    meminfo_path = tmp_path / "meminfo"
    reader = _reader(tmp_path, monkeypatch, meminfo_path=meminfo_path)

    def malformed_read(_path: Path, **_kwargs: object) -> str:
        raise UnicodeDecodeError("ascii", b"\xff", 0, 1, "invalid")

    monkeypatch.setattr(Path, "read_text", malformed_read)
    _assert_failure(ColdHostBoundFailure.MEMINFO_MALFORMED, reader.read_mem_available_bytes)


def test_unresolvable_binary_path_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error resolving the binary path is a closed missing-binary failure."""
    reader = _reader(tmp_path, monkeypatch)

    def unavailable_resolve(_path: Path, **_kwargs: object) -> Path:
        raise OSError("unavailable")

    monkeypatch.setattr(Path, "resolve", unavailable_resolve)
    _assert_failure(ColdHostBoundFailure.THROTTLE_BINARY_MISSING, reader.read_throttled_word)


def test_disk_uses_available_not_free_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reserved blocks cannot inflate the space reported to an unprivileged run."""
    reader = _reader(tmp_path, monkeypatch, free_bytes=123)

    def fake_statvfs(_path: object) -> Any:
        return SimpleNamespace(f_bavail=123, f_bfree=999, f_frsize=10)

    monkeypatch.setattr(os, "statvfs", fake_statvfs)
    assert reader.read_free_bytes(tmp_path) == 1230


def test_disk_start_and_run_bounds_are_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The weaker run floor cannot accidentally authorise a start."""
    reader = _reader(tmp_path, monkeypatch, free_bytes=HOST_MIN_FREE_BYTES_DURING)
    reader.check_run_bounds(tmp_path)
    _assert_failure(
        ColdHostBoundFailure.DISK_BELOW_START_BOUND, lambda: reader.check_start_bounds(tmp_path)
    )


def test_disk_below_during_run_floor_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One byte below the during-run floor produces its distinct failure."""
    reader = _reader(tmp_path, monkeypatch, free_bytes=HOST_MIN_FREE_BYTES_DURING - 1)
    _assert_failure(
        ColdHostBoundFailure.DISK_BELOW_RUN_BOUND, lambda: reader.check_run_bounds(tmp_path)
    )


def test_disk_missing_source_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable filesystem source raises the closed disk failure."""
    reader = _reader(tmp_path, monkeypatch)

    def fail_statvfs(_path: object) -> Any:
        raise OSError("no disk")

    monkeypatch.setattr(os, "statvfs", fail_statvfs)
    _assert_failure(ColdHostBoundFailure.DISK_UNREADABLE, lambda: reader.read_free_bytes(tmp_path))


def test_sample_is_finite_and_populated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean sweep returns all host values without a soft path."""
    sample = _reader(tmp_path, monkeypatch).sample(tmp_path)
    assert sample.soc_temp_c == 79.9
    assert sample.mem_available_bytes == HOST_MIN_MEM_AVAILABLE_BYTES
    assert sample.free_bytes == HOST_MIN_FREE_BYTES_BEFORE
    assert sample.throttled_word_hex == "0x0"
    assert sample.captured_at_utc


def test_sample_rejects_non_finite_temperature() -> None:
    """The frozen sample schema cannot serialise a non-finite temperature."""
    with pytest.raises(ValidationError):
        HostBoundSample(
            captured_at_utc="2026-01-01T00:00:00+00:00",
            monotonic_seconds=1.0,
            soc_temp_c=float("nan"),
            throttled_word_hex="0x0",
            mem_available_bytes=HOST_MIN_MEM_AVAILABLE_BYTES,
            free_bytes=HOST_MIN_FREE_BYTES_BEFORE,
        )


def test_non_linux_platform_is_refused_without_monkeypatching_sys_platform() -> None:
    """Platform admission is explicit and does not fabricate a Linux host."""
    _assert_failure(
        ColdHostBoundFailure.PLATFORM_UNSUPPORTED,
        lambda: LinuxHostBoundsReader(platform_name="darwin"),
    )
